/**
 * Orderbook engine adapter, over the engine's own TCP protocol, and the things it will not pretend
 * to be. The same adapter as the reference implementation's `sde.engines.orderbook`, with the same
 * refusals, so a group on this engine reads and writes alike from both libraries.
 *
 * This engine stores L2 depth in one shape fixed in its C++ source (`ORDERBOOK_SHAPE`), so a
 * group either is that shape or cannot be placed here. Each difference from a general-purpose store
 * is named rather than smoothed over:
 *
 * - **No transactions.** `transaction()` refuses: a callback that ran without one would turn a
 *   declared atomicity into a comment.
 * - **No key enforcement.** Two writes with one key both persist (measured), so `get` and a scan
 *   refuse when they meet two rows with one key rather than answer with either.
 * - **Writes are updates of N levels.** `level` is the position of a price within one update, so
 *   `insert` stores level 0 or refuses, and `Session.saveMany` writes whole updates.
 * - **The sequence number is the server's.** It numbers every update per book and refuses a number
 *   chosen by the client. Write `sequence_number` as `null` and read the server's back.
 * - **The engine answers in arrival order.** Its `LIMIT` keeps the first rows to arrive, so a scan
 *   reads complete windows of time, sorts each and assembles the page here.
 * - **It counts nothing over its history.** Its aggregates read the live book, so `Session.count`
 *   and `Session.summarize` are refused by name.
 *
 * Only TCP: the in-process mode of the reference implementation loads the engine's shared library,
 * and this runtime has no binding for it.
 */

import { readFileSync } from 'node:fs'

import { UsageGate } from '../_usage.js'
import { EngineError } from '../errors.js'
import type { PhysicalLayout } from '../placement.js'
import { QueryRefused, type ReadPlan } from '../query.js'
import type { Row } from '../session.js'
import { parseAnswer, statusFields, WireConnection, type WireOptions } from './_orderbook-wire.js'

/** The engine's own name for its storage. One table, and we did not name it. */
export const ORDERBOOK_TABLE = 'orderbook'

/** The nine fields an entity must declare, by name and neutral type, to live in this engine. */
export const ORDERBOOK_SHAPE: Readonly<Record<string, string>> = Object.freeze({
  symbol: 'string',
  exchange: 'string',
  timestamp_ns: 'int64',
  side: 'string',
  level: 'int32',
  price: 'int64',
  quantity: 'int64',
  order_count: 'int32',
  sequence_number: 'int64',
})

/** The key, in the order the engine addresses by. Positional and load-bearing. */
export const ORDERBOOK_KEY: readonly string[] = Object.freeze(['symbol', 'exchange', 'timestamp_ns', 'side', 'level'])

/** The two values `side` may take, sorted as strings compare - the order a key-order scan returns. */
export const SIDES = ['ask', 'bid'] as const

/** The deepest level an update may have: the engine stores at most 1000 levels per side. */
export const MAX_LEVEL = 999
/** The model's int64 against the engine's uint64: no moment before 1970, nothing past int64. */
export const MAX_TIMESTAMP_NS = (1n << 63n) - 1n
const MAX_QUANTITY = (1n << 63n) - 1n
const MAX_ORDER_COUNT = (1n << 31n) - 1n
const MIN_PRICE = -(1n << 63n)
const MAX_PRICE = (1n << 63n) - 1n
/** Updates per pipelined round trip, as in the reference: the engine's rate stops improving there. */
export const BATCH_UPDATES = 64
/** The most rows one query of a scan may bring into this process. */
export const SCAN_CHUNK_ROWS = 100_000
/** The first window of time a scan reads: one second, doubled while sparse, halved when dense. */
export const SCAN_FIRST_WINDOW_NS = 1_000_000_000n
/** The engine's BETWEEN takes uint64s, so "no upper bound" is the largest one. */
const LAST_INSTANT = (1n << 64n) - 1n
/** What the engine sends for a row whose sequence number it does not have. Read as `null`. */
const UNKNOWN_SEQUENCE = 0n

/**
 * What a row read back may hold: the model's types, which are what this library writes.
 *
 * The engine stores unsigned 64-bit times, quantities and numbers, so it can hand back a value the
 * model's int64 cannot hold. No write of this library stores one. Measured: an engine before
 * c1f14c0 read a stored quantity of 2^60 - 1 back as 2^64 - 1, and this adapter returned it.
 */
const READ_BOUNDS: readonly (readonly [string, bigint, bigint])[] = [
  ['timestamp_ns', 0n, MAX_TIMESTAMP_NS],
  ['level', 0n, BigInt(MAX_LEVEL)],
  ['quantity', 0n, MAX_QUANTITY],
  ['order_count', 0n, MAX_ORDER_COUNT],
  ['sequence_number', 1n, (1n << 63n) - 1n],
]

function outOfRange(field: string, low: bigint, high: bigint): string {
  // The value itself is left out: it is read data, and it is not what this library wrote.
  return (
    `the engine returned a row whose ${field} is outside ${low} to ${high}, the range of the model's type. ` +
    'No write of this library stores such a value, so the row is not one it wrote, and it is refused rather ' +
    'than returned. An engine before c1f14c0 read a stored quantity of 2^60 - 1 back as 2^64 - 1.'
  )
}

/** The columns of a `SELECT *` answer, in order. Read by position, so checked against the header. */
const QUERY_COLUMNS = ['timestamp_ns', 'price', 'quantity', 'order_count', 'side', 'level', 'sequence_number']

export interface OrderbookOptions {
  readonly host: string
  readonly port: number
  /** `{identity, secret}` for a server started with `--auth-secret-file`. */
  readonly auth?: { readonly identity: string; readonly secret: string }
  readonly tls?: boolean
  readonly tlsCaFile?: string
  readonly tlsVerify?: boolean
  /** Milliseconds for the connection and for each answer. */
  readonly timeoutMs?: number
}

function name(value: unknown, what: string): string {
  if (typeof value !== 'string' || value === '') {
    throw new EngineError(`${what} must be a non-empty string, not ${describe(value)}`)
  }
  for (const character of value) {
    const point = character.codePointAt(0)!
    if (character === "'" || character === '\\' || /\s/u.test(character) || point < 0x20 || (point >= 0x7f && point < 0xa0)) {
      throw new EngineError(
        `${JSON.stringify(value)} cannot be used as a ${what}: this engine's query language has no escape ` +
          'sequence inside a string literal and its wire protocol separates fields by whitespace, so a ' +
          'quote, a backslash, a space or a control character cannot be stored or addressed faithfully. ' +
          'Refused rather than escaped, because the escaping one would reach for would address a ' +
          'different book without saying so.',
      )
    }
  }
  return value
}

function literal(value: string): string {
  return `'${name(value, 'symbol or exchange')}'`
}

function describe(value: unknown): string {
  return typeof value === 'bigint' ? `${value}n` : typeof value === 'string' ? JSON.stringify(value) : String(value)
}

/** An integer in `[low, high]`, as a bigint, or a refusal naming the field and both bounds. */
function integer(value: unknown, field: string, low: bigint, high: bigint): bigint {
  let exact: bigint
  if (typeof value === 'bigint') exact = value
  else if (typeof value === 'number' && Number.isSafeInteger(value)) exact = BigInt(value)
  else throw new EngineError(`${field} must be an integer (a bigint, or a number within 2^53), not ${describe(value)}`)
  if (exact < low || exact > high) {
    throw new EngineError(
      `${field} ${exact} is outside what this engine stores (${low} to ${high}). Refused before sending: ` +
        'the server would refuse it with a message about tokens, or store something else.',
    )
  }
  return exact
}

type Key = readonly [bigint, string, number]

function keyOf(row: Readonly<Row>): Key {
  return [row['timestamp_ns'] as bigint, row['side'] as string, row['level'] as number]
}

function compareKeys(left: Key, right: Key): number {
  if (left[0] !== right[0]) return left[0] < right[0] ? -1 : 1
  if (left[1] !== right[1]) return left[1] < right[1] ? -1 : 1
  return left[2] - right[2]
}

interface Update {
  readonly symbol: string
  readonly exchange: string
  readonly side: string
  readonly timestamp: bigint
  readonly levels: readonly (readonly [bigint, bigint, bigint])[]
  readonly rows: readonly number[]
}

interface Scan {
  readonly symbol: string
  readonly exchange: string
  readonly side: string | null
  readonly level: number | null
  readonly priceLow: bigint | null
  readonly priceHigh: bigint | null
  readonly low: bigint
  readonly high: bigint
  readonly descending: boolean
  readonly after: Key | null
}

function keeps(scan: Scan, row: Readonly<Row>): boolean {
  if (scan.side !== null && row['side'] !== scan.side) return false
  if (scan.level !== null && row['level'] !== scan.level) return false
  if (scan.after !== null) {
    const order = compareKeys(keyOf(row), scan.after)
    return scan.descending ? order < 0 : order > 0
  }
  return true
}

const EQUAL_FIELDS = new Set(['symbol', 'exchange', 'side', 'level', 'price'])
const RANGE_FIELDS = new Set(['timestamp_ns', 'price'])
const ORDER = ['timestamp_ns', 'side', 'level']

/** The read, or `null` when it provably matches nothing. Every refusal comes before any I/O. */
function scanOf(plan: ReadPlan): Scan | null {
  const equal = new Map<string, unknown>()
  const bounds = new Map<string, { ge?: unknown; lt?: unknown }>()
  for (const filter of plan.filters) {
    const field = filter.column.name
    if (filter.operation === 'eq') equal.set(field, filter.value)
    else {
      const entry = bounds.get(field) ?? {}
      entry[filter.operation] = filter.value
      bounds.set(field, entry)
    }
  }
  const unsupported = [...equal.keys()].filter((field) => !EQUAL_FIELDS.has(field)).sort()
  if (unsupported.length > 0) {
    throw new QueryRefused(
      `this engine cannot filter on ${JSON.stringify(unsupported)}: its query language selects one book by ` +
        'symbol and exchange and narrows it by time and price. Reading a book\'s history to filter it here ' +
        'would be a full scan dressed as a query.',
    )
  }
  const ranged = [...bounds.keys()].filter((field) => !RANGE_FIELDS.has(field)).sort()
  if (ranged.length > 0) {
    throw new QueryRefused(`this engine has no range over ${JSON.stringify(ranged)}; it bounds a read by timestamp_ns or price`)
  }
  if (!equal.has('symbol') || !equal.has('exchange')) {
    throw new QueryRefused(
      'a read here names one book: where must fix both symbol and exchange. The engine\'s query language ' +
        'takes them in its FROM clause, so there is no scan across books - a property of an engine built ' +
        'for one workload, not a limitation to route around.',
    )
  }
  const remaining = plan.order.map((column) => column.name).filter((field) => !equal.has(field))
  const expected = ORDER.filter((field) => !equal.has(field))
  if (remaining.join('\u0000') !== expected.join('\u0000')) {
    throw new QueryRefused(
      `this engine answers one book in time order (then side, then level), and this read asks for ` +
        `${JSON.stringify(remaining)}. Ordering a book's history by anything else would mean reading all of it first.`,
    )
  }
  let low = 0n, high = LAST_INSTANT
  const times = bounds.get('timestamp_ns') ?? {}
  if (times.ge !== undefined) low = maxOf(low, BigInt(times.ge as bigint))
  if (times.lt !== undefined) high = minOf(high, BigInt(times.lt as bigint) - 1n)
  let priceLow: bigint | null = null, priceHigh: bigint | null = null
  const prices = bounds.get('price') ?? {}
  if (prices.ge !== undefined) priceLow = BigInt(prices.ge as bigint)
  if (prices.lt !== undefined) priceHigh = BigInt(prices.lt as bigint) - 1n
  if (equal.has('price')) {
    const value = BigInt(equal.get('price') as bigint)
    priceLow = priceLow === null ? value : maxOf(priceLow, value)
    priceHigh = priceHigh === null ? value : minOf(priceHigh, value)
  }
  const side = equal.has('side') ? String(equal.get('side')) : null
  const level = equal.has('level') ? Number(equal.get('level')) : null
  if (side !== null && !(SIDES as readonly string[]).includes(side)) return null
  let after: Key | null = null
  if (plan.after !== null) {
    const position = new Map(plan.order.map((column, index) => [column.name, plan.after![index]]))
    after = [
      BigInt(position.get('timestamp_ns') as bigint),
      side ?? String(position.get('side')),
      level ?? Number(position.get('level')),
    ]
    if (plan.descending) high = minOf(high, after[0])
    else low = maxOf(low, after[0])
  }
  if (low > high || (priceLow !== null && priceHigh !== null && priceLow > priceHigh)) return null
  return {
    symbol: name(equal.get('symbol'), 'symbol'),
    exchange: name(equal.get('exchange'), 'exchange'),
    side,
    level,
    priceLow,
    priceHigh,
    low,
    high,
    descending: plan.descending,
    after,
  }
}

function maxOf(left: bigint, right: bigint): bigint {
  return left > right ? left : right
}
function minOf(left: bigint, right: bigint): bigint {
  return left < right ? left : right
}

const DSN_PARAMETERS = new Set(['tls', 'ca', 'verify', 'timeout'])
const SWITCH: Readonly<Record<string, boolean>> = { on: true, off: false }

export class OrderbookEngine {
  readonly dialect = 'orderbook'

  /** Why `Session.count` is refused for a group on this engine (`countEngine` in `query.ts`). */
  readonly countRefusal =
    'this engine counts nothing over its history: its aggregates read the live book, not the stored rows, ' +
    'and its query language refuses a time filter on an aggregate. Refused rather than computed from a ' +
    'full scan, which is what a count here would have to be.'

  /** Why `Session.summarize` is refused for a group on this engine. */
  readonly summaryRefusal =
    'this engine summarizes nothing over its history: its aggregates read the live book, not the stored ' +
    'rows. Refused rather than computed from a full scan.'

  /**
   * The scan's two bounds, read at every scan. Tests lower them to reach a split window with a
   * handful of rows, as the reference's tests patch its module constants; nothing else should.
   */
  static limits: { chunkRows: number; firstWindowNs: bigint } = {
    chunkRows: SCAN_CHUNK_ROWS,
    firstWindowNs: SCAN_FIRST_WINDOW_NS,
  }

  private readonly usage = new UsageGate()
  private readonly wire: WireOptions
  private connection: WireConnection | null = null
  private unflushed = 0

  constructor(options: OrderbookOptions) {
    const { host, port } = options
    if (typeof host !== 'string' || host === '') throw new EngineError('an orderbook engine needs a host')
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new EngineError('a host needs a port; this engine has no default port worth guessing')
    }
    const auth = options.auth
    if (auth !== undefined && (
      typeof auth.identity !== 'string' || auth.identity === '' || /\s/u.test(auth.identity) ||
      typeof auth.secret !== 'string' || auth.secret === '')) {
      throw new EngineError(
        'auth is {identity, secret}: two non-empty strings, the identity without whitespace, as in the ' +
          "server's --auth-secret-file",
      )
    }
    const tls = options.tls === true
    if (options.tlsCaFile !== undefined && !tls) {
      throw new EngineError('tlsCaFile without tls verifies nothing: the connection would be plain text')
    }
    if (options.tlsVerify === false && !tls) {
      throw new EngineError('tlsVerify: false without tls: there is no certificate to decline to check')
    }
    if (options.tlsCaFile !== undefined && options.tlsVerify === false) {
      throw new EngineError('tlsCaFile with tlsVerify: false is a trust anchor nothing consults')
    }
    const timeoutMs = options.timeoutMs ?? 10_000
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw new EngineError('timeoutMs is a positive number of milliseconds')
    this.wire = {
      host,
      port,
      ...(auth === undefined ? {} : { auth: { identity: auth.identity, secret: auth.secret } }),
      tls,
      ...(options.tlsCaFile === undefined ? {} : { tlsCaFile: options.tlsCaFile }),
      tlsVerify: options.tlsVerify !== false,
      timeoutMs,
    }
    if (options.tlsCaFile !== undefined) {
      // Read now, so a missing file is a refusal at construction rather than at the first connect.
      try {
        readFileSync(options.tlsCaFile)
      } catch {
        throw new EngineError(`tlsCaFile ${JSON.stringify(options.tlsCaFile)} is not a readable file`)
      }
    }
  }

  /**
   * A connection from `orderbook://[identity[:secret]@]host:port[?tls=on&ca=PATH&verify=off&timeout=S]`,
   * the form the reference implementation reads, with the same refusals. No message repeats the
   * DSN, which carries the secret.
   */
  static fromDsn(dsn: string): OrderbookEngine {
    if (typeof dsn !== 'string') throw new EngineError('an orderbook DSN is a string')
    let parts: URL
    try {
      parts = new URL(dsn)
    } catch {
      throw new EngineError('an orderbook DSN is orderbook://[identity:secret@]host:port[?options]')
    }
    if (parts.protocol !== 'orderbook:') throw new EngineError('an orderbook DSN starts with orderbook://')
    if (parts.hostname === '' || parts.port === '') {
      throw new EngineError('an orderbook DSN names a host and a port: orderbook://host:port')
    }
    if ((parts.pathname !== '' && parts.pathname !== '/') || parts.hash !== '') {
      throw new EngineError('an orderbook DSN has no path or fragment')
    }
    const identity = parts.username === '' ? undefined : decodeURIComponent(parts.username)
    const secret = parts.password === '' ? undefined : decodeURIComponent(parts.password)
    if ((identity === undefined) !== (secret === undefined)) {
      throw new EngineError("an orderbook DSN's identity needs its secret: identity:secret@host")
    }
    const options = new Map<string, string>()
    for (const [key, value] of parts.searchParams) {
      if (!DSN_PARAMETERS.has(key)) {
        throw new EngineError(`the orderbook DSN parameter ${JSON.stringify(key)} is unknown; it takes ${JSON.stringify([...DSN_PARAMETERS].sort())}`)
      }
      if (options.has(key)) throw new EngineError(`the orderbook DSN gives ${JSON.stringify(key)} twice`)
      options.set(key, value)
    }
    for (const key of ['tls', 'verify']) {
      const value = options.get(key)
      if (value !== undefined && !(value in SWITCH)) throw new EngineError(`the orderbook DSN parameter ${JSON.stringify(key)} is on or off`)
    }
    const seconds = Number(options.get('timeout') ?? '10')
    if (!Number.isFinite(seconds) || seconds <= 0) throw new EngineError("the orderbook DSN parameter 'timeout' is a number of seconds")
    const host = parts.hostname.startsWith('[') ? parts.hostname.slice(1, -1) : parts.hostname
    return new OrderbookEngine({
      host,
      port: Number(parts.port),
      ...(identity === undefined || secret === undefined ? {} : { auth: { identity, secret } }),
      tls: SWITCH[options.get('tls') ?? 'off']!,
      ...(options.get('ca') === undefined ? {} : { tlsCaFile: options.get('ca')! }),
      tlsVerify: SWITCH[options.get('verify') ?? 'on']!,
      timeoutMs: seconds * 1000,
    })
  }

  toString(): string {
    const identity = this.wire.auth === undefined ? '' : `, identity=${JSON.stringify(this.wire.auth.identity)}`
    return `OrderbookEngine(tcp ${this.wire.host}:${this.wire.port}${identity}, tls=${this.wire.tls === true ? 'on' : 'off'})`
  }

  /** Never the secret, whatever inspects this object. */
  [Symbol.for('nodejs.util.inspect.custom')](): string {
    return this.toString()
  }

  toJSON(): string {
    return this.toString()
  }

  // --- connection ----------------------------------------------------------------------------

  async connect(): Promise<void> {
    return this.usage.operation(async () => {
      if (this.connection !== null) return
      let connection: WireConnection
      try {
        connection = await WireConnection.open(this.wire)
      } catch (error) {
        throw new EngineError(`could not open the orderbook engine: ${message(error)}`)
      }
      let capabilities: Set<string>
      try {
        const answer = parseAnswer(await connection.execute('STATUS'))
        if (answer.error !== null) throw new EngineError(answer.error)
        capabilities = new Set((statusFields(answer.raw).get('capabilities') ?? '').split(',').filter(Boolean))
      } catch (error) {
        await connection.close()
        throw new EngineError(`could not read the orderbook server's capabilities: ${message(error)}`)
      }
      if (!capabilities.has('insert_event_time')) {
        await connection.close()
        throw new EngineError(
          'this orderbook server cannot store a write\'s event time, so every update would be stamped with its ' +
            'arrival instead of the time it happened and could not be found by the time it carries. Upgrade ' +
            'the server; nothing was written.',
        )
      }
      this.connection = connection
    })
  }

  async close(): Promise<void> {
    return this.usage.operation(async () => {
      const connection = this.connection
      this.connection = null
      this.unflushed = 0
      await connection?.close()
    })
  }

  private open(): WireConnection {
    if (this.connection === null || !this.connection.isOpen) {
      if (this.connection !== null) {
        this.connection = null
        throw new EngineError('the connection to the orderbook engine was lost; connect() again')
      }
      throw new EngineError('not connected; call connect() first')
    }
    return this.connection
  }

  private async lose(): Promise<void> {
    const connection = this.connection
    this.connection = null
    this.unflushed = 0
    try {
      await connection?.close()
    } catch {
      // The caller already holds the error that says the outcome is unknown.
    }
  }

  // --- schema --------------------------------------------------------------------------------

  private checkLayout(layout: PhysicalLayout, keys: Readonly<Record<string, readonly string[]>>): void {
    const entities = Object.keys(layout.tables).sort()
    if (entities.length !== 1) {
      throw new EngineError(
        `this engine stores one thing and the map gives it ${JSON.stringify(entities)}. A colocation group is ` +
          'what shares an engine, so a group of two cannot be placed here.',
      )
    }
    const entity = entities[0]!
    const table = layout.tables[entity]!
    if (table !== ORDERBOOK_TABLE) {
      throw new EngineError(
        `the map calls the table ${JSON.stringify(table)} and this engine's storage is ${JSON.stringify(ORDERBOOK_TABLE)}. ` +
          'The name is the engine\'s, not ours: there is no CREATE TABLE to send it, so a map naming something ' +
          'else was built for another engine.',
      )
    }
    const declared = Object.keys(layout.columns[entity] ?? {})
    const expected = Object.keys(ORDERBOOK_SHAPE)
    const missing = expected.filter((field) => !declared.includes(field)).sort()
    const extra = declared.filter((field) => !expected.includes(field)).sort()
    if (missing.length > 0 || extra.length > 0) {
      throw new EngineError(
        `the map's layout for ${entity} does not match this engine's fixed shape:` +
          (missing.length > 0 ? ` missing ${JSON.stringify(missing)}` : '') +
          (extra.length > 0 ? ` unexpected ${JSON.stringify(extra)}` : '') +
          `. The shape is fixed in the engine and the whole of it is ${JSON.stringify(expected.slice().sort())}.`,
      )
    }
    const key = [...(keys[entity] ?? [])]
    if (key.join('\u0000') !== ORDERBOOK_KEY.join('\u0000')) {
      throw new EngineError(
        `the map keys ${entity} by ${JSON.stringify(key)} and this engine addresses rows by ${JSON.stringify(ORDERBOOK_KEY)}. ` +
          'The order is positional and it is load-bearing: the symbol and the exchange are how a query reaches the data at all.',
      )
    }
  }

  /** Verify, because there is nothing to create: the storage exists when the engine opens it. */
  async ensureSchema(layout: PhysicalLayout, options: { readonly keys: Readonly<Record<string, readonly string[]>> }): Promise<readonly never[]> {
    this.checkLayout(layout, options.keys)
    return []
  }

  /** The same check, for a session opening on a generation-bearing map, with no findings. */
  async validateSchema(
    layout: PhysicalLayout,
    options?: { readonly keys?: Readonly<Record<string, readonly string[]>> },
  ): Promise<readonly never[]> {
    this.checkLayout(layout, options?.keys ?? {})
    return []
  }

  // --- writes --------------------------------------------------------------------------------

  private table(table: string): void {
    if (table !== ORDERBOOK_TABLE) throw new EngineError(`this engine has one table, ${JSON.stringify(ORDERBOOK_TABLE)}, not ${JSON.stringify(table)}`)
  }

  private sequence(value: unknown): void {
    if (value === null || value === undefined) return
    throw new EngineError(
      "over TCP the sequence number is the server's: it numbers every update per book, and a number chosen " +
        'here would be refused by a current server and silently replaced by an older one, so the row read ' +
        'back would disagree with the row written. Write sequence_number as null and read the server\'s ' +
        'number back.',
    )
  }

  private levelsOf(levels: readonly (readonly [unknown, unknown, unknown])[], where: string): (readonly [bigint, bigint, bigint])[] {
    if (levels.length === 0) {
      throw new EngineError('an update with no levels is not an empty update, it is a write that would report success without storing anything')
    }
    if (levels.length > MAX_LEVEL + 1) {
      throw new EngineError(`${where}has ${levels.length} levels and this engine stores at most ${MAX_LEVEL + 1} per side`)
    }
    return levels.map(([price, quantity, count]) => [
      integer(price, `${where}price`, MIN_PRICE, MAX_PRICE),
      integer(quantity, `${where}quantity`, 0n, MAX_QUANTITY),
      integer(count, `${where}order_count`, 0n, MAX_ORDER_COUNT),
    ] as const)
  }

  private shapeOf(values: Readonly<Row>, where: string): void {
    const fields = Object.keys(ORDERBOOK_SHAPE)
    const missing = fields.filter((field) => field !== 'sequence_number' && !Object.hasOwn(values, field)).sort()
    if (missing.length > 0) {
      throw new EngineError(`${where}missing ${JSON.stringify(missing)}: every field of the fixed shape is required, and this engine has no defaults`)
    }
    const extra = Object.keys(values).filter((field) => !fields.includes(field)).sort()
    if (extra.length > 0) {
      throw new EngineError(
        `${where}carries ${JSON.stringify(extra)}, which this engine has nowhere to store: its shape is fixed. A ` +
          'write generation in particular is never stamped here: a group on this engine carries no write generation.',
      )
    }
  }

  private command(update: Update): string {
    const header = update.levels.length > 1
      ? `MINSERT ${update.symbol} ${update.exchange} ${update.side} ${update.levels.length} ${update.timestamp}`
      : `INSERT ${update.symbol} ${update.exchange} ${update.side} ${update.levels[0]![0]} ${update.levels[0]![1]} ${update.levels[0]![2]} ${update.timestamp}`
    if (update.levels.length === 1) return header
    return header + '\n' + update.levels.map(([price, quantity, count]) => `${price} ${quantity} ${count}`).join('\n')
  }

  /** One depth level, at level 0, or a refusal - level is a position within an update. */
  async insert(table: string, values: Readonly<Row>): Promise<void> {
    return this.usage.operation(async () => {
      this.table(table)
      this.shapeOf(values, `insert into ${table}: `)
      const level = integer(values['level'], 'level', 0n, BigInt(MAX_LEVEL))
      if (level !== 0n) {
        throw new EngineError(
          `insert into ${table} declares level ${level}, and this engine's write API has no level parameter - a ` +
            "price's level is its index within one update. Writing this would store it at level 0 and the read " +
            'would disagree with the write. Write the whole update with Session.saveMany (levels 0 to n-1 of ' +
            'one side of one book at one instant), which is the granularity this engine has.',
        )
      }
      const update = this.updates([values])[0]!
      const connection = this.open()
      let raw: string
      try {
        raw = await connection.execute(this.command(update))
      } catch (error) {
        await this.lose()
        throw new EngineError(
          `the engine did not confirm the write to ${table}: ${message(error)}. If the connection dropped after ` +
            'the update was sent, it may still have been stored: read the book before writing it again.',
        )
      }
      const answer = parseAnswer(raw)
      if (answer.error !== null) throw new EngineError(`insert into ${table} failed: ${answer.error}`)
      this.unflushed += 1
    })
  }

  /** Rows grouped into the engine's updates, every malformed batch refused before sending. */
  private updates(rows: readonly Readonly<Row>[]): Update[] {
    interface Entry { readonly levels: Map<number, readonly [bigint, bigint, bigint]>; readonly rows: Map<number, number> }
    const grouped = new Map<string, { readonly book: readonly [string, string, string, bigint]; readonly entry: Entry }>()
    rows.forEach((row, index) => {
      const where = `row ${index}: `
      this.shapeOf(row, where)
      const side = row['side']
      if (typeof side !== 'string' || !(SIDES as readonly string[]).includes(side)) {
        throw new EngineError(`${where}side must be one of ${JSON.stringify(SIDES)}, not ${describe(side)}`)
      }
      const book = [
        name(row['symbol'], 'symbol'),
        name(row['exchange'], 'exchange'),
        side,
        integer(row['timestamp_ns'], `${where}timestamp_ns`, 0n, MAX_TIMESTAMP_NS),
      ] as const
      const level = Number(integer(row['level'], `${where}level`, 0n, BigInt(MAX_LEVEL)))
      const value = this.levelsOf([[row['price'], row['quantity'], row['order_count']]], where)[0]!
      this.sequence(row['sequence_number'])
      const id = `${book[0]}\u0000${book[1]}\u0000${book[2]}\u0000${book[3]}`
      let group = grouped.get(id)
      if (group === undefined) {
        group = { book, entry: { levels: new Map(), rows: new Map() } }
        grouped.set(id, group)
      }
      if (group.entry.levels.has(level)) {
        throw new EngineError(
          `rows ${group.entry.rows.get(level)} and ${index} are both level ${level} of one update ` +
            `(${book[0]} ${book[1]} ${side} at ${book[3]}). An update holds one price per level, so the second would have nowhere to go.`,
        )
      }
      group.entry.levels.set(level, value)
      group.entry.rows.set(level, index)
    })
    const updates: Update[] = []
    for (const { book, entry } of grouped.values()) {
      const depth = entry.levels.size
      const present = [...entry.levels.keys()].sort((a, b) => a - b)
      if (present.some((level, position) => level !== position)) {
        throw new EngineError(
          `the update for ${book[0]} ${book[1]} ${book[2]} at ${book[3]} has levels ${JSON.stringify(present)}. An update ` +
            'is levels 0 to n-1 without a gap: the engine numbers levels by position, so a missing one would renumber every level after it.',
        )
      }
      updates.push({
        symbol: book[0],
        exchange: book[1],
        side: book[2],
        timestamp: book[3],
        levels: Array.from({ length: depth }, (_, level) => entry.levels.get(level)!),
        rows: Array.from({ length: depth }, (_, level) => entry.rows.get(level)!),
      })
    }
    return updates
  }

  /**
   * A batch of rows, written as the engine's updates, in pipelined round trips of `BATCH_UPDATES`.
   *
   * **Not a transaction.** A refusal by the server raises with the first update it refused and how
   * many were stored. A failed connection raises with the outcome named as unknown and closes this
   * adapter, so a reply still on its way is not read as the answer to the next write.
   */
  async insertMany(table: string, rows: readonly Readonly<Row>[]): Promise<void> {
    return this.usage.operation(async () => {
      this.table(table)
      const updates = this.updates(rows)
      let stored = 0
      for (let start = 0; start < updates.length; start += BATCH_UPDATES) {
        const part = updates.slice(start, start + BATCH_UPDATES)
        const connection = this.open()
        let answers: string[]
        try {
          answers = await connection.executePipelined(part.map((update) => this.command(update)))
        } catch (error) {
          await this.lose()
          throw new EngineError(
            `the outcome of this batch is unknown: ${stored} of its ${updates.length} updates were confirmed, and ` +
              `the connection failed on the next ${part.length} (${message(error)}). Those may have been stored in ` +
              'full, in part or not at all; read the book before writing them again, then connect() again.',
          )
        }
        const outcomes = answers.map((raw) => parseAnswer(raw))
        const refused = outcomes.map((outcome, index) => [outcome, index] as const).filter(([outcome]) => outcome.error !== null)
        stored += outcomes.length - refused.length
        for (const [index, outcome] of outcomes.entries()) if (outcome.error === null) this.unflushed += part[index]!.levels.length
        if (refused.length > 0) {
          const [first, index] = refused[0]!
          throw new EngineError(
            `the server refused ${refused.length} of ${updates.length} updates in this batch; the first was update ` +
              `${start + index} (rows ${JSON.stringify(part[index]!.rows)}): ${first.error}. ${stored} updates were stored - ` +
              'a batch here is not a transaction - and any after this part of the batch were not sent.',
          )
        }
      }
    })
  }

  /** Make everything written so far queryable; reads do it themselves when there is something to. */
  async flush(): Promise<void> {
    return this.usage.operation(async () => this.flushNow())
  }

  private async flushNow(): Promise<void> {
    if (this.unflushed === 0) return
    const connection = this.open()
    let raw: string
    try {
      raw = await connection.execute('FLUSH')
    } catch (error) {
      await this.lose()
      throw new EngineError(`flush failed: ${message(error)}`)
    }
    const answer = parseAnswer(raw)
    if (answer.error !== null) throw new EngineError(`flush failed: ${answer.error}`)
    this.unflushed = 0
  }

  // --- reads ---------------------------------------------------------------------------------

  /** Rows of one query; an unknown book is an empty one, because the server says which it is. */
  private async query(statement: string): Promise<(readonly string[])[]> {
    const connection = this.open()
    let raw: string
    try {
      raw = await connection.execute(statement)
    } catch (error) {
      await this.lose()
      throw new EngineError(`query failed: ${statement}: ${message(error)}`)
    }
    const answer = parseAnswer(raw)
    if (answer.error !== null) {
      if (answer.error.includes('OB_ERR_NOT_FOUND')) return []
      throw new EngineError(`query failed: ${statement}: ${answer.error}`)
    }
    if (answer.header.length < 6 || answer.header.some((column, index) => column !== QUERY_COLUMNS[index])) {
      throw new EngineError(
        `this adapter reads the standard row columns by position and the server answered with ${JSON.stringify(answer.header)}`,
      )
    }
    return answer.rows.map((row) => [...row])
  }

  private static row(symbol: string, exchange: string, fields: readonly string[]): Row {
    const sequence = fields.length > 6 ? BigInt(fields[6]!) : UNKNOWN_SEQUENCE
    const row: Row = {
      symbol,
      exchange,
      timestamp_ns: BigInt(fields[0]!),
      side: fields[4] === '0' ? 'bid' : 'ask',
      level: Number(fields[5]),
      price: BigInt(fields[1]!),
      quantity: BigInt(fields[2]!),
      order_count: Number(fields[3]),
      // Unknown, not zero: the engine's own numbering starts at 1.
      sequence_number: sequence === UNKNOWN_SEQUENCE ? null : sequence,
    }
    for (const [name, low, high] of READ_BOUNDS) {
      const value = row[name]
      if (value === null) continue
      const number = BigInt(value as bigint | number)
      if (number < low || number > high) throw new EngineError(outOfRange(name, low, high))
    }
    return row
  }

  private static duplicate(table: string, key: Readonly<Row>, count: number): string {
    const shown = Object.fromEntries(ORDERBOOK_KEY.map((field) => [field, typeof key[field] === 'bigint' ? String(key[field]) : key[field]]))
    return (
      `${count} rows in ${table} share the key ${JSON.stringify(shown)}. This engine is an append-only log of ` +
      'depth updates and does not enforce a key, so this is a key violation it could not have prevented. ' +
      'Refused rather than answered with one of them: picking either would be a read that lies about ' +
      'uniqueness, and you would not see it.'
    )
  }

  /** One row by key, `null` if there is none, and a refusal if there are two. */
  async get(table: string, key: Readonly<Row>): Promise<Row | null> {
    return this.usage.operation(async () => {
      this.table(table)
      const missing = ORDERBOOK_KEY.filter((field) => !Object.hasOwn(key, field)).sort()
      if (missing.length > 0) {
        throw new EngineError(
          `get from ${table} is missing ${JSON.stringify(missing)} from the key. This engine addresses rows by ` +
            `${JSON.stringify(ORDERBOOK_KEY)} and cannot scan for a partial one.`,
        )
      }
      let timestamp: bigint
      try {
        timestamp = integer(key['timestamp_ns'], 'timestamp_ns', -(1n << 63n), MAX_TIMESTAMP_NS)
      } catch {
        return null
      }
      if (timestamp < 0n || !(SIDES as readonly string[]).includes(String(key['side']))) return null
      await this.flushNow()
      const symbol = name(key['symbol'], 'symbol'), exchange = name(key['exchange'], 'exchange')
      const chunk = OrderbookEngine.limits.chunkRows
      const raw = await this.query(
        `SELECT * FROM ${literal(symbol)}.${literal(exchange)} WHERE timestamp BETWEEN ${timestamp} AND ${timestamp} LIMIT ${chunk + 1}`,
      )
      if (raw.length > chunk) throw new EngineError(`${symbol} ${exchange} holds more than ${chunk} rows at the instant ${timestamp}`)
      const level = Number(key['level'])
      const rows = raw
        .map((fields) => OrderbookEngine.row(symbol, exchange, fields))
        .filter((row) => row['side'] === key['side'] && row['level'] === level)
      if (rows.length === 0) return null
      if (rows.length > 1) throw new EngineError(OrderbookEngine.duplicate(table, key, rows.length))
      return rows[0]!
    })
  }

  private async window(scan: Scan, low: bigint, high: bigint): Promise<{ rows: Row[]; sent: number } | null> {
    let price = ''
    if (scan.priceLow !== null || scan.priceHigh !== null) {
      price = ` AND price BETWEEN ${scan.priceLow ?? MIN_PRICE} AND ${scan.priceHigh ?? MAX_PRICE}`
    }
    const chunk = OrderbookEngine.limits.chunkRows
    const raw = await this.query(
      `SELECT * FROM ${literal(scan.symbol)}.${literal(scan.exchange)} WHERE timestamp BETWEEN ${low} AND ${high}${price} LIMIT ${chunk + 1}`,
    )
    if (raw.length > chunk) return null
    const rows = raw.map((fields) => OrderbookEngine.row(scan.symbol, scan.exchange, fields)).filter((row) => keeps(scan, row))
    rows.sort((left, right) => (scan.descending ? -1 : 1) * compareKeys(keyOf(left), keyOf(right)))
    return { rows, sent: raw.length }
  }

  /**
   * One page of one book in key order, assembled from complete windows of time: the engine answers
   * in arrival order, so the page cannot be its `LIMIT`. The window doubles while sparse and halves
   * past `SCAN_CHUNK_ROWS`, and one instant holding more than that is refused, as in the reference.
   */
  async selectRows(table: string, plan: ReadPlan): Promise<Row[]> {
    return this.usage.operation(async () => {
      this.table(table)
      const scan = scanOf(plan)
      if (scan === null) return []
      await this.flushNow()
      const need = plan.limit + 1
      const page: Row[] = []
      const { chunkRows, firstWindowNs } = OrderbookEngine.limits
      let width = firstWindowNs
      let low = scan.low, high = scan.high
      while (low <= high && page.length < need) {
        const [start, end] = scan.descending
          ? [maxOf(low, high - width + 1n), high]
          : [low, minOf(high, low + width - 1n)]
        const window = await this.window(scan, start, end)
        if (window === null) {
          if (start === end) {
            throw new EngineError(
              `${scan.symbol} ${scan.exchange} holds more than ${chunkRows} rows at the instant ${start}; that many ` +
                'updates at one nanosecond is a key collision on a scale this read cannot page through',
            )
          }
          width = maxOf(1n, (end - start + 1n) / 2n)
          continue
        }
        page.push(...window.rows.slice(0, need - page.length))
        if (scan.descending) high = start - 1n
        else low = end + 1n
        if (page.length < need && window.sent * 4 < chunkRows) width *= 2n
      }
      for (let index = 1; index < page.length; index += 1) {
        if (compareKeys(keyOf(page[index - 1]!), keyOf(page[index]!)) === 0) {
          throw new EngineError(OrderbookEngine.duplicate(table, page[index]!, 2))
        }
      }
      return page
    })
  }

  // --- transactions --------------------------------------------------------------------------

  async transaction<T>(_body: () => Promise<T>): Promise<T> {
    throw new EngineError(
      'this engine has no multi-statement transactions, so there is nothing here to give you. One group is one ' +
        "engine's transaction semantics: if two entities must change together, declare that with atomic_with " +
        'and the planner will place them somewhere that can. Refused rather than quietly running the body, ' +
        'because a transaction that is not one is worse than not having the method.',
    )
  }
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

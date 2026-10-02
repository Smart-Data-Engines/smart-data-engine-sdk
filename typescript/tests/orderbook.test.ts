/**
 * The orderbook adapter's decisions, against a server that speaks the engine's protocol in this
 * process: arrival order, `LIMIT` on arrival, `OB_ERR_NOT_FOUND`, a sequence number per book, the
 * authentication challenge. The same decisions as the reference's `test_orderbook_tcp.py`; what the
 * engine itself does is held by `orderbook.slice.test.ts` against a real `ob_tcp_server`.
 */

import { createHmac, randomBytes } from 'node:crypto'
import { createServer, type Server, type Socket } from 'node:net'

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { EngineError, loadMap, QueryRefused, Recorder, Session } from '../src/index.js'
import { OrderbookEngine, ORDERBOOK_KEY, ORDERBOOK_SHAPE, ORDERBOOK_TABLE } from '../src/engines/orderbook.js'
import { authDigest } from '../src/engines/_orderbook-wire.js'
import { planRead, type ReadColumn } from '../src/query.js'
import { modelFromNeutral } from '../src/testing/loader.js'

interface StoredRow {
  readonly symbol: string
  readonly exchange: string
  readonly timestamp: bigint
  readonly side: 0 | 1
  readonly level: number
  readonly price: bigint
  readonly quantity: bigint
  readonly count: bigint
  readonly sequence: bigint
}

/** A book store that answers like the engine: arrival order, LIMIT on arrival, NOT_FOUND. */
class FakeServer {
  readonly rows: StoredRow[] = []
  readonly commands: string[] = []
  capabilities = 'insert_event_time,strict_args,backup'
  auth: { identity: string; secret: string } | null = null
  /** Destroy the connection on receiving this command, counted from 1 over writes only. */
  dropOnWrite: number | null = null
  /** Refuse these writes, counted from 0 over writes only. */
  readonly refuse = new Set<number>()
  private writes = 0
  private readonly sequences = new Map<string, bigint>()
  private server: Server | null = null
  private readonly sockets = new Set<Socket>()
  port = 0

  async start(): Promise<void> {
    this.server = createServer((socket) => this.serve(socket))
    await new Promise<void>((resolve) => this.server!.listen(0, '127.0.0.1', resolve))
    const address = this.server.address()
    this.port = typeof address === 'object' && address !== null ? address.port : 0
  }

  async stop(): Promise<void> {
    // A test that failed before closing its engine leaves a connection open, and close() waits for it.
    for (const socket of this.sockets) socket.destroy()
    await new Promise<void>((resolve) => this.server?.close(() => resolve()))
  }

  store(symbol: string, exchange: string, side: 0 | 1, timestamp: bigint, levels: readonly (readonly [bigint, bigint, bigint])[]): void {
    const book = `${symbol}\u0000${exchange}`
    const sequence = (this.sequences.get(book) ?? 0n) + 1n
    this.sequences.set(book, sequence)
    levels.forEach(([price, quantity, count], level) => {
      this.rows.push({ symbol, exchange, timestamp, side, level, price, quantity, count, sequence })
    })
  }

  private serve(socket: Socket): void {
    this.sockets.add(socket)
    socket.on('close', () => this.sockets.delete(socket))
    let buffer = ''
    let nonce: string | null = null
    let authenticated = this.auth === null
    socket.write('OK ob_tcp_server v0.1.0\n\n')
    socket.on('data', (chunk) => {
      buffer += chunk.toString('utf8')
      for (;;) {
        const newline = buffer.indexOf('\n')
        if (newline === -1) return
        const line = buffer.slice(0, newline)
        let consumed = newline + 1
        let body: string[] = []
        if (line.startsWith('MINSERT ')) {
          const count = Number(line.split(' ')[4])
          const lines = buffer.slice(consumed).split('\n')
          if (lines.length <= count) return
          body = lines.slice(0, count)
          consumed += body.reduce((total, item) => total + item.length + 1, 0)
        }
        buffer = buffer.slice(consumed)
        this.commands.push(body.length > 0 ? `${line}\n${body.join('\n')}` : line)
        if (line === 'QUIT') {
          socket.end()
          return
        }
        if (line === 'AUTH') {
          nonce = randomBytes(8).toString('hex')
          socket.write(`OK CHALLENGE ${nonce}\n\n`)
          continue
        }
        if (line.startsWith('AUTH ')) {
          const [, identity, digest] = line.split(' ')
          const expected = this.auth === null || nonce === null ? '' : authDigest(this.auth.identity, this.auth.secret, nonce)
          if (this.auth !== null && identity === this.auth.identity && digest === expected) {
            authenticated = true
            socket.write('OK AUTH\n\n')
          } else {
            socket.write('ERR auth_failed\n')
          }
          continue
        }
        if (!authenticated) {
          socket.write('ERR auth_required\n')
          continue
        }
        if (line.startsWith('INSERT ') || line.startsWith('MINSERT ')) {
          this.writes += 1
          if (this.dropOnWrite !== null && this.writes === this.dropOnWrite) {
            socket.destroy()
            return
          }
          if (this.refuse.has(this.writes - 1)) {
            socket.write('ERR something the server said\n')
            continue
          }
          const parts = line.split(' ')
          const side = parts[3] === 'ask' ? 1 : 0
          if (parts[0] === 'INSERT') {
            this.store(parts[1]!, parts[2]!, side, BigInt(parts[7]!), [[BigInt(parts[4]!), BigInt(parts[5]!), BigInt(parts[6]!)]])
          } else {
            this.store(parts[1]!, parts[2]!, side, BigInt(parts[5]!), body.map((item) => {
              const [price, quantity, count] = item.split(' ')
              return [BigInt(price!), BigInt(quantity!), BigInt(count!)] as const
            }))
          }
          socket.write('OK\n\n')
          continue
        }
        if (line === 'FLUSH') {
          socket.write('OK\n\n')
          continue
        }
        if (line === 'STATUS') {
          socket.write(`OK\nsessions\tqueries\tinserts\n1\t0\t0\ncapabilities: ${this.capabilities}\nrole: standalone\n\n`)
          continue
        }
        const select = /^SELECT \* FROM '([^']+)'\.'([^']+)'(?: WHERE timestamp BETWEEN (\d+) AND (\d+)(?: AND price BETWEEN (-?\d+) AND (-?\d+))?)? LIMIT (\d+)$/.exec(line)
        if (select === null) {
          socket.write(`ERR the fake cannot parse: ${line}\n`)
          continue
        }
        const [, symbol, exchange, low, high, priceLow, priceHigh, limit] = select
        let book = this.rows.filter((row) => row.symbol === symbol && row.exchange === exchange)
        if (book.length === 0) {
          socket.write(`ERR OB_ERR_NOT_FOUND: symbol '${symbol}' exchange '${exchange}' not found\n`)
          continue
        }
        if (low !== undefined) book = book.filter((row) => row.timestamp >= BigInt(low) && row.timestamp <= BigInt(high!))
        if (priceLow !== undefined) book = book.filter((row) => row.price >= BigInt(priceLow) && row.price <= BigInt(priceHigh!))
        book = book.slice(0, Number(limit))
        socket.write(
          'OK\ntimestamp_ns\tprice\tquantity\torder_count\tside\tlevel\tsequence_number\n' +
            book.map((row) => `${row.timestamp}\t${row.price}\t${row.quantity}\t${row.count}\t${row.side}\t${row.level}\t${row.sequence}\n`).join('') +
            '\n',
        )
      }
    })
    socket.on('error', () => {})
  }
}

let server: FakeServer
beforeEach(async () => {
  server = new FakeServer()
  await server.start()
})
afterEach(async () => {
  OrderbookEngine.limits = { chunkRows: 100_000, firstWindowNs: 1_000_000_000n }
  await server.stop()
})

async function connected(options: Partial<ConstructorParameters<typeof OrderbookEngine>[0]> = {}): Promise<OrderbookEngine> {
  const engine = new OrderbookEngine({ host: '127.0.0.1', port: server.port, timeoutMs: 2000, ...options })
  await engine.connect()
  return engine
}

function row(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    symbol: 'BTCUSDT',
    exchange: 'binance',
    timestamp_ns: 1_000n,
    side: 'bid',
    level: 0,
    price: 5_000_000n,
    quantity: 3n,
    order_count: 1,
    sequence_number: null,
    ...overrides,
  }
}

function model() {
  return modelFromNeutral({
    entities: [
      {
        name: 'DepthLevel',
        fields: Object.entries(ORDERBOOK_SHAPE).map(([name, type]) => ({ name, type, nullable: name === 'sequence_number' })),
        key: [...ORDERBOOK_KEY],
      },
    ],
    relations: [],
    atomic: [],
  })
}

function plan(options: Parameters<typeof planRead>[2]) {
  const spec = model().entities[0]!
  const columns: ReadColumn[] = spec.fields.map((field) => ({ name: field.name, type: field.type }))
  return planRead(columns, spec.key, options)
}

const BOOK = { symbol: 'BTCUSDT', exchange: 'binance' }

describe('connecting', () => {
  it('refuses a setting that would do nothing', () => {
    const base = { host: 'h', port: 1 }
    expect(() => new OrderbookEngine({ ...base, port: 0 })).toThrow('needs a port')
    expect(() => new OrderbookEngine({ ...base, tlsVerify: false })).toThrow('no certificate to decline')
    expect(() => new OrderbookEngine({ ...base, tlsCaFile: '/nope' })).toThrow('verifies nothing')
    expect(() => new OrderbookEngine({ ...base, auth: { identity: 'a b', secret: 's' } })).toThrow('without whitespace')
    expect(() => new OrderbookEngine({ ...base, auth: { identity: 'a', secret: '' } })).toThrow('two non-empty strings')
  })

  it('reads a DSN and never repeats its secret', () => {
    const engine = OrderbookEngine.fromDsn('orderbook://desk:s%3Ecret@localhost:59091?timeout=3')
    expect(String(engine)).toBe('OrderbookEngine(tcp localhost:59091, identity="desk", tls=off)')
    expect(JSON.stringify({ engine })).not.toContain('cret')
    for (const [dsn, fragment] of [
      ['postgres://h:1', 'starts with orderbook://'],
      ['orderbook://h', 'names a host and a port'],
      ['orderbook://h:1/db', 'no path'],
      ['orderbook://desk@h:1', 'needs its secret'],
      ['orderbook://h:1?sslmode=require', 'is unknown'],
      ['orderbook://h:1?tls=yes', 'is on or off'],
      ['orderbook://h:1?verify=off', 'no certificate to decline'],
      ['orderbook://h:1?timeout=-1', 'number of seconds'],
    ] as const) {
      expect(() => OrderbookEngine.fromDsn(dsn), dsn).toThrow(fragment)
    }
    try {
      OrderbookEngine.fromDsn('orderbook://desk:topsecret@h:1?tls=maybe')
    } catch (error) {
      expect(String(error)).not.toContain('topsecret')
    }
  })

  it('answers the challenge, and a wrong secret is refused by name', async () => {
    server.auth = { identity: 'desk', secret: 'the-secret' }
    const engine = await connected({ auth: { identity: 'desk', secret: 'the-secret' } })
    await engine.insert(ORDERBOOK_TABLE, row())
    expect(server.rows).toHaveLength(1)
    await engine.close()
    const wrong = new OrderbookEngine({ host: '127.0.0.1', port: server.port, auth: { identity: 'desk', secret: 'not-it' } })
    const refused = await wrong.connect().catch((error: unknown) => error)
    expect(String(refused)).toContain('Authentication failed')
    expect(String(refused)).not.toContain('not-it')
    // The digest is the reference's: five NUL-separated fields, HMAC-SHA256 keyed by the secret.
    const message = Buffer.from('ob-auth-v1\0client\0initiator\0desk\0abc')
    expect(authDigest('desk', 'k', 'abc')).toBe(createHmac('sha256', 'k').update(message).digest('hex'))
  })

  it('refuses a server that cannot store an event time, before any write', async () => {
    server.capabilities = 'strict_args'
    await expect(connected()).rejects.toThrow('cannot store a write\'s event time')
    expect(server.commands.filter((command) => command.startsWith('INSERT') || command.startsWith('MINSERT'))).toEqual([])
  })
})

describe('writing', () => {
  it('stores one level at level 0, without a sequence number of its own', async () => {
    const engine = await connected()
    await engine.insert(ORDERBOOK_TABLE, row())
    expect(server.commands.at(-1)).toBe('INSERT BTCUSDT binance bid 5000000 3 1 1000')
    await expect(engine.insert(ORDERBOOK_TABLE, row({ level: 1 }))).rejects.toThrow('declares level 1')
    await expect(engine.insert(ORDERBOOK_TABLE, row({ sequence_number: 41n }))).rejects.toThrow("sequence number is the server's")
    await expect(engine.insert(ORDERBOOK_TABLE, row({ __sde_write_epoch: 2 }))).rejects.toThrow('carries no write generation')
    await expect(engine.insert(ORDERBOOK_TABLE, row({ quantity: -1n }))).rejects.toThrow('quantity -1 is outside')
    await expect(engine.insert(ORDERBOOK_TABLE, row({ symbol: 'BTC USD' }))).rejects.toThrow('cannot be used as a symbol')
    await expect(engine.insert(ORDERBOOK_TABLE, row({ timestamp_ns: 1.5 }))).rejects.toThrow('must be an integer')
    await expect(engine.insert('another', row())).rejects.toThrow('has one table')
    expect(server.rows).toHaveLength(1)
    await engine.close()
  })

  it('writes a batch as whole updates, in round trips of 64', async () => {
    const engine = await connected()
    const rows = Array.from({ length: 130 }, (_, index) => row({ timestamp_ns: BigInt(index) }))
    rows.push(row({ timestamp_ns: 5n, level: 1, price: 4_999_900n }))
    await engine.insertMany(ORDERBOOK_TABLE, rows)
    const writes = server.commands.filter((command) => command.startsWith('INSERT') || command.startsWith('MINSERT'))
    expect(writes).toHaveLength(130)
    expect(writes[5]).toBe('MINSERT BTCUSDT binance bid 2 5\n5000000 3 1\n4999900 3 1')
    expect(server.rows).toHaveLength(131)
    await engine.close()
  })

  it('refuses a malformed update whole, before sending', async () => {
    const engine = await connected()
    await expect(engine.insertMany(ORDERBOOK_TABLE, [row(), row({ level: 2 })])).rejects.toThrow('levels [0,2]')
    await expect(engine.insertMany(ORDERBOOK_TABLE, [row(), row()])).rejects.toThrow('both level 0 of one update')
    await expect(engine.insertMany(ORDERBOOK_TABLE, [row({ side: 'mid' })])).rejects.toThrow('side must be one of')
    expect(server.rows).toHaveLength(0)
    await engine.close()
  })

  it('names what the server refused and how much was stored', async () => {
    const engine = await connected()
    server.refuse.add(65)
    const rows = Array.from({ length: 130 }, (_, index) => row({ timestamp_ns: BigInt(index) }))
    const refused = await engine.insertMany(ORDERBOOK_TABLE, rows).catch((error: unknown) => String(error))
    expect(refused).toContain('update 65 (rows [65])')
    expect(refused).toContain('127 updates were stored')
    expect(refused).toContain('not a transaction')
    await engine.close()
  })

  it('names a failed connection as an unknown outcome and closes the adapter', async () => {
    const engine = await connected()
    server.dropOnWrite = 70
    const rows = Array.from({ length: 130 }, (_, index) => row({ timestamp_ns: BigInt(index) }))
    const unknown = await engine.insertMany(ORDERBOOK_TABLE, rows).catch((error: unknown) => String(error))
    expect(unknown).toContain('the outcome of this batch is unknown')
    expect(unknown).toContain('64 of its 130 updates were confirmed')
    await expect(engine.insert(ORDERBOOK_TABLE, row())).rejects.toThrow('not connected')
  })
})

describe('reading', () => {
  async function book(engine: OrderbookEngine): Promise<void> {
    // Written out of event-time order, as a feed with corrections does.
    for (const stamp of [3_000n, 1_000n, 2_000n]) {
      for (const [side, base] of [['ask', 101n], ['bid', 99n]] as const) {
        server.store('BTCUSDT', 'binance', side === 'ask' ? 1 : 0, stamp, [[base + stamp, 1n, 1n], [base + stamp + 1n, 2n, 1n]])
      }
    }
    await engine.flush()
  }

  it('answers in key order whatever the arrival order, and pages with limit plus one', async () => {
    const engine = await connected()
    await book(engine)
    const rows = await engine.selectRows(ORDERBOOK_TABLE, plan({ where: BOOK, limit: 3 }))
    expect(rows.map((item) => [item['timestamp_ns'], item['side'], item['level']])).toEqual([
      [1_000n, 'ask', 0], [1_000n, 'ask', 1], [1_000n, 'bid', 0], [1_000n, 'bid', 1],
    ])
    expect(rows[0]).toEqual({
      symbol: 'BTCUSDT', exchange: 'binance', timestamp_ns: 1_000n, side: 'ask', level: 0,
      price: 1_101n, quantity: 1n, order_count: 1, sequence_number: 3n,
    })
    const next = await engine.selectRows(ORDERBOOK_TABLE, plan({
      where: BOOK, limit: 3, after: { ...BOOK, timestamp_ns: 1_000n, side: 'bid', level: 0 },
    }))
    expect(next.map((item) => [item['timestamp_ns'], item['side'], item['level']])[0]).toEqual([1_000n, 'bid', 1])
    const backwards = await engine.selectRows(ORDERBOOK_TABLE, plan({ where: BOOK, limit: 2, descending: true }))
    expect(backwards.map((item) => [item['timestamp_ns'], item['side'], item['level']])).toEqual([
      [3_000n, 'bid', 1], [3_000n, 'bid', 0], [3_000n, 'ask', 1],
    ])
    await engine.close()
  })

  it('filters where the engine can and refuses what it cannot, before any query', async () => {
    const engine = await connected()
    await book(engine)
    const bids = await engine.selectRows(ORDERBOOK_TABLE, plan({ where: { ...BOOK, side: 'bid', level: 1 }, limit: 10 }))
    expect(bids.map((item) => item['price'])).toEqual([1_100n, 2_100n, 3_100n])
    const ranged = await engine.selectRows(ORDERBOOK_TABLE, plan({ where: BOOK, bounds: { field: 'timestamp_ns', low: 2_000n, high: 3_000n }, limit: 10 }))
    expect(new Set(ranged.map((item) => item['timestamp_ns']))).toEqual(new Set([2_000n]))
    const before = server.commands.length
    for (const [options, fragment] of [
      [{ where: { symbol: 'BTCUSDT' } }, 'names one book'],
      [{ where: { ...BOOK, quantity: 1n } }, 'cannot filter on ["quantity"]'],
      [{ where: BOOK, bounds: { field: 'level', low: 0, high: 3 } }, 'no range over ["level"]'],
      [{ where: BOOK, orderBy: 'price' }, 'time order'],
    ] as const) {
      await expect(engine.selectRows(ORDERBOOK_TABLE, plan({ ...options, limit: 1 })), fragment).rejects.toThrow(QueryRefused)
    }
    expect(server.commands.length).toBe(before)
    await engine.close()
  })

  it('reads a book nobody wrote to as empty, and asks nothing for a read that matches nothing', async () => {
    const engine = await connected()
    expect(await engine.selectRows(ORDERBOOK_TABLE, plan({ where: { symbol: 'NEW', exchange: 'X' }, limit: 5 }))).toEqual([])
    expect(await engine.get(ORDERBOOK_TABLE, { symbol: 'NEW', exchange: 'X', timestamp_ns: 1n, side: 'bid', level: 0 })).toBeNull()
    const before = server.commands.length
    expect(await engine.selectRows(ORDERBOOK_TABLE, plan({ where: { ...BOOK, side: 'mid' }, limit: 5 }))).toEqual([])
    expect(server.commands.length).toBe(before)
    await engine.close()
  })

  it('answers a key no row can have without asking: a negative time, a side that is neither', async () => {
    const engine = await connected()
    const before = server.commands.length
    expect(await engine.get(ORDERBOOK_TABLE, { ...BOOK, timestamp_ns: -1n, side: 'bid', level: 0 })).toBeNull()
    expect(await engine.get(ORDERBOOK_TABLE, { ...BOOK, timestamp_ns: 1n, side: 'mid', level: 0 })).toBeNull()
    expect(server.commands.length).toBe(before)
    await engine.close()
  })

  it('refuses two rows with one key, in a page and in get', async () => {
    const engine = await connected()
    server.store('BTCUSDT', 'binance', 0, 7n, [[1n, 1n, 1n]])
    server.store('BTCUSDT', 'binance', 0, 7n, [[2n, 1n, 1n]])
    await expect(engine.get(ORDERBOOK_TABLE, { ...BOOK, timestamp_ns: 7n, side: 'bid', level: 0 })).rejects.toThrow('2 rows in orderbook share the key')
    await expect(engine.selectRows(ORDERBOOK_TABLE, plan({ where: BOOK, limit: 5 }))).rejects.toThrow('share the key')
    await engine.close()
  })

  // The engine stores unsigned 64-bit values and the model's int64 cannot hold all of them. Measured:
  // an engine before c1f14c0 read a stored quantity of 2^60 - 1 back as 2^64 - 1, and this adapter
  // returned it. No write of this library stores such a value.
  it('refuses a row outside the model\'s types on read, and reads the edges of those types', async () => {
    const engine = await connected()
    const good = { exchange: 'binance', timestamp: 1_000n, side: 0 as const, level: 0, price: 10n, quantity: 5n, count: 1n, sequence: 1n }
    const cases: [string, Partial<StoredRow>][] = [
      ['quantity', { quantity: (1n << 64n) - 1n }],
      ['quantity', { quantity: 1n << 63n }],
      ['timestamp_ns', { timestamp: 1n << 63n }],
      ['order_count', { count: 1n << 31n }],
      ['level', { level: 1000 }],
      ['sequence_number', { sequence: 1n << 63n }],
    ]
    for (const [index, [field, change]] of cases.entries()) {
      const symbol = `BAD${index}`
      server.rows.push({ ...good, ...change, symbol })
      const refused = await engine.selectRows(ORDERBOOK_TABLE, plan({ where: { symbol, exchange: 'binance' }, limit: 5 }))
        .then(() => null, (error: unknown) => error as Error)
      expect(refused?.message, field).toContain(`whose ${field} is outside`)
      const value = String(Object.values(change)[0])
      expect(refused?.message, 'a value read back is data and stays out').not.toContain(value)
    }
    // The control: the largest value of each type is a value, and the engine's 0 is an unknown number.
    server.rows.push({ symbol: 'EDGE', exchange: 'binance', timestamp: (1n << 63n) - 1n, side: 0, level: 999, price: -(1n << 63n), quantity: (1n << 63n) - 1n, count: (1n << 31n) - 1n, sequence: 0n })
    const rows = await engine.selectRows(ORDERBOOK_TABLE, plan({ where: { symbol: 'EDGE', exchange: 'binance' }, limit: 5 }))
    expect(rows.map((row) => [row['quantity'], row['order_count'], row['level'], row['sequence_number']])).toEqual([
      [(1n << 63n) - 1n, 2147483647, 999, null],
    ])
    await engine.close()
  })

  it('splits a window too large and refuses one instant that holds more than the cap', async () => {
    OrderbookEngine.limits = { chunkRows: 4, firstWindowNs: 1_000n }
    const engine = await connected()
    for (let stamp = 0n; stamp < 6n; stamp += 1n) server.store('BTCUSDT', 'binance', 0, stamp, [[stamp, 1n, 1n]])
    const rows = await engine.selectRows(ORDERBOOK_TABLE, plan({ where: BOOK, limit: 10 }))
    expect(rows.map((item) => item['timestamp_ns'])).toEqual([0n, 1n, 2n, 3n, 4n, 5n])
    for (let index = 0; index < 5; index += 1) server.store('ETHUSDT', 'binance', 0, 9n, [[1n, 1n, 1n]])
    await expect(engine.selectRows(ORDERBOOK_TABLE, plan({ where: { symbol: 'ETHUSDT', exchange: 'binance' }, limit: 10 })))
      .rejects.toThrow('more than 4 rows at the instant 9')
    await engine.close()
  })

  it('has no transactions', async () => {
    const engine = await connected()
    await expect(engine.transaction(async () => 1)).rejects.toThrow('no multi-statement transactions')
    await engine.close()
  })
})

describe('a session over the orderbook', () => {
  it('writes updates, pages in key order, and refuses count and summary by name before timing them', async () => {
    const logical = model()
    const placement = loadMap({
      contract: 3,
      model_version: logical.version,
      map_version: 1,
      groups: {
        DepthLevel: {
          source: {
            id: 'source', engine: 'ob',
            layout: {
              tables: { DepthLevel: ORDERBOOK_TABLE },
              columns: { DepthLevel: Object.fromEntries(Object.entries(ORDERBOOK_SHAPE).map(([name, type]) => [name, type])) },
            },
          },
        },
      },
    }, { model: logical })
    const engine = await connected()
    const recorder = new Recorder(logical.version)
    const session = await Session.open(logical, placement, { ob: engine }, { recorder })
    await session.saveMany('DepthLevel', [row({ timestamp_ns: 2n }), row({ timestamp_ns: 1n }), row({ timestamp_ns: 1n, level: 1, price: 1n })])
    const page = await session.scan('DepthLevel', { where: BOOK, limit: 2 })
    expect(page.rows.map((item) => [item['timestamp_ns'], item['level']])).toEqual([[1n, 0], [1n, 1]])
    await expect(session.count('DepthLevel', { where: BOOK })).rejects.toThrow('counts nothing over its history')
    await expect(session.summarize('DepthLevel', 'price', { where: BOOK })).rejects.toThrow('summarizes nothing over its history')
    // Refused before they were timed: the window holds the write and the scan, and no failure.
    const window = recorder.roll()!
    expect(window.shapes.map((shape) => [shape.kind, shape.errors]).sort()).toEqual([['bulk_write', 0], ['full_scan', 0]])
    await session.close()
    await engine.close()
  })
})

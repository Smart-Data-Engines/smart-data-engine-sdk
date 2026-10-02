/**
 * The orderbook adapter against a real `ob_tcp_server`, and against the reference library.
 *
 * The engine's measured behaviour again, from this language: the server numbers updates, answers
 * in arrival order, keeps two rows with one key, and says when a book is unknown; with credentials
 * and TLS, a wrong secret is refused as such. And the same book, written by one library and read
 * by the other, in both directions. Runs where an engine is - the SDK's `orderbook` CI job, which
 * fails if anything here was skipped - with `SDE_ORDERBOOK_TCP=host:port`, and
 * `SDE_ORDERBOOK_SECURE_DSN` for the server with `--auth-secret-file` and `--tls-client`.
 */

import { execFileSync } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import { loadMap, Session } from '../src/index.js'
import { OrderbookEngine, ORDERBOOK_KEY, ORDERBOOK_SHAPE, ORDERBOOK_TABLE } from '../src/engines/orderbook.js'
import { modelFromNeutral } from '../src/testing/loader.js'

const TCP = process.env['SDE_ORDERBOOK_TCP']
const SECURE = process.env['SDE_ORDERBOOK_SECURE_DSN']
const PYTHON = process.env['SDE_PYTHON'] ?? resolve('../python/.venv/bin/python')
const EXCHANGE = 'binance'
const T0 = 1_790_000_000_000_000_000n

function engine(): OrderbookEngine {
  const [host, port] = TCP!.split(':')
  return new OrderbookEngine({ host: host!, port: Number(port), timeoutMs: 10_000 })
}

function book(): string {
  return 'T' + randomBytes(6).toString('hex').toUpperCase()
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

async function session(adapter: OrderbookEngine): Promise<Session> {
  const logical = model()
  const placement = loadMap({
    contract: 3,
    model_version: logical.version,
    map_version: 1,
    groups: {
      DepthLevel: {
        source: {
          id: 'source',
          engine: 'ob',
          layout: { tables: { DepthLevel: ORDERBOOK_TABLE }, columns: { DepthLevel: { ...ORDERBOOK_SHAPE } } },
        },
      },
    },
  }, { model: logical })
  return Session.open(logical, placement, { ob: adapter })
}

function level(symbol: string, stamp: bigint, side: 'ask' | 'bid', position: number, price: bigint): Record<string, unknown> {
  return {
    symbol, exchange: EXCHANGE, timestamp_ns: T0 + stamp, side, level: position,
    price, quantity: 5n + BigInt(position), order_count: 1 + position, sequence_number: null,
  }
}

function strings(row: Readonly<Record<string, unknown>>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(row).map(([key, value]) => [key, typeof value === 'bigint' || typeof value === 'number' ? String(value) : value]))
}

describe.skipIf(TCP === undefined)('the orderbook engine, from TypeScript', () => {
  it('numbers updates itself and answers a page in key order although it stores arrival order', async () => {
    const adapter = engine()
    await adapter.connect()
    try {
      const symbol = book()
      const current = await session(adapter)
      const rows = [3_000n, 1_000n, 2_000n].flatMap((stamp) => [
        level(symbol, stamp, 'bid', 0, 9_900n + stamp), level(symbol, stamp, 'bid', 1, 9_899n + stamp),
      ])
      await current.saveMany('DepthLevel', rows)
      const page = await current.scan('DepthLevel', { where: { symbol, exchange: EXCHANGE }, limit: 10 })
      expect(page.rows.map((row) => [row['timestamp_ns'], row['level']])).toEqual([
        [T0 + 1_000n, 0], [T0 + 1_000n, 1], [T0 + 2_000n, 0], [T0 + 2_000n, 1], [T0 + 3_000n, 0], [T0 + 3_000n, 1],
      ])
      // The server's numbering: one per update, per book, in arrival order.
      expect(page.rows.map((row) => row['sequence_number'])).toEqual([2n, 2n, 3n, 3n, 1n, 1n])
      await expect(current.save('DepthLevel', level(symbol, 9n, 'bid', 0, 1n))).resolves.toBeUndefined()
      await expect(current.save('DepthLevel', { ...level(symbol, 9n, 'bid', 0, 1n), sequence_number: 7n }))
        .rejects.toThrow("sequence number is the server's")
      await expect(current.count('DepthLevel', { where: { symbol, exchange: EXCHANGE } })).rejects.toThrow('counts nothing over its history')
      await current.close()
    } finally {
      await adapter.close()
    }
  })

  it('keeps two rows with one key and refuses to pick one; an unknown book is empty', async () => {
    const adapter = engine()
    await adapter.connect()
    try {
      const symbol = book()
      await adapter.insert(ORDERBOOK_TABLE, level(symbol, 5n, 'ask', 0, 10n))
      await adapter.insert(ORDERBOOK_TABLE, level(symbol, 5n, 'ask', 0, 11n))
      await expect(adapter.get(ORDERBOOK_TABLE, { symbol, exchange: EXCHANGE, timestamp_ns: T0 + 5n, side: 'ask', level: 0 }))
        .rejects.toThrow('2 rows in orderbook share the key')
      expect(await adapter.get(ORDERBOOK_TABLE, { symbol: book(), exchange: EXCHANGE, timestamp_ns: T0, side: 'ask', level: 0 })).toBeNull()
    } finally {
      await adapter.close()
    }
  })

  it('reads what the reference library wrote, and the reference reads what this one wrote', async () => {
    const fromPython = book()
    execFileSync(PYTHON, [resolve('../python/tests/orderbook_peer.py'), 'write', TCP!, fromPython], { encoding: 'utf8' })
    const adapter = engine()
    await adapter.connect()
    try {
      const current = await session(adapter)
      const read = await current.scan('DepthLevel', { where: { symbol: fromPython, exchange: EXCHANGE }, limit: 1000 })
      expect(read.rows).toHaveLength(12)
      expect(read.rows[0]).toEqual({
        symbol: fromPython, exchange: EXCHANGE, timestamp_ns: T0 + 1_000n, side: 'ask', level: 0,
        price: 101n * 100n + 1_000n, quantity: 5n, order_count: 1, sequence_number: 3n,
      })
      const fromTypeScript = book()
      const written = [3_000n, 1_000n, 2_000n].flatMap((stamp) => (['ask', 'bid'] as const).flatMap((side) =>
        [0, 1].map((position) => level(fromTypeScript, stamp, side, position, (side === 'ask' ? 10_100n : 9_900n) + stamp + BigInt(position)))))
      await current.saveMany('DepthLevel', written)
      await adapter.flush()
      const output = execFileSync(PYTHON, [resolve('../python/tests/orderbook_peer.py'), 'read', TCP!, fromTypeScript], { encoding: 'utf8' })
      const seen = JSON.parse(output) as Record<string, unknown>[]
      const mine = await current.scan('DepthLevel', { where: { symbol: fromTypeScript, exchange: EXCHANGE }, limit: 1000 })
      expect(seen).toEqual(mine.rows.map(strings))
      expect(seen).toHaveLength(12)
      await current.close()
    } finally {
      await adapter.close()
    }
  })
})

describe.skipIf(SECURE === undefined)('the orderbook engine with credentials and TLS, from TypeScript', () => {
  it('writes and reads through an authenticated TLS connection', async () => {
    const adapter = OrderbookEngine.fromDsn(SECURE!)
    await adapter.connect()
    try {
      const symbol = book()
      await adapter.insert(ORDERBOOK_TABLE, level(symbol, 1n, 'bid', 0, 100n))
      expect(await adapter.get(ORDERBOOK_TABLE, { symbol, exchange: EXCHANGE, timestamp_ns: T0 + 1n, side: 'bid', level: 0 }))
        .toMatchObject({ price: 100n, sequence_number: 1n })
    } finally {
      await adapter.close()
    }
  })

  it('refuses a wrong secret as an authentication failure, and plain text at the TLS port', async () => {
    const parts = new URL(SECURE!)
    parts.password = 'not-the-secret'
    const wrong = OrderbookEngine.fromDsn(parts.toString())
    const refused = await wrong.connect().catch((error: unknown) => String(error))
    expect(refused).toContain('Authentication failed')
    expect(refused).not.toContain('not-the-secret')
    const plain = new URL(SECURE!)
    plain.search = '?timeout=2'
    const unencrypted = OrderbookEngine.fromDsn(plain.toString())
    await expect(unencrypted.connect()).rejects.toThrow(/within 2000 ms/)
  })
})

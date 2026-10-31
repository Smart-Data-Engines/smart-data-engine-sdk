/**
 * A book stream against a real `ob_tcp_server`, from TypeScript: the book the engine composes from
 * what a stream sent is the book the application gave it, in both stream modes.
 *
 * The application is a seeded walk of whole sides and of changes, and the oracle is the walk's own
 * book, not the stream's. After every step the engine is asked for the book as of that step's time
 * (`Session.bookAt`), which it composes by price from history. The reference library's test does
 * the same in both of its engine modes and also reads the live book; this one holds the second
 * library to it. Runs where `SDE_ORDERBOOK_TCP=host:port` names a server, as the `orderbook` CI job
 * does.
 */

import { randomBytes } from 'node:crypto'

import { describe, expect, it } from 'vitest'

import { loadMap, Resync, Session, type Level } from '../src/index.js'
import { OrderbookEngine, ORDERBOOK_KEY, ORDERBOOK_SHAPE, ORDERBOOK_TABLE } from '../src/engines/orderbook.js'
import { modelFromNeutral } from '../src/testing/loader.js'

const TCP = process.env['SDE_ORDERBOOK_TCP']
const EXCHANGE = 'binance'
const T0 = 1_790_000_000_000_000_000n
const SIDES = ['bid', 'ask'] as const
type Side = (typeof SIDES)[number]
// Each side walks inside its own band of prices, so a side holds at most sixty.
const BANDS: Record<Side, readonly [number, number]> = { bid: [940, 1000], ask: [1000, 1060] }
type Book = Map<number, readonly [number, number]>

function engine(): OrderbookEngine {
  const [host, port] = TCP!.split(':')
  return new OrderbookEngine({ host: host!, port: Number(port), timeoutMs: 10_000 })
}

function symbol(): string {
  return 'T' + randomBytes(6).toString('hex').toUpperCase()
}

async function session(adapter: OrderbookEngine): Promise<Session> {
  const logical = modelFromNeutral({
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

/** A seeded generator (mulberry32): the same walk on every run. */
function generator(seed: number): () => number {
  let state = seed >>> 0
  return () => {
    state = (state + 0x6d2b79f5) >>> 0
    let t = state
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function between(random: () => number, low: number, high: number): number {
  return low + Math.floor(random() * (high - low))
}

function levels(side: Side, book: Book): [number, number, number][] {
  const prices = [...book.keys()].sort((a, b) => (side === 'bid' ? b - a : a - b))
  return prices.map((price) => [price, book.get(price)![0], book.get(price)![1]])
}

function plain(read: readonly Level[]): [number, number, number][] {
  return read.map(([price, quantity, count]) => [Number(price), Number(quantity), Number(count)])
}

describe.skipIf(TCP === undefined)('a book stream, from TypeScript', () => {
  for (const mode of ['full', 'incremental'] as const) {
    it(`composes the book the application gave, ${mode}`, async () => {
      const adapter = engine()
      await adapter.connect()
      try {
        const current = await session(adapter)
        const name = symbol()
        const stream = await current.bookStream('DepthLevel', {
          symbol: name, exchange: EXCHANGE, mode, resync: new Resync({ updates: 9, seconds: 0 }),
        })
        const random = generator(mode === 'full' ? 233 : 235)
        const truth: Record<Side, Book> = { bid: new Map(), ask: new Map() }
        let at = T0
        for (let step = 0; step < 120; step += 1) {
          at += 1_000n
          const side: Side = step < 2 ? SIDES[step]! : SIDES[between(random, 0, 2)]!
          const [low, high] = BANDS[side]
          if (mode === 'incremental' && step >= 2 && random() < 0.5) {
            const changes: [number, number, number][] = []
            const picked = new Set<number>()
            for (let k = between(random, 1, 9); k > 0; k -= 1) picked.add(between(random, low, high))
            for (const price of picked) {
              const held = truth[side].has(price)
              if ((held && random() < 0.5) || (!held && random() < 0.2)) {
                changes.push([price, 0, 0])
                truth[side].delete(price)
              } else {
                const value = [between(random, 1, 51), between(random, 1, 6)] as const
                changes.push([price, value[0], value[1]])
                truth[side].set(price, value)
              }
            }
            await stream.apply(side, changes, { timestampNs: at })
          } else {
            const next: Book = new Map([...truth[side]].filter(() => random() > 0.2))
            for (let k = between(random, 1, 7); k > 0; k -= 1) {
              next.set(between(random, low, high), [between(random, 1, 51), between(random, 1, 6)])
            }
            truth[side] = next
            await stream.update(side, levels(side, next), { timestampNs: at })
          }
          const read = await current.bookAt('DepthLevel', { symbol: name, exchange: EXCHANGE, atNs: at })
          expect({ bid: plain(read.bid), ask: plain(read.ask) }, `step ${step}`).toEqual({
            bid: levels('bid', truth.bid), ask: levels('ask', truth.ask),
          })
        }
        if (mode === 'incremental') expect(stream.stats.changes > 0 && stream.stats.whole > 0).toBe(true)
        await current.close()
      } finally {
        await adapter.close()
      }
    })
  }

  it('reads an earlier book as it was', async () => {
    const adapter = engine()
    await adapter.connect()
    try {
      const current = await session(adapter)
      const name = symbol()
      const stream = await current.bookStream('DepthLevel', { symbol: name, exchange: EXCHANGE })
      await stream.update('ask', [[1000, 5, 1], [1001, 3, 1], [1002, 2, 1]], { timestampNs: T0 + 10n })
      await stream.apply('ask', [[1001, 0, 0], [1003, 7, 2]], { timestampNs: T0 + 20n })
      await stream.update('ask', [[1000, 5, 1], [1003, 8, 2]], { timestampNs: T0 + 30n })
      const read = async (at: bigint) => plain((await current.bookAt('DepthLevel', { symbol: name, exchange: EXCHANGE, atNs: at })).ask)
      expect(await read(T0 + 10n)).toEqual([[1000, 5, 1], [1001, 3, 1], [1002, 2, 1]])
      expect(await read(T0 + 25n)).toEqual([[1000, 5, 1], [1002, 2, 1], [1003, 7, 2]])
      expect(await read(T0 + 30n)).toEqual([[1000, 5, 1], [1003, 8, 2]])
      expect([stream.stats.whole, stream.stats.changes]).toEqual([1, 2])
      await current.close()
    } finally {
      await adapter.close()
    }
  })
})

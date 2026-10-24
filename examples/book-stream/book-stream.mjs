#!/usr/bin/env node
/**
 * One order book sent to the orderbook engine whole and as its changes, and read back by price - the
 * TypeScript half of `book_stream.py`, which says what it does. The feed is the same walk, from the
 * same seed, so the two halves send the same updates and print the same counts.
 *
 *   BOOK_STREAM_OB_DSN=orderbook://host:port node book-stream.mjs [--steps 600] [--depth 20]
 */

import { readFileSync } from 'node:fs'
import { argv, env, exit } from 'node:process'
import { randomBytes } from 'node:crypto'

import { assemble, loadMap, Session } from '@smart-data-engines/sde'
import { OrderbookEngine, ORDERBOOK_SHAPE, ORDERBOOK_TABLE } from '@smart-data-engines/sde/engines/orderbook'

const HERE = new URL('.', import.meta.url)
const EXCHANGE = 'SDE'
const T0 = 1_790_000_000_000_000_000n
const STEP_NS = 1_000_000n
const READ_EVERY = 10

function options(args) {
  const out = { steps: 600, depth: 20 }
  for (let i = 0; i < args.length; i += 2) {
    const name = args[i].replace(/^--/, '')
    if (!(name in out)) throw new Error(`unknown option ${args[i]}`)
    out[name] = Number(args[i + 1])
  }
  return out
}

/** The model in model.json, the declaration the Python half reads: one model version for both. */
function declared(data) {
  return assemble(
    data.entities.map((entity) => ({
      name: entity.name,
      fields: entity.fields.map((field) => ({ name: field.name, type: field.type, nullable: field.nullable === true })),
      key: entity.key,
      pii: entity.pii ?? [],
      residency: entity.residency ?? null,
    })),
    (data.relations ?? []).map((relation) => ({ name: relation.name, source: relation.from, target: relation.to })),
    data.atomic ?? [],
    data.cost_ceiling ?? null,
  )
}

function bestFirst(side, book) {
  const prices = [...book.keys()].sort((a, b) => (side === 'bid' ? b - a : a - b))
  return prices.map((price) => [price, ...book.get(price)])
}

/** The exchange: a walk of both sides, the same as the Python half's. */
class Feed {
  constructor(depth) {
    this.depth = depth
    this.state = 2_830_000_000
    this.mid = 100_000
    this.book = { bid: new Map(), ask: new Map() }
  }

  /** mulberry32: the same numbers as the Python half. */
  random() {
    this.state = (this.state + 0x6d2b79f5) >>> 0
    let t = this.state
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }

  between(low, high) {
    return low + Math.floor(this.random() * (high - low))
  }

  step(number) {
    const side = number % 2 === 0 ? 'bid' : 'ask'
    this.mid += this.between(-2, 3)
    const sign = side === 'bid' ? -1 : 1
    const near = this.mid + sign
    if (number < 2 || this.random() < 0.2) {
      const book = new Map()
      const count = this.between(Math.floor(this.depth / 2), this.depth + 1)
      for (let k = 0; k < count; k += 1) {
        book.set(near + sign * k, [this.between(1, 50), this.between(1, 6)])
      }
      this.book[side] = book
      return [side, 'snapshot', bestFirst(side, book)]
    }
    const changes = []
    for (const price of [...this.book[side].keys()].sort((a, b) => a - b)) {
      if (side === 'bid' ? price > this.mid : price < this.mid) {
        changes.push([price, 0, 0])
        this.book[side].delete(price)
      }
    }
    for (let n = this.between(1, 6); n > 0; n -= 1) {
      const price = near + sign * this.between(0, this.depth)
      if (changes.some((c) => c[0] === price)) continue
      if (this.book[side].has(price) && this.random() < 0.4) {
        changes.push([price, 0, 0])
        this.book[side].delete(price)
      } else {
        const value = [this.between(1, 50), this.between(1, 6)]
        changes.push([price, ...value])
        this.book[side].set(price, value)
      }
    }
    return [side, 'changes', changes]
  }
}

async function main() {
  const { steps, depth } = options(argv.slice(2))
  const dsn = env.BOOK_STREAM_OB_DSN
  if (!dsn) {
    console.error('set BOOK_STREAM_OB_DSN to orderbook://host:port')
    return 2
  }
  const model = declared(JSON.parse(readFileSync(new URL('model.json', HERE), 'utf8')))
  const placement = loadMap({
    contract: 3,
    model_version: model.version,
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
  }, { model })
  const engine = OrderbookEngine.fromDsn(dsn)
  await engine.connect()
  const session = await Session.open(model, placement, { ob: engine })
  const run = randomBytes(4).toString('hex').toUpperCase()
  const books = { full: `BSF${run}`, incremental: `BSI${run}` }
  const streams = {}
  for (const [mode, symbol] of Object.entries(books)) {
    streams[mode] = await session.bookStream('DepthLevel', { symbol, exchange: EXCHANGE, mode })
  }
  const feed = new Feed(depth)
  const truth = new Map()
  const mismatches = []
  try {
    for (let number = 0; number < steps; number += 1) {
      const at = T0 + BigInt(number + 1) * STEP_NS
      const [side, given, levels] = feed.step(number)
      await streams.full.update(side, bestFirst(side, feed.book[side]), { timestampNs: at })
      if (given === 'snapshot') {
        await streams.incremental.update(side, levels, { timestampNs: at })
      } else {
        await streams.incremental.apply(side, levels, { timestampNs: at })
      }
      if (number % READ_EVERY === READ_EVERY - 1 || number === steps - 1) {
        truth.set(at, { bid: bestFirst('bid', feed.book.bid), ask: bestFirst('ask', feed.book.ask) })
      }
    }
    const plain = (levels) => levels.map((level) => level.map(Number))
    for (const [at, expected] of truth) {
      for (const [mode, symbol] of Object.entries(books)) {
        const got = await session.bookAt('DepthLevel', { symbol, exchange: EXCHANGE, atNs: at })
        if (JSON.stringify({ bid: plain(got.bid), ask: plain(got.ask) }) !== JSON.stringify(expected)) {
          mismatches.push(`${mode} at ${at}`)
        }
      }
    }
  } finally {
    await session.close()
    await engine.close()
  }
  const stats = Object.fromEntries(Object.entries(streams).map(([mode, stream]) => [mode, {
    updates: stream.stats.updates,
    sides_whole: stream.stats.whole,
    updates_of_changes: stream.stats.changes,
    unchanged: stream.stats.unchanged,
    levels_sent: stream.stats.levelsWhole + stream.stats.levelsChanges,
  }]))
  console.log(JSON.stringify({ steps, books_read: truth.size * Object.keys(books).length, mismatches, streams: stats }, null, 2))
  return mismatches.length === 0 ? 0 : 1
}

exit(await main())

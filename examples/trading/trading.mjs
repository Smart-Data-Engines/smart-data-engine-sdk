#!/usr/bin/env node
/**
 * The trading application's TypeScript half: orders, fills and market trades, and a read of the
 * book depth the Python half wrote. The same signed map, the same engines, its own connections.
 *
 *   node trading.mjs run    --map MAP --keys KEYS --project ID --engines ENGINES --run RUN [--updates 200] [--books 4]
 *   node trading.mjs verify --map MAP --keys KEYS --project ID --engines ENGINES --run RUN [--updates 200] [--books 4]
 *
 * With `--depth-run RUN2`, `verify` also pages through the depth of every book of the Python run RUN2
 * and checks the count of rows, so one library reads what the other wrote. The arguments and the
 * engines file are `trading.py`'s.
 */

import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { argv, env, exit } from 'node:process'

import { assemble, loadMap, Session, Timestamp } from '@smart-data-engines/sde'
import { ClickHouseEngine } from '@smart-data-engines/sde/engines/clickhouse'
import { OrderbookEngine } from '@smart-data-engines/sde/engines/orderbook'
import { PostgresEngine } from '@smart-data-engines/sde/engines/postgres'

const HERE = new URL('.', import.meta.url)
const EXCHANGE = 'sde'
const T0 = 1_790_000_000_000_000_000n
const ORDER_EVERY = 10
const LEVELS = 5

function options(words) {
  const [command, ...rest] = words
  const out = { command, updates: 200, books: 4 }
  for (let index = 0; index < rest.length; index += 2) {
    const key = rest[index].replace(/^--/, '').replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())
    out[key] = ['updates', 'books'].includes(key) ? Number(rest[index + 1]) : rest[index + 1]
  }
  for (const required of ['map', 'keys', 'project', 'engines', 'run']) {
    if (out[required] === undefined) throw new Error(`--${required} is required`)
  }
  if (!/^[0-9a-f]{32}$/.test(out.run)) throw new Error('--run is 32 lowercase hex digits')
  return out
}

function adapter(dsn) {
  if (dsn.startsWith('postgresql://') || dsn.startsWith('postgres://')) return new PostgresEngine(dsn)
  if (dsn.startsWith('clickhouse://') || dsn.startsWith('clickhouses://')) return new ClickHouseEngine(dsn)
  if (dsn.startsWith('orderbook://')) return OrderbookEngine.fromDsn(dsn)
  throw new Error('a DSN starts with postgresql://, clickhouse:// or orderbook://')
}

/** A UUID derived from the run and a name, so `verify` regenerates what `run` wrote. */
function derived(run, name) {
  const hex = createHash('sha256').update(`${run}:${name}`).digest('hex').slice(0, 32)
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-4${hex.slice(13, 16)}-8${hex.slice(17, 20)}-${hex.slice(20, 32)}`
}

class Traffic {
  constructor(run, books, updates) {
    this.run = run
    this.books = books
    this.updates = updates
    // T, not S: the Python half's books start with S, so the two never write one book.
    this.symbols = Array.from({ length: books }, (_, book) => `T${run.slice(0, 10).toUpperCase()}${book}`)
  }

  trade(update, book) {
    return {
      symbol: this.symbols[book], exchange: EXCHANGE, trade_id: BigInt(update),
      price: 1_000_000n + BigInt((update % 50) * 10), quantity: BigInt(1 + (update % 9)),
      at_ns: T0 + BigInt(update) * 1_000_000n + BigInt(book) + 500n,
    }
  }

  order(update) {
    const id = derived(this.run, `order-${update}`)
    const micros = BigInt(Date.UTC(2026, 9, 2)) * 1000n + BigInt(update)
    const price = `${100 + (update % 50)}.12345678`
    const order = {
      id, account: `acct-${update % 3}`, symbol: this.symbols[update % this.books],
      side: update % 2 === 0 ? 'buy' : 'sell', qty: `${1 + (update % 5)}.50000000`, price,
      placed_at: micros,
    }
    const fills = [0, 1].map((part) => ({
      id: derived(this.run, `fill-${update}-${part}`), order_id: id, qty: '0.75000000', price,
      at: micros + BigInt(part + 1),
    }))
    return [order, fills]
  }
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

async function open(config) {
  const model = declared(JSON.parse(readFileSync(new URL('model.json', HERE), 'utf8')))
  const keys = Object.fromEntries(Object.entries(JSON.parse(readFileSync(config.keys, 'utf8')))
    .map(([name, value]) => [name, Buffer.from(value, 'base64')]))
  const placement = loadMap(JSON.parse(readFileSync(config.map, 'utf8')), { model, publicKey: keys, requireSignature: true })
  const entries = JSON.parse(readFileSync(config.engines, 'utf8'))
  const needed = [...new Set(Object.values(placement.groups).flatMap((group) => [group.source, ...group.derived].map((m) => m.engine)))].sort()
  const engines = {}
  for (const name of needed) {
    const variable = entries[name].runtime_env
    if (!env[variable]) throw new Error(`${name}: set ${variable} to its runtime DSN`)
    engines[name] = adapter(env[variable])
    await engines[name].connect()
  }
  return { model, placement, engines }
}

/** A Timestamp field as microseconds, whatever the engine handed back. */
function micros(value) {
  return typeof value === 'bigint' ? value : value.epochMicroseconds
}

async function main() {
  const config = options(argv.slice(2))
  const { model, placement, engines } = await open(config)
  const session = await Session.open(model, placement, engines, { projectId: config.project })
  const traffic = new Traffic(config.run, config.books, config.updates)
  const mismatches = []
  const counts = { trades: 0, orders: 0, fills: 0, depth: 0 }
  try {
    if (config.command === 'run') {
      for (let update = 0; update < traffic.updates; update += 1) {
        await session.saveMany('MarketTrade', traffic.symbols.map((_, book) => traffic.trade(update, book)))
        if (update % ORDER_EVERY === 0) {
          const [order, fills] = traffic.order(update)
          await session.transaction(['Order', 'Fill'], async (inside) => {
            await inside.save('Order', { ...order, placed_at: Timestamp.fromEpochMicroseconds(order.placed_at) })
            for (const fill of fills) await inside.save('Fill', { ...fill, at: Timestamp.fromEpochMicroseconds(fill.at) })
          })
        }
      }
    } else {
      for (let book = 0; book < traffic.books; book += 1) {
        const where = { symbol: traffic.symbols[book], exchange: EXCHANGE }
        const page = await session.scan('MarketTrade', { where, limit: 1000 })
        const expected = Array.from({ length: traffic.updates }, (_, update) => traffic.trade(update, book))
        if (same(page.rows, expected) === false) mismatches.push(`${where.symbol}: trades differ`)
        counts.trades += page.rows.length
      }
      for (let update = 0; update < traffic.updates; update += ORDER_EVERY) {
        const [order, fills] = traffic.order(update)
        const stored = await session.get('Order', { id: order.id })
        if (stored === null || micros(stored.placed_at) !== order.placed_at || stored.price !== order.price) {
          mismatches.push(`order ${update} differs`)
        }
        counts.orders += 1
        for (const fill of fills) {
          const got = await session.get('Fill', { id: fill.id })
          if (got === null || got.order_id !== fill.order_id || got.qty !== fill.qty) mismatches.push(`fill ${fill.id} differs`)
          counts.fills += 1
        }
      }
      if (config.depthRun !== undefined) {
        const symbols = Array.from({ length: config.books }, (_, book) => `S${config.depthRun.slice(0, 10).toUpperCase()}${book}`)
        for (const symbol of symbols) {
          let after = null
          let rows = 0
          for (;;) {
            const page = await session.scan('DepthLevel', { where: { symbol, exchange: EXCHANGE }, limit: 1000, after })
            rows += page.rows.length
            if (page.nextAfter === null) break
            after = page.nextAfter
          }
          if (rows !== config.updates * LEVELS) mismatches.push(`${symbol}: ${rows} depth rows, expected ${config.updates * LEVELS}`)
          counts.depth += rows
        }
      }
    }
  } finally {
    await session.close()
    for (const engine of Object.values(engines)) await engine.close()
  }
  console.log(JSON.stringify({ run: traffic.run, command: config.command, verified: counts, mismatches }))
  return mismatches.length === 0 ? 0 : 1
}

/** Two lists of rows with the same fields and values, in order, whatever order the keys are in. */
function same(left, right) {
  const canonical = (rows) => JSON.stringify(rows.map((row) => Object.fromEntries(Object.entries(row)
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([key, value]) => [key, typeof value === 'bigint' ? String(value) : value]))))
  return canonical(left) === canonical(right)
}

main().then((code) => exit(code), (error) => {
  console.error(error instanceof Error ? error.message : String(error))
  exit(2)
})

/**
 * The library's own scans use the engines' indexes - asked of each engine's planner.
 *
 * Found in the general test on 2 October 2026: a B-tree an agent decided and the operator built
 * served none of the reads it was decided for. PostgreSQL text columns, and every index on them,
 * had the default collation while the reads compare `(col COLLATE "C")`; a ClickHouse uuid equality
 * was `toString(col) = ...`, which the primary key cannot prune. `test_index_use_live.py` holds the
 * same claims for the reference library.
 *
 * Each case runs the read through `Session`, records the exact statement the adapter sent, and asks
 * the engine for its plan: on PostgreSQL with `enable_seqscan` off, where the claim is the predicate
 * as an index's condition, and on ClickHouse with `EXPLAIN indexes = 1`, where it is one granule kept.
 */

import { describe, expect, it } from 'vitest'

import { buildModel, colocationGroups, entity, loadMap, Session, T, Timestamp } from '../src/index.js'
import type { LogicalModel } from '../src/index.js'
import { fixture, type Database } from './_generation-engines.js'

const LIVE = process.env['SDE_POSTGRES_DSN'] !== undefined && process.env['SDE_CLICKHOUSE_DSN'] !== undefined

function model(): LogicalModel {
  return buildModel([
    entity('Reading', {
      fields: { station: T.string, at: T.timestamptz, sensor: T.string, celsius: T.int32 },
      key: ['station', 'at'],
    }),
    entity('Order', { fields: { id: T.uuid, account: T.string }, key: ['id'] }),
  ])
}

/** The layouts the reference's `default_layout` derives for this model, written out. */
const LAYOUTS = {
  postgres: {
    Reading: { table: 'reading', columns: { at: 'timestamptz', celsius: 'integer', sensor: 'text', station: 'text' } },
    Order: { table: 'order', columns: { account: 'text', id: 'uuid' } },
  },
  clickhouse: {
    Reading: { table: 'reading', columns: { at: "DateTime64(6, 'UTC')", celsius: 'Int32', sensor: 'String', station: 'String' } },
    Order: { table: 'order', columns: { account: 'String', id: 'UUID' } },
  },
} as const

function placementFor(built: LogicalModel, dialect: 'postgres' | 'clickhouse', indexes: Record<string, unknown>[]) {
  const groups: Record<string, unknown> = {}
  for (const group of colocationGroups(built)) {
    const tables: Record<string, string> = {}
    const columns: Record<string, Record<string, string>> = {}
    for (const member of group.members) {
      const declared = LAYOUTS[dialect][member as 'Reading' | 'Order']
      tables[member] = declared.table
      columns[member] = { ...declared.columns }
    }
    const mine = indexes.filter((index) => group.members.includes(String(index['entity'])))
    groups[group.name] = {
      source: { id: `${group.name}@e`, engine: 'e', layout: { tables, columns, ...(mine.length > 0 ? { indexes: mine } : {}) } },
    }
  }
  // Contract 3: indexes without a method are B-trees, and no write generation is needed for a
  // question about the planner.
  return loadMap({ contract: 3, model_version: built.version, map_version: 1, groups }, { model: built })
}

/** Every statement the adapter sends through its one raw method, with its values. */
function spy(engine: Database, method: 'run' | 'query'): [string, unknown[]][] {
  const target = engine as unknown as Record<string, (sql: string, values?: unknown[]) => Promise<unknown>>
  const original = target[method]!.bind(engine)
  const calls: [string, unknown[]][] = []
  target[method] = async (sql: string, values?: unknown[]) => {
    calls.push([sql, values ?? []])
    return original(sql, values)
  }
  return calls
}

async function pgScans(engine: Database, statement: string, values: unknown[]): Promise<string[]> {
  const run = (engine as unknown as { run: (sql: string, values?: unknown[]) => Promise<{ rows: Record<string, unknown>[] }> }).run.bind(engine)
  await run('SET enable_seqscan = off')
  const result = await run('EXPLAIN (FORMAT JSON) ' + statement, values)
  await run('RESET enable_seqscan')
  const plan = (result.rows[0]!['QUERY PLAN'] as { Plan: Record<string, unknown> }[])[0]!.Plan
  const nodes: string[] = []
  const walk = (node: Record<string, unknown>): void => {
    const type = String(node['Node Type'])
    if (type.includes('Scan')) {
      const label = type + (node['Index Name'] !== undefined ? ':' + String(node['Index Name']) : '')
      nodes.push(label + (node['Index Cond'] !== undefined ? ' on condition' : ''))
    }
    for (const child of (node['Plans'] as Record<string, unknown>[] | undefined) ?? []) walk(child)
  }
  walk(plan)
  return nodes
}

const START = Timestamp.from('2026-01-01T00:00:00Z')

async function fill(session: Session): Promise<void> {
  await session.saveMany('Reading', Array.from({ length: 1_000 }, (_, n) => ({
    station: `st-${String(n % 50).padStart(2, '0')}`,
    at: Timestamp.fromEpochMicroseconds(START.epochMicroseconds + BigInt(n) * 1_000_000n),
    sensor: `sn-${String(n % 400).padStart(3, '0')}`,
    celsius: n % 40,
  })))
}

describe.skipIf(!LIVE)('the library\'s scans use the engine\'s indexes', () => {
  it('PostgreSQL: a scan by a text key prefix uses the primary key, a count by a text column its index', async () => {
    await fixture('postgres', async (engine) => {
      const built = model()
      const index = { entity: 'Reading', name: 'reading_sensor_btree', columns: ['sensor'] }
      const placement = placementFor(built, 'postgres', [index])
      const session = await Session.open(built, placement, { e: engine })
      await session.ensureSchema()
      await fill(session)
      const calls = spy(engine, 'run')
      const page = await session.scan('Reading', { where: { station: 'st-07' }, limit: 5 })
      expect(page.rows.map((row) => row['station'])).toEqual(Array(5).fill('st-07'))
      const [scanned, scannedValues] = calls.at(-1)!
      expect(await session.count('Reading', { where: { sensor: 'sn-123' } })).toBe(3n)
      const [counted, countedValues] = calls.at(-1)!
      const byKey = await pgScans(engine, scanned, scannedValues)
      expect(byKey.some((scan) => scan.endsWith(':reading_pkey on condition')), JSON.stringify(byKey)).toBe(true)
      expect(byKey).not.toContain('Seq Scan')
      const bySensor = await pgScans(engine, counted, countedValues)
      expect(bySensor.some((scan) => scan.endsWith(':reading_sensor_btree on condition')), JSON.stringify(bySensor)).toBe(true)
      await session.close()
    })
  })

  it('ClickHouse: a scan by a uuid key keeps one granule', async () => {
    await fixture('clickhouse', async (engine) => {
      const built = model()
      const placement = placementFor(built, 'clickhouse', [])
      const session = await Session.open(built, placement, { e: engine })
      await session.ensureSchema()
      const ids = Array.from({ length: 40_000 }, (_, n) => {
        const hex = (BigInt(n + 1) * 2_654_435_761n).toString(16).padStart(32, '0')
        return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
      })
      for (let start = 0; start < ids.length; start += 1_000) {
        await session.saveMany('Order', ids.slice(start, start + 1_000).map((id, n) => ({ id, account: `a-${(start + n) % 97}` })))
      }
      const query = (engine as unknown as { query: (sql: string) => Promise<Record<string, unknown>[]> }).query.bind(engine)
      const command = (engine as unknown as { command: (sql: string) => Promise<void> }).command.bind(engine)
      await command('OPTIMIZE TABLE `order` FINAL')
      const calls = spy(engine, 'query')
      const page = await session.scan('Order', { where: { id: ids[12_345]! }, limit: 5 })
      expect(page.rows.map((row) => row['id'])).toEqual([ids[12_345]])
      const [statement] = calls.at(-1)!
      const explained = await query('EXPLAIN indexes = 1 ' + statement)
      const text = explained.map((row) => String(Object.values(row)[0])).join('\n')
      const granules = /Granules: (\d+)\/(\d+)/.exec(text)
      expect(granules, text).not.toBeNull()
      expect(Number(granules![2]), text).toBeGreaterThan(1)
      expect(Number(granules![1]), text).toBe(1)
      await session.close()
    })
  })
})

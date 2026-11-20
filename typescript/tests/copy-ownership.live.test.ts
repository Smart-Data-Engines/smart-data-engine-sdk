/**
 * A copy's table is one the library created and marked (format contract §7r), on live engines.
 *
 * `prepareSchema` created a copy's tables with `CREATE TABLE IF NOT EXISTS`, so a map naming a copy
 * like a table the client already had, of the same shape, made that table the copy. The controls:
 * a fresh copy is created and marked and taken again on a second run, and a source the client
 * already has is still taken as the source.
 */
import { describe, expect, it } from 'vitest'
import { buildModel, entity, loadMap, MigrationRefused, prepareSchema, T } from '../src/index.js'
import { copyMarker } from '../src/ownership.js'
import { fixture as engineFixture, type Database, type Dialect } from './_generation-engines.js'

const LIVE = process.env['SDE_POSTGRES_DSN'] !== undefined && process.env['SDE_CLICKHOUSE_DSN'] !== undefined
const PROJECT = '3'.repeat(32)
type Engines = Record<Dialect, Database>

function pair(body: (engines: Engines) => Promise<void>): Promise<void> {
  return engineFixture('postgres', (pg) => engineFixture('clickhouse', (ch) => body({ postgres: pg, clickhouse: ch })))
}

function provisioning(source: Dialect) {
  const model = buildModel([entity('Event', { fields: { id: T.int64, value: T.int32 }, key: ['id'] })])
  const copy: Dialect = source === 'postgres' ? 'clickhouse' : 'postgres'
  const material = (engine: Dialect, role: string) => ({ id: role, engine, layout: {
    tables: { Event: role + '_events' }, columns: { Event: engine === 'postgres'
      ? { id: 'bigint', value: 'integer' } : { id: 'Int64', value: 'Int32' } },
  } })
  const raw = { contract: 4, project_id: PROJECT, model_version: model.version, map_version: 1,
    groups: { Event: { write_epoch: 1, source: material(source, 'source'),
      derived: [{ ...material(copy, 'copy'), lag_budget_ms: 60000 }], also_write: ['copy'] } } }
  return { model, placement: loadMap(raw, { model }), copy }
}

async function sql(engine: Database, statement: string): Promise<void> {
  await (engine as unknown as { run?: (s: string) => Promise<unknown>; command?: (s: string) => Promise<void> })
    [engine.dialect === 'postgres' ? 'run' : 'command']!(statement)
}

async function clientTable(engine: Database, table: string): Promise<void> {
  if (engine.dialect === 'postgres') {
    await sql(engine, `CREATE TABLE "${table}" (id bigint PRIMARY KEY, value integer)`)
  } else {
    await sql(engine, `CREATE TABLE \`${table}\` (id Int64, value Int32) ENGINE = ReplacingMergeTree ORDER BY (id)`)
  }
  await engine.insert(table, { id: 7n, value: 70 })
}

async function markOf(engine: Database, table: string): Promise<string | null> {
  const found = await (engine as unknown as { mark: (t: string) => Promise<{ comment: string | null } | null> }).mark(table)
  return found === null ? null : found.comment
}

describe.skipIf(!LIVE)("a copy's table carries its creator's mark", () => {
  it.each(['postgres', 'clickhouse'] as const)('a copy is created marked and taken again (source %s)', async (source) => {
    await pair(async (engines) => {
      const { model, placement, copy } = provisioning(source)
      await prepareSchema(model, placement, engines, { projectId: PROJECT })
      expect(await markOf(engines[copy], 'copy_events')).toBe(copyMarker(PROJECT, 'Event', 'copy_events'))
      // A second run finds its own mark and takes the table again, as provisioning always did.
      await prepareSchema(model, placement, engines, { projectId: PROJECT })
    })
  })

  it.each(['postgres', 'clickhouse'] as const)("a client's table of the copy's name is refused and kept (source %s)", async (source) => {
    await pair(async (engines) => {
      const { model, placement, copy } = provisioning(source)
      await clientTable(engines[copy], 'copy_events')
      await expect(prepareSchema(model, placement, engines, { projectId: PROJECT }))
        .rejects.toThrow(MigrationRefused)
      await expect(prepareSchema(model, placement, engines, { projectId: PROJECT }))
        .rejects.toThrow(/not one this library created for it/)
      // Nothing was written to it: its row is there, and no generation column of ours.
      expect(await engines[copy].get('copy_events', { id: 7n })).toEqual({ id: 7n, value: 70 })
    })
  })

  it.each(['postgres', 'clickhouse'] as const)('a source the client has is still taken (source %s)', async (source) => {
    await pair(async (engines) => {
      const { model, placement } = provisioning(source)
      await clientTable(engines[source], 'source_events')
      await prepareSchema(model, placement, engines, { projectId: PROJECT })
      expect(await engines[source].get('source_events', { id: 7n })).toMatchObject({ id: 7n, value: 70 })
    })
  })
})

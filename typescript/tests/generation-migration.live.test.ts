/** A stored epoch is bookkeeping, and a delayed copy must retain its original session's epoch. */
import { describe, expect, it } from 'vitest'
import { backfill, buildModel, entity, loadMap, prepareSchema, Session, T, verify, WRITE_EPOCH_COLUMN, type PlacementMap } from '../src/index.js'
import { fixture as engineFixture, type Database, type Dialect } from './_generation-engines.js'

const LIVE = process.env['SDE_POSTGRES_DSN'] !== undefined && process.env['SDE_CLICKHOUSE_DSN'] !== undefined
const PROJECT = '1'.repeat(32), HOLD = '5'.repeat(32)
type Engines = Record<Dialect, Database>

function pair(body: (engines: Engines) => Promise<void>): Promise<void> {
  return engineFixture('postgres', (pg) => engineFixture('clickhouse', (ch) => body({ postgres: pg, clickhouse: ch })))
}
function document(source: Dialect, epoch: number) {
  const model = buildModel([entity('Event', { fields: { id: T.int64, value: T.int32 }, key: ['id'] })])
  const target: Dialect = source === 'postgres' ? 'clickhouse' : 'postgres'
  const material = (engine: Dialect, role: string) => ({ id: role, engine, layout: {
    tables: { Event: role + '_events' }, columns: { Event: engine === 'postgres'
      ? { id: 'bigint', value: 'integer' } : { id: 'Int64', value: 'Int32' } },
  } })
  const raw = { contract: 4, project_id: PROJECT, model_version: model.version, map_version: epoch,
    groups: { Event: { write_epoch: epoch, source: material(source, 'source'),
      derived: [{ ...material(target, 'copy'), lag_budget_ms: 60000 }], also_write: ['copy'] } } }
  return { model, placement: loadMap(raw, { model }), target }
}
async function advance(engines: Engines, placement: PlacementMap, epoch: number): Promise<void> {
  const spot = placement.groups['Event']!
  for (const material of [spot.source, ...spot.derived]) {
    const fence = engines[material.engine as Dialect].writeFence(material.layout.tables['Event']!, { projectId: PROJECT })
    await fence.freeze(HOLD); await fence.advance(epoch); await fence.release(HOLD)
  }
}

describe.skipIf(!LIVE)('generation-aware native migration', () => {
  it.each(['postgres', 'clickhouse'] as const)('copies historical epochs faithfully from %s', async (source) => {
    await pair(async (engines) => {
      const first = document(source, 1)
      await prepareSchema(first.model, first.placement, engines, { projectId: PROJECT })
      await engines[source].insert('source_events', { id: 1n, value: 11, [WRITE_EPOCH_COLUMN]: 1 })
      await advance(engines, first.placement, 2)
      const next = document(source, 2)
      const session = await Session.open(next.model, next.placement, engines, { projectId: PROJECT })
      await backfill(session, 'Event')
      expect((await engines[next.target].get('copy_events', { id: 1n }))?.[WRITE_EPOCH_COLUMN]).toBe(2n)
      expect((await engines[source].get('source_events', { id: 1n }))?.[WRITE_EPOCH_COLUMN]).toBe(1n)
      expect((await verify(session, 'Event')).matched).toBe(true)
      await session.save('Event', { id: 2n, value: 22 })
      expect((await engines[next.target].get('copy_events', { id: 2n }))?.[WRITE_EPOCH_COLUMN]).toBe(2n)
      expect(await session.get('Event', { id: 2n })).toEqual({ id: 2n, value: 22 })
    })
  })
  it('keeps a deferred copy old after commit and a newer session opens', async () => {
    await pair(async (engines) => {
      const first = document('postgres', 1)
      await prepareSchema(first.model, first.placement, engines, { projectId: PROJECT })
      const old = await Session.open(first.model, first.placement, engines, { projectId: PROJECT })
      const original = engines.postgres.transaction.bind(engines.postgres)
      let once = true
      engines.postgres.transaction = async <T>(body: () => Promise<T>): Promise<T> => {
        const result = await original(body)
        if (once) {
          once = false
          await advance(engines, first.placement, 2)
          const next = document('postgres', 2)
          await Session.open(next.model, next.placement, engines, { projectId: PROJECT })
          await engines.clickhouse.insert('copy_events', { id: 1n, value: 99, [WRITE_EPOCH_COLUMN]: 2 })
        }
        return result
      }
      try {
        await old.transaction(['Event'], async (session) => { await session.save('Event', { id: 1n, value: 11 }) })
        expect((await engines.postgres.get('source_events', { id: 1n }))?.['value']).toBe(11)
        expect((await engines.clickhouse.get('copy_events', { id: 1n }))?.['value']).toBe(99)
      } finally {
        engines.postgres.transaction = original
      }
    })
  })
})

/** Stable verification is a native barrier plus exact logical comparison, not a live count heuristic. */
import { describe, expect, it } from 'vitest'
import { buildModel, entity, loadMap, prepareSchema, Session, T, verify, verificationRequest,
  InspectionContext, verifyFrozen, frozenVerifyRecord, WRITE_EPOCH_COLUMN, FenceState } from '../src/index.js'
import { fixture as engineFixture, type Database } from './_generation-engines.js'

const LIVE = process.env['SDE_POSTGRES_DSN'] !== undefined && process.env['SDE_CLICKHOUSE_DSN'] !== undefined
const PROJECT = '1'.repeat(32), HOLD = '6'.repeat(32)
type Engines = { postgres: Database; clickhouse: Database }
function pair(body: (engines: Engines) => Promise<void>): Promise<void> {
  return engineFixture('postgres', (pg) => engineFixture('clickhouse', (ch) => body({ postgres: pg, clickhouse: ch })))
}
async function setup(engines: Engines) {
  const model = buildModel([entity('Event', { fields: { id: T.int64, value: T.int32 }, key: ['id'] })])
  const raw = { contract: 4, project_id: PROJECT, model_version: model.version, map_version: 1,
    groups: { Event: { write_epoch: 1,
      source: { id: 'source', engine: 'postgres', layout: { tables: { Event: 'source_events' },
        columns: { Event: { id: 'bigint', value: 'integer' } } } },
      derived: [{ id: 'copy', engine: 'clickhouse', lag_budget_ms: 60000,
        layout: { tables: { Event: 'copy_events' }, columns: { Event: { id: 'Int64', value: 'Int32' } } } }],
      also_write: ['copy'] } } }
  const placement = loadMap(raw, { model })
  await prepareSchema(model, placement, engines, { projectId: PROJECT })
  const session = await Session.open(model, placement, engines, { projectId: PROJECT })
  await session.save('Event', { id: 1n, value: 11 })
  const request = verificationRequest(placement, { group: 'Event', projectId: PROJECT,
    requestId: '7'.repeat(32), requestedAt: '2026-09-12T12:00:00Z' })
  return { context: new InspectionContext(model, placement, engines, { projectId: PROJECT }), session, request }
}

describe.skipIf(!LIVE)('frozen exact comparison', () => {
  it('refuses a target-only row and keeps both barriers installed', async () => {
    await pair(async (engines) => {
      const { context, session, request } = await setup(engines)
      await engines.clickhouse.insert('copy_events', { id: 99n, value: 99, [WRITE_EPOCH_COLUMN]: 1 })
      expect((await verify(session, 'Event')).matched).toBe(true)
      const report = await verifyFrozen(context, 'Event', { request, holdId: HOLD, epochs: { source: 1, copy: 1 } })
      expect(report.matched).toBe(false)
      expect(report.comparison.rowsSource).toBe(1)
      expect(report.comparison.rowsTarget).toBe(2)
      expect(report.barriers).toHaveLength(2)
      expect(frozenVerifyRecord(report)['comparison']).not.toHaveProperty('differences')
      for (const [engine, table] of [[engines.postgres, 'source_events'], [engines.clickhouse, 'copy_events']] as const) {
        expect((await engine.writeFence(table, { projectId: PROJECT }).state()).holds).toContain(HOLD)
        await expect(engine.insert(table, { id: 2n, value: 22, [WRITE_EPOCH_COLUMN]: 1 })).rejects.toThrow()
      }
    })
  })
  it('compares different native epochs without adopting a runtime map', async () => {
    await pair(async (engines) => {
      const { context, request } = await setup(engines)
      const fence = engines.clickhouse.writeFence('copy_events', { projectId: PROJECT })
      await fence.freeze('8'.repeat(32)); await fence.advance(2); await fence.release('8'.repeat(32))
      await engines.clickhouse.insert('copy_events', { id: 1n, value: 11, [WRITE_EPOCH_COLUMN]: 2 })
      await expect(Session.open(context.model, context.placement, engines, { projectId: PROJECT })).rejects.toThrow('write generation')
      const report = await verifyFrozen(context, 'Event', { request, holdId: HOLD, epochs: { source: 1, copy: 2 } })
      expect(report.matched).toBe(true)
      expect(Object.fromEntries(report.barriers.map((item) => [item.materialization, item.epoch]))).toEqual({ source: 1, copy: 2 })
      expect(await engines.postgres.mapWatermark()).toBeNull()
      expect(await engines.clickhouse.mapWatermark()).toBeNull()
    })
  })
  it('invalidates a result if its source barrier was released during comparison', async () => {
    await pair(async (engines) => {
      const { context, request } = await setup(engines)
      const original = engines.postgres.count.bind(engines.postgres)
      engines.postgres.count = async (table) => {
        const result = await original(table)
        await engines.postgres.writeFence('source_events', { projectId: PROJECT }).release(HOLD)
        return result
      }
      try {
        await expect(verifyFrozen(context, 'Event', { request, holdId: HOLD, epochs: { source: 1, copy: 1 } })).rejects.toThrow('lost its named barrier')
      } finally { engines.postgres.count = original }
    })
  })
  it('refuses a wrong expected epoch before closing either table', async () => {
    await pair(async (engines) => {
      const { context, request } = await setup(engines)
      await expect(verifyFrozen(context, 'Event', { request, holdId: HOLD, epochs: { source: 1, copy: 2 } })).rejects.toThrow('expected write generation')
      expect((await engines.postgres.writeFence('source_events', { projectId: PROJECT }).state()).holds).toEqual([])
      expect((await engines.clickhouse.writeFence('copy_events', { projectId: PROJECT }).state()).holds).toEqual([])
    })
  })
  it('does not let equal counts hide a different target value', async () => {
    await pair(async (engines) => {
      const { context, request } = await setup(engines)
      await engines.clickhouse.insert('copy_events', { id: 1n, value: 99, [WRITE_EPOCH_COLUMN]: 1 })
      const report = await verifyFrozen(context, 'Event', { request, holdId: HOLD, epochs: { source: 1, copy: 1 } })
      expect(report.comparison.rowsSource).toBe(1)
      expect(report.comparison.rowsTarget).toBe(1)
      expect(report.comparison.matched).toBe(false)
      expect(report.matched).toBe(false)
    })
  })

  it('refuses a retired id before closing another table', async () => {
    await pair(async (engines) => {
      const { context, request } = await setup(engines)
      const source = engines.postgres.writeFence('source_events', { projectId: PROJECT })
      await source.freeze(HOLD); await source.release(HOLD)
      await expect(verifyFrozen(context, 'Event', { request, holdId: HOLD, epochs: { source: 1, copy: 1 } })).rejects.toThrow('retired barrier id')
      expect((await source.state()).holds).toEqual([])
      expect((await engines.clickhouse.writeFence('copy_events', { projectId: PROJECT }).state()).holds).toEqual([])
    })
  })

  it('invalidates a matching comparison when the table identity changes', async () => {
    await pair(async (engines) => {
      const { context, request } = await setup(engines)
      const actualFactory = engines.postgres.writeFence.bind(engines.postgres)
      const actualCount = engines.postgres.count.bind(engines.postgres)
      let compared = false
      engines.postgres.count = async (table) => { const value = await actualCount(table); compared = true; return value }
      engines.postgres.writeFence = (table, options) => {
        const fence = actualFactory(table, options), actualState = fence.state.bind(fence)
        fence.state = async () => {
          const state = await actualState()
          return compared ? new FenceState('replacement-table', state.projectId, state.column,
            state.minimums, state.maximums, state.holds, state.retired) : state
        }
        return fence
      }
      try {
        await expect(verifyFrozen(context, 'Event', { request, holdId: HOLD, epochs: { source: 1, copy: 1 } })).rejects.toThrow('changed identity')
      } finally {
        engines.postgres.writeFence = actualFactory
        engines.postgres.count = actualCount
      }
    })
  })
  it('retains the request captured before asynchronous metadata calls', async () => {
    await pair(async (engines) => {
      const { context, request } = await setup(engines)
      const replacement = verificationRequest(context.placement, { group: 'Event', projectId: PROJECT,
        requestId: '9'.repeat(32), requestedAt: '2026-09-12T12:00:00Z' })
      const options = { request, holdId: HOLD, epochs: { source: 1, copy: 1 } }
      const actual = engines.clickhouse.validateSchema.bind(engines.clickhouse)
      engines.clickhouse.validateSchema = async (layout) => {
        await actual(layout)
        options.request = replacement
      }
      try {
        const report = await verifyFrozen(context, 'Event', options)
        expect(report.comparison.request?.requestId).toBe(request.requestId)
      } finally { engines.clickhouse.validateSchema = actual }
    })
  })

})

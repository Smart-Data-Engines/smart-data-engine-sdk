/** Session read policy independently of native SQL compilation. */
import { expect, it } from 'vitest'
import { buildModel, entity, enumerateShapes, loadMap, QueryRefused, Recorder, ResourceClosed,
  Session, shapeId, T, type ReadPlan, type Row, type ScanOptions } from '../src/index.js'
import { MemoryEngine } from '../src/testing/memory.js'

class Reader extends MemoryEngine {
  constructor(name: string, readonly calls: string[]) { super({ name }) }
  async selectRows(_table: string, _plan: ReadPlan): Promise<Row[]> {
    this.calls.push(this.name + '.select')
    return [{ id: 2n, label: this.name }, { id: 3n, label: 'sentinel' }]
  }
  async countRows(_table: string, _plan: ReadPlan): Promise<bigint> {
    this.calls.push(this.name + '.count')
    return 9007199254740993n
  }
}
async function fixture(partial = false, capability = true) {
  const model = buildModel([entity('Event', { fields: { id: T.int64, label: T.string }, key: ['id'] })])
  const routing = Object.fromEntries(enumerateShapes(model).filter(shape => ['full_scan', 'aggregate'].includes(shape.kind))
    .map(shape => [shapeId(shape), 'copy']))
  const full = { tables: { Event: 'event' }, columns: { Event: { id: 'bigint', label: 'text' } } }
  const copied = { tables: { Event: 'copy' }, columns: { Event: partial ? { id: 'bigint' } : full.columns.Event } }
  const placement = loadMap({ contract: 3, model_version: model.version, map_version: 1,
    groups: { Event: { source: { id: 'source', engine: 'source', layout: full },
      derived: [{ id: 'copy', engine: 'copy', layout: copied, lag_budget_ms: 30000 }] } }, routing }, { model })
  const calls: string[] = [], recorder = new Recorder(model.version)
  const session = await Session.open(model, placement, {
    source: new Reader('source', calls), copy: capability ? new Reader('copy', calls) : new MemoryEngine(),
  }, { recorder })
  return { session, calls, recorder }
}
it('uses the routed copy and overrides it only for fresh or a source transaction', async () => {
  const { session, calls } = await fixture()
  const page = await session.scan('Event', { limit: 1 })
  expect(page.rows).toEqual([{ id: 2n, label: 'copy' }])
  expect(page.nextAfter).toEqual({ id: 2n })
  page.rows[0]!['id'] = 99n
  expect(page.nextAfter).toEqual({ id: 2n })
  expect((await session.scan('Event', { fresh: true, limit: 1 })).rows[0]!['label']).toBe('source')
  await session.transaction(['Event'], async () => {
    expect((await session.scan('Event', { limit: 1 })).rows[0]!['label']).toBe('source')
  })
  expect(calls).toEqual(['copy.select', 'source.select', 'source.select'])
})
it('refuses a missing projection without falling back, while count needs only its filter fields', async () => {
  const { session, calls } = await fixture(true)
  await expect(session.scan('Event')).rejects.toThrow(QueryRefused)
  expect(calls).toEqual([])
  expect(await session.count('Event')).toBe(9007199254740993n)
  await expect(session.count('Event', { where: { label: 'sensitive filter' } })).rejects.toThrow(QueryRefused)
  expect(calls).toEqual(['copy.count'])
  expect((await session.scan('Event', { fresh: true })).rows[0]!['label']).toBe('source')
})
it('keeps the exact count out of telemetry', async () => {
  const { session, recorder } = await fixture()
  expect(await session.count('Event', { where: { label: 'private value' } })).toBe(9007199254740993n)
  const window = recorder.roll()!
  expect(window.shapes).toHaveLength(1)
  expect(window.shapes[0]).toMatchObject({ kind: 'aggregate', rows: 1 })
  const encoded = JSON.stringify(window)
  expect(encoded).not.toContain('private value')
  expect(encoded).not.toContain('9007199254740993')
})
it.each(['scan', 'count'] as const)('checks the %s capability and lifetime before I/O', async operation => {
  const { session, calls } = await fixture(false, false)
  await expect(session[operation]('Event')).rejects.toThrow(QueryRefused)
  expect(calls).toEqual([])
  await session.close()
  await expect(session[operation]('Event')).rejects.toThrow(ResourceClosed)
})
it.each([
  { fresh: 1 }, { where: [] }, { limit: 0 }, { after: { bad: 1 } },
  { bounds: { field: 'missing', low: 1 } }, { orderBy: 'missing' },
])('refuses malformed scans before an adapter call %#', async options => {
  const { session, calls, recorder } = await fixture()
  await expect(session.scan('Event', options as ScanOptions)).rejects.toThrow(QueryRefused)
  expect(calls).toEqual([])
  expect(recorder.roll()).toBeUndefined()
})

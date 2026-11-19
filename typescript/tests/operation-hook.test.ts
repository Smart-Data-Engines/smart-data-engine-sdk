/**
 * The session's operation hook: a record a completed operation, nothing the operation carried - the
 * counterpart of python/tests/test_operation_hook.py.
 *
 * Engine-free: the in-memory double answers reads from its own tables, so every method is reached
 * without a server. In the sentinel test the control is the sentinel coming back in the rows the
 * operations return, so the test cannot pass by looking at nothing.
 */
import { afterEach, beforeEach, expect, it } from 'vitest'
import { EngineError, enumerateShapes, hashIdentifiers, internalFailures, loadMap, ModelPlanningError,
  OPERATION_FIELDS, OPERATIONS, outcomeOf, resetInternalFailures, Session, shapeId,
  type OperationRecord, type ReadColumn, type ReadPlan, type Row } from '../src/index.js'
import { modelFromNeutral } from '../src/testing/loader.js'
import { MemoryEngine } from '../src/testing/memory.js'

const SENTINEL = 'sentinel-7f3a9c41-in-every-value'

class Reader extends MemoryEngine {
  async selectRows(table: string, plan: ReadPlan): Promise<Row[]> {
    return (this.tables[table] ?? []).map((row) => ({ ...row })).slice(0, plan.limit + 1)
  }
  async countRows(table: string, _plan: ReadPlan): Promise<bigint> {
    return BigInt((this.tables[table] ?? []).length)
  }
  async summarizeRows(table: string, _plan: ReadPlan, column: ReadColumn): Promise<Row> {
    const values = (this.tables[table] ?? []).map((row) => BigInt(row[column.name] as bigint))
    const total = values.reduce((a, b) => a + b, 0n)
    const min = values.reduce((a, b) => (b < a ? b : a))
    const max = values.reduce((a, b) => (b > a ? b : a))
    return { sde_count: values.length, sde_present: values.length, sde_min: String(min),
      sde_max: String(max), sde_total: String(total) }
  }
}

const LAYOUT = {
  tables: { Note: 'note', Tag: 'tag' },
  columns: { Note: { body: 'text', id: 'text', size: 'bigint' }, Tag: { id: 'text', note: 'text' } },
}

const model = modelFromNeutral({
  entities: [
    { name: 'Note', fields: [{ name: 'body', type: 'string' }, { name: 'id', type: 'string' },
      { name: 'size', type: 'int64' }], key: ['id'] },
    { name: 'Tag', fields: [{ name: 'id', type: 'string' }, { name: 'note', type: 'string' }], key: ['id'] },
  ],
  relations: [],
  atomic: [['Note', 'Tag']],
})

async function open(records: OperationRecord[], engine: MemoryEngine = new Reader({ name: 'pg-main' })) {
  const placement = loadMap({ contract: 3, model_version: model.version, map_version: 1,
    groups: { Note: { source: { id: 'notes@pg', engine: 'pg-main', layout: LAYOUT } } } }, { model })
  return Session.open(model, placement, { 'pg-main': engine }, { hook: (record) => records.push(record) })
}

beforeEach(() => resetInternalFailures())
afterEach(() => resetInternalFailures())

it('makes each method one record with where it went', async () => {
  const records: OperationRecord[] = []
  const notes = await open(records)
  await notes.save('Note', { id: 'a', body: 'one', size: 1n })
  await notes.saveMany('Note', [{ id: 'b', body: 'two', size: 2n }, { id: 'c', body: 'three', size: 3n }])
  expect(await notes.get('Note', { id: 'a' })).not.toBeNull()
  await notes.scan('Note', { limit: 10 })
  await notes.count('Note')
  await notes.summarize('Note', 'size')
  expect(records.map((r) => [r.operation, r.rows])).toEqual([
    ['save', 1], ['save_many', 2], ['get', 1], ['scan', 3], ['count', 1], ['summarize', 1],
  ])
  const shapes = new Set(enumerateShapes(model).map((shape) => shapeId(shape)))
  for (const record of records) {
    expect([record.entity, record.group, record.engine]).toEqual(['Note', 'Note', 'pg-main'])
    expect([record.dialect, record.materialization, record.outcome]).toEqual(['postgres', 'notes@pg', 'ok'])
    expect(record.durationNs).toBeGreaterThanOrEqual(0)
    expect(OPERATIONS).toContain(record.operation)
    expect(shapes.has(record.shape)).toBe(true)
    expect(Object.isFrozen(record)).toBe(true)
  }
})

it('makes a transaction a record of its own beside its operations', async () => {
  const records: OperationRecord[] = []
  const notes = await open(records)
  await notes.transaction(['Note', 'Tag'], async (inner) => {
    await inner.save('Note', { id: 'a', body: 'one', size: 1n })
    await inner.save('Tag', { id: 't', note: 'a' })
  })
  expect(records.map((r) => [r.operation, r.entity, r.outcome])).toEqual([
    ['save', 'Note', 'ok'], ['save', 'Tag', 'ok'], ['transaction', 'Note', 'ok'],
  ])
  expect([records[2]!.shape, records[2]!.materialization]).toEqual(['', 'notes@pg'])
})

it('says what ended a transaction that threw', async () => {
  const records: OperationRecord[] = []
  const notes = await open(records)
  await expect(notes.transaction(['Note'], async () => { throw new ModelPlanningError('gave up') }))
    .rejects.toThrow(ModelPlanningError)
  await expect(notes.transaction(['Note'], async () => { throw new TypeError('not ours') }))
    .rejects.toThrow(TypeError)
  expect(records.map((r) => r.outcome)).toEqual(['ModelPlanningError', 'unexpected'])
})

it('gives an engine error as its class, never its message', async () => {
  class Refusing extends Reader {
    override async insert(table: string): Promise<void> {
      throw new EngineError(`insert into ${table} failed: Key (id)=(${SENTINEL}) already exists`)
    }
  }
  const records: OperationRecord[] = []
  const notes = await open(records, new Refusing({ name: 'pg-main' }))
  await expect(notes.save('Note', { id: SENTINEL, body: 'x', size: 1n })).rejects.toThrow(EngineError)
  expect(records).toHaveLength(1)
  expect([records[0]!.operation, records[0]!.outcome, records[0]!.rows]).toEqual(['save', 'EngineError', 1])
  expect(JSON.stringify(records[0])).not.toContain(SENTINEL)
})

it('lets a hook that throws change nothing, and counts it', async () => {
  const placement = loadMap({ contract: 3, model_version: model.version, map_version: 1,
    groups: { Note: { source: { id: 'notes@pg', engine: 'pg-main', layout: LAYOUT } } } }, { model })
  const notes = await Session.open(model, placement, { 'pg-main': new Reader({ name: 'pg-main' }) }, {
    hook: () => { throw new Error("the application's exporter is down") },
  })
  await notes.save('Note', { id: 'a', body: 'one', size: 1n })
  expect(await notes.get('Note', { id: 'a' })).toEqual({ id: 'a', body: 'one', size: 1n })
  expect(internalFailures()).toEqual({ 'session.hook': 2 })
})

it('gives the hook nothing an operation carried', async () => {
  const records: OperationRecord[] = []
  const notes = await open(records)
  await notes.save('Note', { id: SENTINEL, body: SENTINEL, size: 7n })
  await notes.saveMany('Note', [{ id: `${SENTINEL}-2`, body: SENTINEL, size: 8n }])
  await notes.transaction(['Note', 'Tag'], async (inner) => {
    await inner.save('Tag', { id: `${SENTINEL}-t`, note: SENTINEL })
  })
  const returned: unknown[] = [await notes.get('Note', { id: SENTINEL })]
  returned.push(...(await notes.scan('Note', { where: { body: SENTINEL }, limit: 10 })).rows)
  await notes.count('Note', { where: { body: SENTINEL } })
  await notes.summarize('Note', 'size', { where: { body: SENTINEL } })
  const text = (value: unknown) => JSON.stringify(value, (_key, item) => typeof item === 'bigint' ? String(item) : item)
  // The control: the value is there to be found, in what the operations return.
  expect(returned.some((item) => text(item).includes(SENTINEL))).toBe(true)
  expect(records).toHaveLength(8)
  for (const record of records) expect(text(record)).not.toContain(SENTINEL)
})

it('has the contract\'s fields in order', () => {
  expect(OPERATION_FIELDS).toEqual(['operation', 'entity', 'group', 'shape', 'engine', 'dialect',
    'materialization', 'durationNs', 'rows', 'outcome'])
  expect(outcomeOf(undefined)).toBe('ok')
  expect(outcomeOf(new EngineError('x'))).toBe('EngineError')
  expect(outcomeOf(new RangeError('x'))).toBe('unexpected')
})

it('names a hashed model in the application\'s own words', async () => {
  const plain = modelFromNeutral({ entities: [{ name: 'Note', fields: [{ name: 'body', type: 'string' },
    { name: 'id', type: 'string' }], key: ['id'] }], relations: [], atomic: [] })
  const { model: hashed, names } = hashIdentifiers(plain, Buffer.from('operation-hook-salt-for-a-test!!'))
  const group = Object.values(names.entities)[0]!
  const placement = loadMap({ contract: 3, model_version: hashed.version, map_version: 1,
    groups: { [group]: { source: { id: 'notes@pg', engine: 'pg-main', layout: { auto: true } } } } }, { model: hashed })
  const records: OperationRecord[] = []
  const notes = await Session.open(hashed, placement, { 'pg-main': new Reader({ name: 'pg-main' }) },
    { names, hook: (record) => records.push(record) })
  await notes.save('Note', { id: 'a', body: 'one' })
  expect([records[0]!.entity, records[0]!.group]).toEqual(['Note', 'Note'])
  expect(group).not.toBe('Note')  // the control: the model's own name is a digest
})

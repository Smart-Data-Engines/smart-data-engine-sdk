/** Application batches retain source/transaction/copy and value boundaries. */
import { expect, it } from 'vitest'
import { buildModel, BulkWriteRefused, entity, hashIdentifiers, loadMap, MAX_BATCH_ROWS,
  ModelPlanningError, Recorder, ResourceClosed, Session, T, Timestamp, type Row } from '../src/index.js'
import { batchColumns } from '../src/bulk.js'
import { MemoryEngine, Recorded } from '../src/testing/memory.js'

async function fixture(options: { hashed?: boolean; json?: boolean; sourceBulk?: boolean; copyBulk?: boolean } = {}) {
  let model = buildModel([entity('Event', { fields: { id: T.int64, value: options.json ? T.json : T.string }, key: ['id'] })])
  let names
  if (options.hashed) ({ model, names } = hashIdentifiers(model, Buffer.alloc(32, 'b')))
  const name = model.entities[0]!.name
  const raw = { contract: 3, model_version: model.version, map_version: 1, groups: {
    [name]: { source: { id: 'source', engine: 'source', layout: { auto: true } },
      derived: [{ id: 'copy', engine: 'copy', layout: { auto: true }, lag_budget_ms: 30000 }], also_write: ['copy'] },
  } }
  const journal = new Recorded()
  const source = new MemoryEngine({ name: 'source', journal, canBulkWrite: options.sourceBulk ?? true })
  const copy = new MemoryEngine({ name: 'copy', journal, canBulkWrite: options.copyBulk ?? true })
  const recorder = new Recorder(model.version)
  const session = await Session.open(model, loadMap(raw, { model }), { source, copy }, { recorder, ...(names ? { names } : {}) })
  const table = session.placement.groups[name]!.source.layout.tables[name]!
  return { session, source, copy, journal, recorder, table }
}
const rows = () => [{ id: 1, value: 'one' }, { value: 'two', id: 2 }]

it('uses one bulk shape and one source call followed by one copy call', async () => {
  const { session, source, copy, table, journal, recorder } = await fixture()
  await session.saveMany('Event', rows())
  expect(journal.calls).toEqual(['source', 'copy'].map(engine => ({ engine, call: 'insert_many', table, rows: 2 })))
  expect(source.tables[table]).toEqual(rows()); expect(copy.tables[table]).toEqual(rows())
  const window = recorder.roll()!
  expect(window.shapes).toHaveLength(1)
  expect(window.shapes[0]).toMatchObject({ kind: 'bulk_write', calls: 1, rows: 2, errors: 0 })
})

for (const hashed of [false, true]) it(`snapshots nested JSON before outer commit (hashed=${hashed})`, async () => {
  const { session, source, copy, table } = await fixture({ hashed, json: true })
  const input = [{ id: 1, value: { nested: ['original'] } }]
  await session.transaction(['Event'], async () => {
    await session.transaction(['Event'], async () => session.saveMany('Event', input))
    input[0]!.id = 99; input[0]!.value.nested[0] = 'changed'; input.length = 0
    expect(copy.tables).toEqual({})
  })
  expect(source.tables[table]).toEqual(copy.tables[table])
  expect(await session.get('Event', { id: 1 })).toEqual({ id: 1, value: { nested: ['original'] } })
})

it('does not keep an inner rollback or replay an inner commit before outer commit', async () => {
  const { session, source, copy, table } = await fixture()
  await session.transaction(['Event'], async () => {
    await session.saveMany('Event', [rows()[0]!])
    await expect(session.transaction(['Event'], async () => {
      await session.saveMany('Event', [rows()[1]!]); throw new Error('inner rollback')
    })).rejects.toThrow('inner rollback')
    expect(copy.tables).toEqual({})
  })
  expect(source.tables[table]).toEqual([rows()[0]]); expect(copy.tables[table]).toEqual([rows()[0]])
  await expect(session.transaction(['Event'], async () => {
    await session.transaction(['Event'], async () => session.saveMany('Event', [rows()[1]!]))
    throw new Error('outer rollback')
  })).rejects.toThrow('outer rollback')
  expect(source.tables[table]).toEqual([rows()[0]]); expect(copy.tables[table]).toEqual([rows()[0]])
})

it.each(['source', 'copy'])('preflights the %s capability before any source work', async side => {
  const { session, journal } = await fixture({ sourceBulk: side !== 'source', copyBulk: side !== 'copy' })
  await expect(session.saveMany('Event', rows())).rejects.toThrow(BulkWriteRefused)
  expect(journal.calls).toEqual([])
})

it.each([{}, 'rows', [null], [{}], [{ id: 1 }], [{ id: 1, value: 'v', unknown: 9 }],
  [{ id: 1, value: 'v' }, { id: 2 }], Array(MAX_BATCH_ROWS + 1).fill({ id: 1, value: 'v' }),
  [{ id: 1, value: () => 0 }], [{ id: 1, value: undefined }], [{ id: 1, value: new Map() }],
])('refuses an invalid batch before I/O %#', async input => {
  const { session, journal, recorder } = await fixture()
  await expect(session.saveMany('Event', input as readonly Row[])).rejects.toThrow(BulkWriteRefused)
  expect(journal.calls).toEqual([]); expect(recorder.roll()).toBeUndefined()
})

it('validates empty batches against the session and entity without needing a bulk adapter', async () => {
  const { session, journal, recorder } = await fixture({ sourceBulk: false, copyBulk: false })
  await session.saveMany('Event', [])
  await expect(session.saveMany('Missing', [])).rejects.toThrow(ModelPlanningError)
  await session.close()
  await expect(session.saveMany('Event', [])).rejects.toThrow(ResourceClosed)
  expect(journal.calls).toEqual([]); expect(recorder.roll()).toBeUndefined()
})

it('counts the technical generation in the limit', () => {
  const row = Object.fromEntries(Array.from({ length: 60 }, (_, i) => [`f${i}`, i]))
  expect(batchColumns(Array(1000).fill(row))).toHaveLength(60)
  expect(() => batchColumns(Array(1000).fill(row), 1)).toThrow('60000')
  expect(batchColumns(Array(983).fill({ ...row, extra: 1 }))).toHaveLength(61)
  expect(() => batchColumns(Array(984).fill({ ...row, extra: 1 }))).toThrow('60000')
  // Space-joined field names would make these unequal sets appear equal.
  expect(() => batchColumns([{ 'a b': 1, c: 2 }, { a: 1, 'b c': 2 }])).toThrow(BulkWriteRefused)
})

it('snapshots before the first await, preserving mutable dates, buffers and exact Timestamp', async () => {
  const { session, source, copy, table } = await fixture()
  const instant = Timestamp.from('2026-09-14T00:00:00.123456Z')
  const date = new Date('2026-09-14T00:00:00.123Z'), buffer = Buffer.from('abc')
  const input = [{ id: 1, value: { date, buffer, instant } }]
  const write = session.saveMany('Event', input)
  date.setTime(0); buffer[0] = 0; input[0]!.id = 99
  await write
  expect(source.tables[table]).toEqual(copy.tables[table])
  expect(source.tables[table]![0]).toEqual({ id: 1, value: {
    date: new Date('2026-09-14T00:00:00.123Z'), buffer: Buffer.from('abc'), instant,
  } })
})

it('refuses cyclic values locally', async () => {
  const { session, journal } = await fixture({ json: true })
  const value: Row = {}; value['cycle'] = value
  await expect(session.saveMany('Event', [{ id: 1, value }])).rejects.toThrow('cycles')
  expect(journal.calls).toEqual([])
})

for (const hashed of [false, true]) it(`uses logical relation columns through hashing=${hashed}`, async () => {
  const { ref } = await import('../src/index.js')
  let model = buildModel([
    entity('Parent', { fields: { id: T.int64 }, key: ['id'] }),
    entity('Child', { fields: { id: T.int64 }, key: ['id'], relations: { parent: ref('Parent') } }),
  ])
  let names
  if (hashed) ({ model, names } = hashIdentifiers(model, Buffer.alloc(32, 'r')))
  const { colocationGroups, groupColumns } = await import('../src/index.js')
  const group = colocationGroups(model)[0]!
  const placement = loadMap({ contract: 3, model_version: model.version, map_version: 1,
    groups: { [group.name]: { source: { id: 'source', engine: 'db', layout: { auto: true } } } } }, { model })
  const engine = new MemoryEngine()
  const session = await Session.open(model, placement, { db: engine }, names ? { names } : {})
  await session.saveMany('Parent', [{ id: 1 }])
  await session.saveMany('Child', [{ id: 2, parent_id: 1 }])
  expect(await session.get('Child', { id: 2 })).toEqual({ id: 2, parent_id: 1 })
  const columns = groupColumns(model, group)
  for (const [entity, table] of Object.entries(placement.groups[group.name]!.source.layout.tables)) {
    expect(Object.keys(engine.tables[table]![0]!).sort()).toEqual(Object.keys(columns[entity]!).sort())
  }
})

it('refuses a bulk call inherited from an expired transaction scope', async () => {
  const { session, source } = await fixture()
  let release!: () => void
  const ready = new Promise<void>(resolve => { release = resolve })
  let escaped!: Promise<unknown>
  await session.transaction(['Event'], async () => {
    escaped = ready.then(() => session.saveMany('Event', rows())).catch(error => error)
  })
  release()
  expect(await escaped).toBeInstanceOf(ResourceClosed)
  expect(source.tables).toEqual({})
})

it('gives the shared local refusal for an unknown hashed batch field', async () => {
  const { session, journal } = await fixture({ hashed: true })
  await expect(session.saveMany('Event', [{ id: 1, value: 'v', unknown: 9 }])).rejects.toThrow(BulkWriteRefused)
  expect(journal.calls).toEqual([])
})

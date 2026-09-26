/**
 * `Session.measureStorage` without a server: the mirror of the reference's
 * `test_measure_storage.py`. The in-memory engine, with or without a catalogue, reaches every
 * branch - a size summed over a group's tables, the source alone during a staging, an adapter
 * without a catalogue, a refused read, a failed one, a missing table and adapter misuse.
 */
import { describe, expect, it } from 'vitest'

import { EngineError, loadMap, Recorder, ResourceBusy, Session, windowRecord } from '../src/index.js'
import { MemoryEngine } from '../src/testing/memory.js'
import { modelFromNeutral } from '../src/testing/loader.js'

const MODEL = modelFromNeutral({
  entities: [
    { name: 'Detail', fields: [{ name: 'body', type: 'string' }, { name: 'id', type: 'int64' }], key: ['id'] },
    { name: 'Event', fields: [{ name: 'at', type: 'timestamptz' }, { name: 'id', type: 'int64' }], key: ['id'] },
    { name: 'Label', fields: [{ name: 'id', type: 'int64' }, { name: 'name', type: 'string' }], key: ['id'] },
  ],
  relations: [{ name: 'event', from: 'Detail', to: 'Event' }],
})

class Catalogued extends MemoryEngine {
  readonly asked: string[][] = []
  constructor(
    private readonly sizes: Record<string, readonly [number, number]>,
    private readonly error?: unknown,
    name = 'engine',
  ) {
    super({ name })
  }
  async storageSizes(tables: readonly string[]): Promise<Map<string, readonly [number, number]>> {
    this.asked.push([...tables])
    if (this.error !== undefined) throw this.error
    return new Map(tables.filter((table) => table in this.sizes).map((table) => [table, this.sizes[table]!]))
  }
}

/** PostgreSQL columns of each entity, as the reference's default layout renders them. */
const COLUMNS: Record<string, Record<string, string>> = {
  Detail: { body: 'text', event_id: 'bigint', id: 'bigint' },
  Event: { at: 'timestamptz', id: 'bigint' },
  Label: { id: 'bigint', name: 'text' },
}

function material(engine: string, tables: Record<string, string>, id = 'source'): Record<string, unknown> {
  const columns = Object.fromEntries(Object.keys(tables).map((entity) => [entity, COLUMNS[entity]!]))
  return { id, engine, layout: { tables, columns } }
}

function placement(groups: Record<string, unknown>) {
  return loadMap({ contract: 3, model_version: MODEL.version, map_version: 1, groups }, { model: MODEL })
}

async function twoGroups(pg: MemoryEngine, other: MemoryEngine, recorder?: Recorder): Promise<Session> {
  const placed = placement({
    Detail: { source: material('pg', { Detail: 'details', Event: 'events' }) },
    Label: { source: material('other', { Label: 'labels' }) },
  })
  return Session.open(MODEL, placed, { pg, other }, recorder === undefined ? {} : { recorder })
}

describe('Session.measureStorage', () => {
  it('sums a group over its tables, one statement per engine, and the window carries it', async () => {
    const pg = new Catalogued({ details: [1_000, 100], events: [4_000, 900], labels: [70, 0] })
    const other = new Catalogued({ labels: [500, 50] }, undefined, 'other')
    const recorder = new Recorder(MODEL.version)
    const session = await twoGroups(pg, other, recorder)
    const measured = await session.measureStorage()
    expect(measured.unavailable).toEqual({})
    expect(measured.sizes.map((s) => [s.group, s.engine, s.totalBytes, s.secondaryIndexBytes])).toEqual([
      ['Detail', 'pg', 5_000, 1_000],
      ['Label', 'other', 500, 50],
    ])
    expect(pg.asked).toEqual([['details', 'events']])
    await session.save('Label', { id: 1n, name: 'x' })
    const body = (windowRecord(recorder.roll()!, MODEL)['groups'] as Record<string, Record<string, unknown>>)['Label']!
    expect(body['total_bytes']).toBe(500)
    expect(body['index_to_table_ratio']).toBe(50 / 450)
  })

  it('leaves the groups of an adapter without a catalogue unknown', async () => {
    const session = await twoGroups(new Catalogued({ details: [1, 0], events: [1, 0] }), new MemoryEngine({ name: 'other' }))
    const measured = await session.measureStorage()
    expect(measured.unavailable).toEqual({ Label: 'unsupported' })
    expect(measured.sizes.map((s) => s.group)).toEqual(['Detail'])
  })

  it.each([
    [Object.assign(new Error('denied'), { code: '42501' }), 'refused'],
    [new Error('Code: 497. DB::Exception: Not enough privileges'), 'refused'],
    [new Error('connection reset by peer'), 'failed'],
  ])('turns a refused or failed read into an unknown size (%s)', async (cause, reason) => {
    const error = new EngineError('storage sizes could not be read', { cause })
    const session = await twoGroups(new Catalogued({}, error), new Catalogued({ labels: [9, 0] }, undefined, 'other'))
    const measured = await session.measureStorage()
    expect(measured.unavailable).toEqual({ Detail: reason })
    expect(measured.sizes.map((s) => s.group)).toEqual(['Label'])
  })

  it('reports a table the map names that does not exist as missing, not empty', async () => {
    const session = await twoGroups(new Catalogued({ events: [10, 0] }), new Catalogued({ labels: [9, 0] }, undefined, 'other'))
    expect((await session.measureStorage()).unavailable).toEqual({ Detail: 'missing_table' })
  })

  it('lets adapter misuse through to the caller', async () => {
    const busy = new Catalogued({}, new ResourceBusy('another session owns the current transaction scope'))
    const session = await twoGroups(busy, new Catalogued({ labels: [9, 0] }, undefined, 'other'))
    await expect(session.measureStorage()).rejects.toThrow(ResourceBusy)
  })

  it('measures only the source while a staging maintains a copy', async () => {
    const pg = new Catalogued({ details: [1_000, 0], events: [2_000, 0] })
    const copy = new Catalogued({ copy_details: [7, 0], copy_events: [7, 0], labels: [5, 0] }, undefined, 'copy')
    const placed = placement({
      Detail: {
        source: material('pg', { Detail: 'details', Event: 'events' }),
        derived: [{ ...material('copy', { Detail: 'copy_details', Event: 'copy_events' }, 'stage'), lag_budget_ms: 30_000 }],
        also_write: ['stage'],
      },
      Label: { source: material('copy', { Label: 'labels' }) },
    })
    const session = await Session.open(MODEL, placed, { pg, copy })
    const measured = await session.measureStorage()
    expect(measured.sizes.map((s) => [s.group, s.totalBytes])).toEqual([['Detail', 3_000], ['Label', 5]])
    expect(copy.asked).toEqual([['labels']])
  })
})

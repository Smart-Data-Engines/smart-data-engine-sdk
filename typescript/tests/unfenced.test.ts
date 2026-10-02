/**
 * Map contract 6 off the wire: schema preparation and operator inspection beside a group without a
 * write generation. The shared vectors hold the loader, the session and the packets in both
 * languages; this holds the two entry points they do not reach, as `test_unfenced.py` does.
 */
import { describe, expect, it } from 'vitest'
import { InspectionContext, loadMap, MigrationRefused, prepareSchema } from '../src/index.js'
import type { Engine } from '../src/session.js'
import { modelFromNeutral } from '../src/testing/loader.js'
import { MemoryEngine, Recorded } from '../src/testing/memory.js'
import { WriteFence } from '../src/write-fence.js'
import { MemoryFences } from './_write-fence.js'

const PROJECT = '1'.repeat(32)

function model() {
  return modelFromNeutral({
    entities: [
      { name: 'Reading', fields: [{ name: 'celsius', type: 'int32' }, { name: 'id', type: 'int64' }], key: ['id'] },
      {
        name: 'Tick',
        fields: [{ name: 'at', type: 'int64' }, { name: 'price', type: 'int64' }, { name: 'venue', type: 'string' }],
        key: ['venue', 'at'],
      },
    ],
  })
}

function placement(logical: ReturnType<typeof model>) {
  return loadMap({
    contract: 6,
    project_id: PROJECT,
    model_version: logical.version,
    map_version: 1,
    groups: {
      Reading: {
        write_epoch: 2,
        source: {
          id: 'Reading@pg', engine: 'pg-main',
          layout: { tables: { Reading: 'reading' }, columns: { Reading: { id: 'bigint', celsius: 'integer' } } },
        },
      },
      Tick: {
        source: {
          id: 'Tick@book', engine: 'book-1',
          layout: { tables: { Tick: 'tick' }, columns: { Tick: { venue: 'text', at: 'bigint', price: 'bigint' } } },
        },
      },
    },
  }, { model: logical })
}

/** Give `engine` write fences that start unprepared, and return their backends by table. */
function fenced(engine: MemoryEngine): Map<string, MemoryFences> {
  const backends = new Map<string, MemoryFences>()
  Object.assign(engine, {
    writeFence(table: string, options: { projectId: string }): WriteFence {
      if (!backends.has(table)) backends.set(table, new MemoryFences())
      return new WriteFence(backends.get(table)!, table, options)
    },
  })
  return backends
}

function engines(bookFences: boolean) {
  const journal = new Recorded()
  const pg = new MemoryEngine({ dialect: 'postgres', name: 'pg-main', journal })
  const book = new MemoryEngine({ dialect: 'orderbook', name: 'book-1', journal, canKeepBookkeeping: false })
  const backends = fenced(pg)
  if (bookFences) fenced(book)
  return { engines: { 'pg-main': pg, 'book-1': book } as Record<string, MemoryEngine>, backends, journal }
}

describe('map contract 6 beside a group without a write generation', () => {
  it('prepares a generation only where the map carries one', async () => {
    const logical = model()
    const { engines: all, backends, journal } = engines(false)
    await prepareSchema(logical, placement(logical), all as unknown as Record<string, Engine>, { projectId: PROJECT })
    expect(journal.calls.map((call) => call.call)).toEqual(['ensure_schema', 'ensure_schema'])
    expect([...backends.keys()]).toEqual(['reading'])
    expect(backends.get('reading')!.column).toBe('valid')
    expect(all['book-1']!.tables).toEqual({ tick: [] })
  })

  it('refuses an engine that fences before creating anything', async () => {
    const logical = model()
    const { engines: all, backends, journal } = engines(true)
    await expect(prepareSchema(logical, placement(logical), all as unknown as Record<string, Engine>,
      { projectId: PROJECT })).rejects.toThrow('carries no write generation on book-1')
    expect(journal.calls).toEqual([])
    expect(backends.size).toBe(0)
    expect(all['pg-main']!.tables).toEqual({})
    expect(all['book-1']!.tables).toEqual({})
  })

  it('needs no engine for that group to inspect the others', () => {
    const logical = model()
    const { engines: all } = engines(false)
    const map = placement(logical)
    const context = new InspectionContext(logical, map, { 'pg-main': all['pg-main']! } as unknown as Record<string, Engine>,
      { projectId: PROJECT })
    expect(context.engineNamed('pg-main')).toBe(all['pg-main'])
    expect(() => new InspectionContext(logical, map, { 'book-1': all['book-1']! } as unknown as Record<string, Engine>,
      { projectId: PROJECT })).toThrow(MigrationRefused)
    expect(() => new InspectionContext(logical, map, { 'book-1': all['book-1']! } as unknown as Record<string, Engine>,
      { projectId: PROJECT })).toThrow('missing engines ["pg-main"]')
  })
})

/** The physical design vocabulary: the parts no shared vector can reach, in this runtime. */
import { describe, expect, it } from 'vitest'
import { EngineError, MapError } from '../src/errors.js'
import { loadMap } from '../src/placement.js'
import type { PhysicalLayout } from '../src/placement.js'
import {
  CLICKHOUSE_METHODS,
  INDEX_METHODS,
  POSTGRES_METHODS,
  TEMPORAL_TYPES,
  capabilities,
  declaredTables,
  describeFinding,
  parseIdentifierList,
  parsePartitionKey,
  refuseFindings,
} from '../src/physical.js'
import { schemaStatements } from '../src/schema.js'

describe('reading the catalogue back', () => {
  it.each([
    ['station, at', ['station', 'at']],
    ['at', ['at']],
    ['select, order', ['select', 'order']],
    ['`null`, `ząb`', ['null', 'ząb']],
    ['`a b`, c', ['a b', 'c']],
    ['`tick\\`tock`', ['tick`tock']],
    ['`back\\\\slash`', ['back\\slash']],
    ['', []],
  ])('parses %j into names rather than predicting a string', (text, names) => {
    expect(parseIdentifierList(text)).toEqual(names)
  })

  it.each(['`unterminated', '`dangling\\', 'a,b', 'a, ', 'a, , b', 'toYYYYMM(at)', '1abc'])(
    'refuses %j, which is not evidence that the table matches',
    (text) => {
      expect(() => parseIdentifierList(text)).toThrow()
    },
  )

  it('reads a partition key as one function of one name', () => {
    expect(parsePartitionKey('')).toBeNull()
    expect(parsePartitionKey('toYYYYMM(at)')).toEqual(['toYYYYMM', 'at'])
    expect(parsePartitionKey('toDate(`posted day`)')).toEqual(['toDate', 'posted day'])
    for (const text of ['at', 'toYYYYMM(toDate(at))', "toYYYYMM(at, 'UTC')", 'toYYYYMM(a, b)']) {
      expect(() => parsePartitionKey(text)).toThrow()
    }
  })
})

describe('what a dialect is offered', () => {
  it('derives capabilities from the tables the renderer reads, key for key with the reference', () => {
    const postgres = capabilities('postgres')
    const clickhouse = capabilities('clickhouse')
    expect(Object.keys(postgres['index_methods'] as object).sort()).toEqual([...POSTGRES_METHODS])
    expect(Object.keys(clickhouse['index_methods'] as object).sort()).toEqual([...CLICKHOUSE_METHODS])
    expect(INDEX_METHODS).toEqual(['bloom_filter', 'brin', 'btree', 'minmax', 'set'])
    expect(TEMPORAL_TYPES).toEqual(['date', 'timestamptz'])
    expect(postgres['partition_granularities']).toEqual([])
    expect(clickhouse['partition_granularities']).toEqual(['day', 'month', 'year'])
    expect((clickhouse['index_methods'] as Record<string, unknown>)['set']).toEqual({
      granularity: [1, 1024], columns: 1, max_rows: [1, 65536],
    })
    expect(capabilities('orderbook')).toEqual({ physical_design: false })
  })
})

function layout(design: Partial<PhysicalLayout> = {}): PhysicalLayout {
  return {
    tables: { Reading: 'reading', Archive: 'archive' },
    columns: {
      Reading: { station: 'String', at: "DateTime64(6, 'UTC')", t: 'Float64' },
      Archive: { sensor: 'String', day: 'Date32' },
    },
    indexes: [],
    partitionBy: {},
    ...design,
  }
}

describe('what a layout declares', () => {
  it('carries the physical key, partition and indexes, tables and indexes in code point order', () => {
    const declared = declaredTables(
      layout({
        keyOrder: { Reading: ['at', 'station'] },
        partitionBy: { Reading: { field: 'at', granularity: 'month' } },
        indexes: [
          { entity: 'Reading', name: 'z_t', columns: ['t'], method: 'minmax', granularity: 4 },
          { entity: 'Reading', name: 'a_station', columns: ['station'], method: 'set', granularity: 2, max_rows: 100 },
        ],
      }),
      { Reading: ['station', 'at'], Archive: ['sensor', 'day'] },
    )
    expect(declared.map((entry) => entry.table)).toEqual(['archive', 'reading'])
    const [archive, reading] = declared
    expect(archive!.key).toEqual(['sensor', 'day'])
    expect(archive!.partition).toBeNull()
    expect(reading!.key).toEqual(['at', 'station'])
    expect(reading!.partition).toEqual(['toYYYYMM', 'at'])
    expect(reading!.indexes.map((index) => [index.name, index.typeFull, index.granularity])).toEqual([
      ['a_station', 'set(100)', 2],
      ['z_t', 'minmax', 4],
    ])
  })

  it('holds a hand-built layout to the key rules when it renders', () => {
    const keys = { Reading: ['station', 'at'], Archive: ['sensor', 'day'] }
    const reordered = layout({ keyOrder: { Reading: ['at'] } })
    expect(() => schemaStatements(reordered, { keys, dialect: 'clickhouse' })).toThrow(EngineError)
    expect(() => schemaStatements(reordered, { keys, dialect: 'postgres' })).toThrow(/must be a permutation of the key/)
    const offKey = layout({ partitionBy: { Reading: { field: 't', granularity: 'day' } } })
    expect(() => schemaStatements(offKey, { keys, dialect: 'clickhouse' })).toThrow(/outside the key/)
    const twoColumns = layout({
      indexes: [{ entity: 'Reading', name: 'i', columns: ['t', 'station'], method: 'minmax', granularity: 1 }],
    })
    expect(() => schemaStatements(twoColumns, { keys, dialect: 'clickhouse' })).toThrow(/exactly one column/)
  })

  it('refuses a non-permutation at render time for a map loaded without its model', () => {
    const placement = loadMap({
      contract: 5, project_id: '1'.repeat(32), model_version: '0'.repeat(16), map_version: 1,
      groups: { Reading: { write_epoch: 1, source: { id: 'r', engine: 'ch', layout: {
        tables: { Reading: 'reading' },
        columns: { Reading: { station: 'String', at: "DateTime64(6, 'UTC')" } },
        key_order: { Reading: ['at'] },
      } } } },
    })
    const loaded = placement.groups['Reading']!.source.layout
    expect(loaded.keyOrder).toEqual({ Reading: ['at'] })
    expect(() => schemaStatements(loaded, { keys: { Reading: ['station', 'at'] }, dialect: 'clickhouse' }))
      .toThrow(/permutation/)
    expect(Object.isFrozen(loaded.keyOrder)).toBe(true)
  })

  it('refuses the new keys in an earlier contract rather than ignoring them', () => {
    const base = (extra: Record<string, unknown>) => ({
      contract: 4, project_id: '1'.repeat(32), model_version: '0'.repeat(16), map_version: 1,
      groups: { Reading: { write_epoch: 1, source: { id: 'r', engine: 'pg', layout: {
        tables: { Reading: 'reading' }, columns: { Reading: { station: 'text' } }, ...extra,
      } } } },
    })
    expect(() => loadMap(base({ key_order: { Reading: ['station'] } }))).toThrow(/key_order is map contract 5/)
    expect(() => loadMap(base({ partition_by: { Reading: { field: 'station', granularity: 'day' } } })))
      .toThrow(/Partitioning is map contract 5/)
    expect(() => loadMap(base({ indexes: [{ entity: 'Reading', name: 'i', columns: ['station'], method: 'btree' }] })))
      .toThrow(MapError)
  })
})

describe('provisioning refuses, a session reports', () => {
  it('raises only when there is something to say, naming the table and the aspect', () => {
    refuseFindings([], EngineError)
    const finding = { table: 'reading', aspect: 'sort key', declared: '["at","station"]', found: '["station","at"]' }
    expect(describeFinding(finding)).toBe('reading: sort key is ["station","at"] and the map declares ["at","station"]')
    expect(() => refuseFindings([finding], EngineError)).toThrow(/reading: sort key.*IF NOT EXISTS/)
  })
})

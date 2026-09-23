/**
 * A physical design applied to real engines and read back from their own catalogues, in this
 * runtime: created as declared, refused where a person provisions over another design, reported -
 * not failed - by a running session, and "unverified" rather than a failed start for a restricted
 * ClickHouse login that cannot read the index catalogue.
 */
import { describe, expect, it } from 'vitest'
import { buildModel, entity, EngineError, loadMap, prepareSchema, Session, T } from '../src/index.js'
import type { LogicalModel, PlacementMap } from '../src/index.js'
import { type Dialect } from './_generation-engines.js'
import { withRoles, type Roles } from './_runtime-roles.js'

const PROJECT = '1'.repeat(32)
const COLUMNS: Record<Dialect, Record<string, string>> = {
  postgres: { at: 'timestamptz', humidity: 'integer', station: 'text', temperature: 'double precision' },
  clickhouse: { at: "DateTime64(6, 'UTC')", humidity: 'Int32', station: 'String', temperature: 'Float64' },
}
const DESIGN: Record<Dialect, Record<string, unknown>> = {
  postgres: {
    key_order: { Reading: ['at', 'station'] },
    indexes: [
      { entity: 'Reading', name: 'reading_at_brin', columns: ['at'], method: 'brin' },
      { entity: 'Reading', name: 'reading_temperature', columns: ['temperature'] },
    ],
  },
  clickhouse: {
    key_order: { Reading: ['at', 'station'] },
    partition_by: { Reading: { field: 'at', granularity: 'month' } },
    indexes: [
      { entity: 'Reading', name: 'reading_temperature', columns: ['temperature'], method: 'minmax', granularity: 4 },
      { entity: 'Reading', name: 'reading_humidity', columns: ['humidity'], method: 'set', granularity: 2, max_rows: 100 },
      { entity: 'Reading', name: 'reading_station', columns: ['station'], method: 'bloom_filter', granularity: 1 },
    ],
  },
}

function placement(dialect: Dialect, design: boolean, mapVersion = 1): { model: LogicalModel; placement: PlacementMap } {
  const model = buildModel([entity('Reading', {
    fields: { at: T.timestamptz, humidity: T.int32, station: T.string, temperature: T.float64 },
    key: ['station', 'at'],
  })])
  const layout = { tables: { Reading: 'readings' }, columns: { Reading: COLUMNS[dialect] }, ...(design ? DESIGN[dialect] : {}) }
  return { model, placement: loadMap({ contract: 5, project_id: PROJECT, model_version: model.version, map_version: mapVersion,
    groups: { Reading: { write_epoch: 1, source: { id: 's', engine: 'db', layout } } } }, { model }) }
}

async function catalogue(roles: Roles, dialect: Dialect): Promise<Record<string, unknown>> {
  if (dialect === 'postgres') {
    const key = await roles.statement(
      'SELECT a.attname FROM pg_index i JOIN pg_class t ON t.oid = i.indrelid ' +
      'JOIN pg_namespace n ON n.oid = t.relnamespace ' +
      'JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(num, pos) ON true ' +
      'JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.num ' +
      "WHERE i.indisprimary AND n.nspname = current_schema() AND t.relname = 'readings' ORDER BY k.pos")
    const indexes = await roles.statement(
      "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema() AND tablename = 'readings'")
    return { key: key.map((row) => row['attname']),
      indexes: Object.fromEntries(indexes.map((row) => [row['indexname'], row['indexdef']])) }
  }
  const [table] = await roles.statement(
    "SELECT sorting_key, partition_key FROM system.tables WHERE database = currentDatabase() AND name = 'readings'")
  const indexes = await roles.statement(
    'SELECT name, type_full, expr, toInt32(granularity) AS granularity FROM system.data_skipping_indices ' +
    "WHERE database = currentDatabase() AND table = 'readings' ORDER BY name")
  return { key: table!['sorting_key'], partition: table!['partition_key'],
    indexes: Object.fromEntries(indexes.map((row) => [row['name'], [row['type_full'], row['expr'], row['granularity']]])) }
}

describe.each(['postgres', 'clickhouse'] as const)('physical design on %s', (dialect) => {
  const live = process.env[dialect === 'postgres' ? 'SDE_POSTGRES_DSN' : 'SDE_CLICKHOUSE_DSN'] !== undefined

  it.skipIf(!live)('creates a designed layout as declared', async () => {
    await withRoles(dialect, async (roles) => {
      const { model, placement: map } = placement(dialect, true)
      await prepareSchema(model, map, { db: roles.operator }, { projectId: PROJECT })
      const found = await catalogue(roles, dialect)
      if (dialect === 'postgres') {
        expect(found['key']).toEqual(['at', 'station'])
        expect((found['indexes'] as Record<string, string>)['reading_at_brin']).toContain('USING brin (at)')
        expect((found['indexes'] as Record<string, string>)['reading_temperature']).toContain('USING btree (temperature)')
      } else {
        expect(found).toEqual({ key: 'at, station', partition: 'toYYYYMM(at)', indexes: {
          reading_humidity: ['set(100)', 'humidity', 2],
          reading_station: ['bloom_filter', 'station', 1],
          reading_temperature: ['minmax', 'temperature', 4],
        } })
      }
      const layout = map.groups['Reading']!.source.layout
      expect(await roles.operator.validateSchema(layout, { keys: { Reading: ['station', 'at'] } })).toEqual([])
    })
  })

  it.skipIf(!live)('refuses another design at provisioning and reports it from a running session', async () => {
    await withRoles(dialect, async (roles) => {
      const first = placement(dialect, false)
      await prepareSchema(first.model, first.placement, { db: roles.operator }, { projectId: PROJECT })
      const second = placement(dialect, true, 2)
      const aspect = dialect === 'postgres' ? 'primary key' : 'sort key'
      await expect(prepareSchema(second.model, second.placement, { db: roles.operator }, { projectId: PROJECT }))
        .rejects.toThrow(new RegExp(`readings: ${aspect}`))
      await expect(prepareSchema(second.model, second.placement, { db: roles.operator }, { projectId: PROJECT }))
        .rejects.toThrow(EngineError)
      await roles.grant('readings')
      const session = await Session.open(second.model, second.placement, { db: roles.runtime }, { projectId: PROJECT })
      const aspects = Object.fromEntries(session.physical.map((finding) => [finding.aspect, finding.found]))
      expect(aspects[aspect]).toBe('["station","at"]')
      if (dialect === 'postgres') {
        // The refused provisioning did not build the index on the old table first.
        expect(aspects['index reading_at_brin']).toBe('absent')
      } else {
        expect(aspects['partition']).toBe('null')
        expect(aspects['index reading_temperature']).toMatch(/^unverified/)
      }
      const at = new Date('2026-09-23T12:00:00.123Z')
      await session.save('Reading', { station: 's1', at, humidity: 40, temperature: 21.5 })
      expect(await session.get('Reading', { station: 's1', at })).not.toBeNull()
    })
  })

  it.skipIf(!live)('reports nothing to a restricted session when the table matches', async () => {
    await withRoles(dialect, async (roles) => {
      const { model, placement: map } = placement(dialect, true)
      await prepareSchema(model, map, { db: roles.operator }, { projectId: PROJECT })
      await roles.grant('readings')
      if (dialect === 'clickhouse') {
        const [row] = await roles.statement("SELECT currentUser() AS me", true)
        await roles.statement(`GRANT SELECT ON system.data_skipping_indices TO \`${String(row!['me'])}\``)
      }
      const session = await Session.open(model, map, { db: roles.runtime }, { projectId: PROJECT })
      expect(session.physical).toEqual([])
    })
  })
})

describe('the partition rule on this ClickHouse', () => {
  it.skipIf(process.env['SDE_CLICKHOUSE_DSN'] === undefined)(
    'collapses one key inside one partition and not across two', async () => {
      await withRoles('clickhouse', async (roles) => {
        const counts: Record<string, number> = {}
        for (const [name, partition] of [['on_key', 'toYYYYMM(at)'], ['off_key', 'humidity']] as const) {
          await roles.statement(`CREATE TABLE ${name} (station String, at DateTime64(6, 'UTC'), humidity Int32) ` +
            `ENGINE = ReplacingMergeTree PARTITION BY ${partition} ORDER BY (station, at)`)
          for (const humidity of [10, 20]) {
            await roles.statement(`INSERT INTO ${name} VALUES ('s1', '2026-09-23 12:00:00.000000', ${humidity})`)
          }
          await roles.statement(`OPTIMIZE TABLE ${name} FINAL`)
          const [row] = await roles.statement(`SELECT toInt32(count()) AS n FROM ${name}`)
          counts[name] = Number(row!['n'])
        }
        expect(counts).toEqual({ on_key: 1, off_key: 2 })
      })
    })
})

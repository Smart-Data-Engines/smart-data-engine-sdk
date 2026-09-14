/** Native logical pages preserve UUID order, nullable ties, exact time bounds and decimal filters. */
import { expect, it } from 'vitest'
import { buildModel, colocationGroups, entity, hashIdentifiers, loadMap, Recorder, Session, T,
  Timestamp, type Row, type ScanOptions } from '../src/index.js'
import { withRoles } from './_runtime-roles.js'
import type { Dialect } from './_generation-engines.js'

const ids = [
  '00000000-0000-0001-0000-000000000000',
  '00000000-0000-0000-ffff-ffffffffffff',
  'ffffffff-ffff-ffff-0000-000000000000',
  '00000000-0000-0000-0000-000000000001',
  '00000000-0000-0000-0000-000000000002',
  '00000000-0000-0000-0000-000000000003',
]
const base = Timestamp.from('2026-09-14T00:00:00.123456Z')

async function fixture(dialect: Dialect, body: (session: Session, rows: Row[], recorder: Recorder) => Promise<void>, hashed = false) {
  await withRoles(dialect, async role => {
    let model = buildModel([entity('Event', { fields: {
      id: T.uuid, label: { ...T.string, nullable: true }, at: T.timestamptz, amount: T.decimal(12, 2),
    }, key: ['id'] })])
    let names
    if (hashed) ({ model, names } = hashIdentifiers(model, Buffer.alloc(32, 'q')))
    const group = colocationGroups(model)[0]!, spec = model.entities[0]!
    const types: Record<string, string> = dialect === 'postgres'
      ? { uuid: 'uuid', string: 'text', timestamptz: 'timestamptz', 'decimal(12,2)': 'numeric(12,2)' }
      : { uuid: 'UUID', string: 'String', timestamptz: "DateTime64(6, 'UTC')", 'decimal(12,2)': 'Decimal(12, 2)' }
    const columns = Object.fromEntries(spec.fields.map(field => [field.name,
      dialect === 'clickhouse' && field.nullable ? 'Nullable(' + types[field.type]! + ')' : types[field.type]!]))
    const placement = loadMap({ contract: 3, model_version: model.version, map_version: 1, groups: {
      [group.name]: { source: { id: 'source', engine: 'db', layout: {
        tables: { [spec.name]: 'query_events' }, columns: { [spec.name]: columns },
      } } },
    } }, { model })
    await role.operator.ensureSchema(placement.groups[group.name]!.source.layout, { keys: { [spec.name]: spec.key } })
    await role.grant('query_events')
    const recorder = new Recorder(model.version)
    const session = await Session.open(model, placement, { db: role.runtime }, { recorder, ...(names ? { names } : {}) })
    const labels = ['z', null, 'é', 'a', null, 'a']
    const rows = ids.map((id, index) => ({ id, label: labels[index]!,
      at: Timestamp.fromEpochMicroseconds(base.epochMicroseconds + BigInt(Math.floor(index / 2))), amount: index + '.25' }))
    await session.saveMany('Event', rows)
    recorder.roll()
    await body(session, rows, recorder)
  })
}

async function pages(session: Session, options: ScanOptions): Promise<Row[]> {
  const result: Row[] = []
  let after: Readonly<Row> | null = null
  for (let attempt = 0; attempt < 20; attempt++) {
    const page = await session.scan('Event', { ...options, after })
    expect(page.rows.length).toBeLessThanOrEqual(options.limit ?? 100)
    result.push(...page.rows)
    if (page.nextAfter === null) return result
    after = page.nextAfter
  }
  throw new Error('pagination did not terminate')
}
function compare(left: string, right: string) { return left < right ? -1 : left > right ? 1 : 0 }

for (const dialect of ['postgres', 'clickhouse'] as const) {
  const enabled = process.env[dialect === 'postgres' ? 'SDE_POSTGRES_DSN' : 'SDE_CLICKHOUSE_DSN'] !== undefined
  for (const limit of [1, 2, 3, 6]) it.skipIf(!enabled)(dialect + ' preserves every UUID on pages of ' + limit, async () => {
    await fixture(dialect, async (session, rows) => {
      const ordered = [...rows].sort((a, b) => compare(a['id'] as string, b['id'] as string))
      expect(await pages(session, { limit })).toEqual(ordered)
      expect(await pages(session, { limit, descending: true })).toEqual([...ordered].reverse())
    })
  }, 20000)

  for (const hashed of [false, true]) it.skipIf(!enabled)(dialect + ' keeps nullable ties with hashing=' + hashed, async () => {
    await fixture(dialect, async (session, rows) => {
      for (const descending of [false, true]) {
        const sign = descending ? -1 : 1
        const present = rows.filter(row => row['label'] !== null).sort((a, b) => sign *
          (compare(a['label'] as string, b['label'] as string) || compare(a['id'] as string, b['id'] as string)))
        const absent = rows.filter(row => row['label'] === null).sort((a, b) => sign * compare(a['id'] as string, b['id'] as string))
        expect(await pages(session, { orderBy: 'label', descending, limit: 1 })).toEqual([...present, ...absent])
      }
      expect(await session.count('Event', { where: { label: null } })).toBe(2n)
    }, hashed)
  }, 20000)

  it.skipIf(!enabled)(dialect + ' combines exact half-open times and equality without losing ties', async () => {
    await fixture(dialect, async (session, rows, recorder) => {
      const bounds = { field: 'at', low: base, high: Timestamp.fromEpochMicroseconds(base.epochMicroseconds + 3n) }
      const expected = rows.filter(row => row['label'] === 'a').sort((a, b) => {
        const x = (a['at'] as Timestamp).epochMicroseconds, y = (b['at'] as Timestamp).epochMicroseconds
        return x < y ? -1 : x > y ? 1 : compare(a['id'] as string, b['id'] as string)
      })
      expect(await pages(session, { where: { label: 'a' }, bounds, orderBy: 'at', limit: 1 })).toEqual(expected)
      expect(await session.count('Event', { where: { label: 'a' }, bounds })).toBe(BigInt(expected.length))
      const window = recorder.roll()!
      expect(window.shapes.find(shape => shape.kind === 'aggregate')?.rows).toBe(1)
    })
  }, 20000)

  it.skipIf(!enabled)(dialect + ' keeps decimal bounds finer than the stored scale', async () => {
    await fixture(dialect, async (session, rows) => {
      const bounds = { field: 'amount', low: '1.249', high: '3.251' }
      expect(await pages(session, { bounds, orderBy: 'amount', limit: 1 })).toEqual(rows.slice(1, 4))
      expect(await session.count('Event', { bounds })).toBe(3n)
    })
  }, 20000)
}

for (const dialect of ['postgres', 'clickhouse'] as const) {
  const enabled = process.env[dialect === 'postgres' ? 'SDE_POSTGRES_DSN' : 'SDE_CLICKHOUSE_DSN'] !== undefined
  it.skipIf(!enabled)(dialect + ' summarizes decimal values and empty filters exactly', async () => {
    await fixture(dialect, async (session, _rows, recorder) => {
      expect(await session.summarize('Event', 'amount', { where: { label: 'a' }, meanScale: 3 })).toEqual({
        count: 2n, nonNullCount: 2n, minimum: '3.25', maximum: '5.25', total: '8.50', mean: '4.250',
      })
      expect(await session.summarize('Event', 'amount', { where: { label: 'absent' } })).toEqual({
        count: 0n, nonNullCount: 0n, minimum: null, maximum: null, total: null, mean: null,
      })
      const window = recorder.roll()!
      expect(window.shapes[0]!.rows).toBe(2)
    })
  }, 20000)

  it.skipIf(!enabled)(dialect + ' widens int64 before sum and counts non-null values', async () => {
    await withRoles(dialect, async role => {
      const model = buildModel([entity('Number', { fields: {
        id: T.int64, value: { ...T.int64, nullable: true },
      }, key: ['id'] })])
      const placement = loadMap({ contract: 3, model_version: model.version, map_version: 1, groups: {
        Number: { source: { id: 'source', engine: 'db', layout: { tables: { Number: 'numbers_query' },
          columns: { Number: { id: dialect === 'postgres' ? 'bigint' : 'Int64',
            value: dialect === 'postgres' ? 'bigint' : 'Nullable(Int64)' } } } } },
      } }, { model })
      await role.operator.ensureSchema(placement.groups['Number']!.source.layout, { keys: { Number: ['id'] } })
      await role.grant('numbers_query')
      const session = await Session.open(model, placement, { db: role.runtime })
      await session.saveMany('Number', [{ id: 1n, value: 2n**63n-1n }, { id: 2n, value: 2n**63n-1n }, { id: 3n, value: null }])
      expect(await session.summarize('Number', 'value')).toEqual({
        count: 3n, nonNullCount: 2n, minimum: 2n**63n-1n, maximum: 2n**63n-1n,
        total: 2n**64n-2n, mean: '9223372036854775807.000000',
      })
      expect(await session.summarize('Number', 'value', { where: { id: 3n } })).toEqual({
        count: 1n, nonNullCount: 0n, minimum: null, maximum: null, total: null, mean: null,
      })
    })
  }, 20000)
}

for (const dialect of ['postgres', 'clickhouse'] as const) {
  const enabled = process.env[dialect === 'postgres' ? 'SDE_POSTGRES_DSN' : 'SDE_CLICKHOUSE_DSN'] !== undefined
  it.skipIf(!enabled)(dialect + ' reads timestamps in UTC with a different connection zone', async () => {
    await withRoles(dialect, async role => {
      const { PostgresEngine } = await import('../src/engines/postgres.js')
      const { ClickHouseEngine } = await import('../src/engines/clickhouse.js')
      const model = buildModel([entity('Timed', { fields: { id: T.int64, at: T.timestamp }, key: ['id'] })])
      const placement = loadMap({ contract: 3, model_version: model.version, map_version: 1, groups: {
        Timed: { source: { id: 'source', engine: 'db', layout: { tables: { Timed: 'timed_query' }, columns: {
          Timed: { id: dialect === 'postgres' ? 'bigint' : 'Int64', at: dialect === 'postgres' ? 'timestamp' : 'DateTime64(6)' },
        } } } },
      } }, { model })
      await role.operator.ensureSchema(placement.groups['Timed']!.source.layout, { keys: { Timed: ['id'] } })
      await role.grant('timed_query')
      const rows = [
        { id: 1n, at: Timestamp.from('2026-09-14T10:00:00.123456Z') },
        { id: 2n, at: Timestamp.from('2026-09-14T10:00:00.123457Z') },
      ]
      await role.operator.insertMany('timed_query', rows)
      const dsn = new URL(role.runtimeDsn)
      if (dialect === 'postgres') dsn.searchParams.set('options', dsn.searchParams.get('options') + ' -cTimeZone=America/New_York')
      else dsn.searchParams.set('session_timezone', 'America/New_York')
      const engine = dialect === 'postgres' ? new PostgresEngine(dsn.toString()) : new ClickHouseEngine(dsn.toString())
      await engine.connect()
      try {
        const session = await Session.open(model, placement, { db: engine })
        const first = await session.scan('Timed', { bounds: { field: 'at',
          low: '2026-09-14T12:00:00.123456+02:00', high: '2026-09-14T10:00:00.123458Z' }, orderBy: 'at', limit: 1 })
        expect(first.rows).toEqual([rows[0]])
        expect((await session.scan('Timed', { orderBy: 'at', after: first.nextAfter })).rows).toEqual([rows[1]])
      } finally { await engine.close() }
    })
  }, 20000)
}

for (const dialect of ['postgres', 'clickhouse'] as const) {
  const enabled = process.env[dialect === 'postgres' ? 'SDE_POSTGRES_DSN' : 'SDE_CLICKHOUSE_DSN'] !== undefined
  it.skipIf(!enabled)(dialect + ' preserves nonfinite floats and excludes NaN from ranges', async () => {
    await withRoles(dialect, async role => {
      const model = buildModel([entity('FloatValue', { fields: { id: T.int64, value: T.float64 }, key: ['id'] })])
      const placement = loadMap({ contract: 3, model_version: model.version, map_version: 1, groups: {
        FloatValue: { source: { id: 'source', engine: 'db', layout: { tables: { FloatValue: 'float_query' },
          columns: { FloatValue: { id: dialect === 'postgres' ? 'bigint' : 'Int64',
            value: dialect === 'postgres' ? 'double precision' : 'Float64' } } } } },
      } }, { model })
      await role.operator.ensureSchema(placement.groups['FloatValue']!.source.layout, { keys: { FloatValue: ['id'] } })
      await role.grant('float_query')
      await role.statement(dialect === 'postgres'
        ? "INSERT INTO float_query VALUES (1,0.25),(2,'NaN'),(3,'Infinity'),(4,'-Infinity')"
        : "INSERT INTO float_query SELECT 1,0.25 UNION ALL SELECT 2,toFloat64('nan') UNION ALL SELECT 3,toFloat64('inf') UNION ALL SELECT 4,toFloat64('-inf')")
      const session = await Session.open(model, placement, { db: role.runtime })
      expect((await session.scan('FloatValue')).rows).toEqual([
        { id: 1n, value: 0.25 }, { id: 2n, value: NaN }, { id: 3n, value: Infinity }, { id: 4n, value: -Infinity },
      ])
      expect((await session.scan('FloatValue', { bounds: { field: 'value', low: 0 } })).rows.map(row => row['id'])).toEqual([1n, 3n])
      expect(await session.count('FloatValue', { bounds: { field: 'value', low: 0 } })).toBe(2n)
      await role.grant('float_query', true)
      await expect(session.scan('FloatValue')).rejects.toThrow()
      await expect(session.count('FloatValue')).rejects.toThrow()
    })
  }, 20000)
  it.skipIf(!enabled)(dialect + ' excludes generation metadata from page and cursor', async () => {
    const { prepareSchema } = await import('../src/index.js')
    await withRoles(dialect, async role => {
      const model = buildModel([entity('Generation', { fields: { id: T.int64 }, key: ['id'] })])
      const placement = loadMap({ contract: 4, project_id: '1'.repeat(32), model_version: model.version,
        map_version: 1, groups: { Generation: { write_epoch: 1, source: { id: 'source', engine: 'db',
          layout: { tables: { Generation: 'generation_query' }, columns: { Generation: { id: dialect === 'postgres' ? 'bigint' : 'Int64' } } },
        } } } }, { model })
      await prepareSchema(model, placement, { db: role.operator }, { projectId: '1'.repeat(32) })
      await role.grant('generation_query')
      const session = await Session.open(model, placement, { db: role.runtime }, { projectId: '1'.repeat(32) })
      await session.saveMany('Generation', [{ id: 1n }, { id: 2n }])
      const first = await session.scan('Generation', { limit: 1 })
      expect(first.rows).toEqual([{ id: 1n }])
      expect(first.nextAfter).toEqual({ id: 1n })
      expect(await session.count('Generation')).toBe(2n)
      expect((await session.summarize('Generation', 'id')).total).toBe(3n)
    })
  }, 20000)
}

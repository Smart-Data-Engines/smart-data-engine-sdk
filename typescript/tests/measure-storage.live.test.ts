/**
 * What a least-privilege runtime login can read of its tables' size, and `Session.measureStorage`
 * on top of it - both engines, the mirror of the reference's live size tests. PostgreSQL needs no
 * grant; ClickHouse refuses `system.parts` until the column grant in `STORAGE_COLUMNS`, and the
 * session turns the refusal into an unknown size rather than an exception.
 */
import { describe, expect, it } from 'vitest'
import {
  buildModel,
  entity,
  loadMap,
  prepareSchema,
  Recorder,
  Session,
  T,
  windowRecord,
} from '../src/index.js'
import { STORAGE_COLUMNS } from '../src/engines/clickhouse.js'
import { QUOTE } from '../src/schema.js'
import { type Dialect } from './_generation-engines.js'
import { withRoles, type Roles } from './_runtime-roles.js'

const PROJECT = '1'.repeat(32)

async function tables(roles: Roles, dialect: Dialect): Promise<void> {
  if (dialect === 'postgres') {
    await roles.statement('CREATE TABLE "filled" (k bigint PRIMARY KEY, v bigint)')
    await roles.statement('CREATE INDEX "filled_v" ON "filled" (v)')
    await roles.statement('INSERT INTO "filled" SELECT i, i % 97 FROM generate_series(1, 5000) i')
    await roles.statement('CREATE TABLE "empty" (k bigint PRIMARY KEY)')
  } else {
    await roles.statement(
      'CREATE TABLE `filled` (k Int64, v Int64, INDEX filled_v v TYPE minmax GRANULARITY 1) ' +
        'ENGINE = MergeTree ORDER BY k',
    )
    await roles.statement('INSERT INTO `filled` SELECT number, number % 97 FROM numbers(5000)')
    await roles.statement('CREATE TABLE `empty` (k Int64) ENGINE = MergeTree ORDER BY k')
  }
  await roles.grant('filled')
  await roles.grant('empty')
}

async function grantParts(roles: Roles): Promise<void> {
  const quote = QUOTE['clickhouse'] as (value: string) => string
  const username = new URL(roles.runtimeDsn).username
  await roles.statement(`GRANT SELECT(${STORAGE_COLUMNS.join(', ')}) ON system.parts TO ${quote(username)}`)
}

type Sized = { storageSizes(tables: readonly string[]): Promise<ReadonlyMap<string, readonly [number, number]>> }

describe.each(['postgres', 'clickhouse'] as const)('storage sizes on %s', (dialect) => {
  const live = process.env[dialect === 'postgres' ? 'SDE_POSTGRES_DSN' : 'SDE_CLICKHOUSE_DSN'] !== undefined

  it.skipIf(!live)('reads what the administrator reads, and nothing of a missing table', async () => {
    await withRoles(dialect, async (roles) => {
      await tables(roles, dialect)
      const runtime = roles.runtime as unknown as Sized
      const operator = roles.operator as unknown as Sized
      if (dialect === 'clickhouse') {
        await expect(runtime.storageSizes(['filled'])).rejects.toThrow(/storage sizes could not be read/)
        await grantParts(roles)
      }
      const read = await runtime.storageSizes(['filled', 'empty', 'missing'])
      const admin = await operator.storageSizes(['filled', 'empty', 'missing'])
      expect([...read.entries()].sort()).toEqual([...admin.entries()].sort())
      expect([...read.keys()].sort()).toEqual(['empty', 'filled'])
      const [total, secondary] = read.get('filled')!
      expect(total).toBeGreaterThan(secondary)
      expect(secondary).toBeGreaterThan(0)
      expect(read.get('empty')![1]).toBe(0)
      if (dialect === 'clickhouse') expect(read.get('empty')![0]).toBe(0)
      expect((await runtime.storageSizes([])).size).toBe(0)
    })
  })

  it.skipIf(!live)('measures a session group and the window carries it', async () => {
    await withRoles(dialect, async (roles) => {
      const model = buildModel([entity('Event', { fields: { id: T.int64 }, key: ['id'] })])
      const raw = {
        contract: 4, project_id: PROJECT, model_version: model.version, map_version: 7,
        groups: { Event: { write_epoch: 1, source: { id: 'source', engine: 'db', layout: {
          tables: { Event: 'events' }, columns: { Event: { id: dialect === 'postgres' ? 'bigint' : 'Int64' } } } } } },
      }
      const placement = loadMap(raw, { model })
      await prepareSchema(model, placement, { db: roles.operator }, { projectId: PROJECT })
      await roles.grant('events')
      const recorder = new Recorder(model.version)
      const session = await Session.open(model, placement, { db: roles.runtime }, { projectId: PROJECT, recorder })
      for (const start of [0, 1000]) {
        await session.saveMany('Event', Array.from({ length: 1000 }, (_, index) => ({ id: BigInt(start + index) })))
      }
      if (dialect === 'clickhouse') {
        const refused = await session.measureStorage()
        expect(refused.sizes).toEqual([])
        expect(refused.unavailable).toEqual({ Event: 'refused' })
        await grantParts(roles)
      }
      const measured = await session.measureStorage()
      expect(measured.unavailable).toEqual({})
      expect(measured.sizes).toHaveLength(1)
      const size = measured.sizes[0]!
      const [adminTotal, adminSecondary] = (await (roles.operator as unknown as Sized).storageSizes(['events'])).get('events')!
      expect([size.group, size.engine, size.materialization]).toEqual(['Event', 'db', 'source'])
      expect([size.totalBytes, size.secondaryIndexBytes]).toEqual([adminTotal, adminSecondary])
      expect(size.totalBytes).toBeGreaterThan(0)
      const window = recorder.roll()!
      const body = (windowRecord(window, model)['groups'] as Record<string, Record<string, unknown>>)['Event']!
      expect(body['total_bytes']).toBe(size.totalBytes)
      expect(body['missing']).not.toContain('total_bytes')
      await session.close()
      await expect(session.measureStorage()).rejects.toThrow()
    })
  })
})

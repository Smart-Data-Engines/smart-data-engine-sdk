/** Native application inserts on disposable schemas and restricted runtime connections. */
import { expect, it } from 'vitest'
import { buildModel, BulkWriteRefused, EngineError, entity, loadMap, prepareSchema, Session, T,
  Timestamp, WRITE_EPOCH_COLUMN } from '../src/index.js'
import { withRoles, type Roles } from './_runtime-roles.js'
import { fixture, type Dialect } from './_generation-engines.js'

async function native(role: Roles, dialect: Dialect, json = false) {
  const model = buildModel([entity('Event', { fields: { id: T.int64, value: json ? T.json : T.timestamptz }, key: ['id'] })])
  const placement = loadMap({ contract: 3, model_version: model.version, map_version: 1, groups: {
    Event: { source: { id: 'source', engine: 'db', layout: { tables: { Event: 'bulk_events' },
      columns: { Event: { id: dialect === 'postgres' ? 'bigint' : 'Int64',
        value: json ? 'jsonb' : dialect === 'postgres' ? 'timestamptz' : "DateTime64(6, 'UTC')" } } } } },
  } }, { model })
  await role.operator.ensureSchema(placement.groups['Event']!.source.layout, { keys: { Event: ['id'] } })
  await role.grant('bulk_events')
  return Session.open(model, placement, { db: role.runtime })
}

for (const dialect of ['postgres', 'clickhouse'] as const) {
  const live = process.env[dialect === 'postgres' ? 'SDE_POSTGRES_DSN' : 'SDE_CLICKHOUSE_DSN'] !== undefined
  it.skipIf(!live)(`writes every value of a full native ${dialect} batch with exact microseconds`, async () => {
    await withRoles(dialect, async role => {
      const session = await native(role, dialect)
      const base = Timestamp.from('2026-09-14T00:00:00.123456Z').epochMicroseconds
      const rows = Array.from({ length: 1000 }, (_, i) => ({ id: BigInt(i), value: Timestamp.fromEpochMicroseconds(base + BigInt(i)) }))
      await session.saveMany('Event', rows)
      expect(await role.operator.count('bulk_events')).toBe(1000)
      expect(await role.operator.keyRange('bulk_events', ['id'])).toEqual(rows)
      expect(await session.get('Event', { id: 999n })).toEqual(rows[999])
      await expect(session.saveMany('Event', [{ id: 1001n, value: rows[0]!.value }, { id: 1002n }])).rejects.toThrow(BulkWriteRefused)
      expect(await role.operator.count('bulk_events')).toBe(1000)
    })
  }, 20000)

  it.skipIf(!live)(`keeps a stale ${dialect} batch fenced after another session opens`, async () => {
    await fixture(dialect, async (engine, table) => {
      const model = buildModel([entity('Record', { fields: { id: T.int64 }, key: ['id'] })])
      const map = (epoch: number) => loadMap({ contract: 4, project_id: '1'.repeat(32), model_version: model.version,
        map_version: epoch, groups: { Record: { write_epoch: epoch, source: { id: 'source', engine: 'db',
          layout: { tables: { Record: table }, columns: { Record: { id: dialect === 'postgres' ? 'bigint' : 'Int64' } } },
        } } } }, { model })
      await prepareSchema(model, map(1), { db: engine }, { projectId: '1'.repeat(32) })
      const old = await Session.open(model, map(1), { db: engine }, { projectId: '1'.repeat(32) })
      await old.saveMany('Record', [{ id: 1n }, { id: 2n }])
      const fence = engine.writeFence(table, { projectId: '1'.repeat(32) }), hold = '4'.repeat(32)
      await fence.freeze(hold); await fence.advance(2); await fence.release(hold)
      const current = await Session.open(model, map(2), { db: engine }, { projectId: '1'.repeat(32) })
      await expect(old.saveMany('Record', [{ id: 3n }, { id: 4n }])).rejects.toThrow(EngineError)
      await current.saveMany('Record', [{ id: 5n }, { id: 6n }])
      expect(await engine.count(table)).toBe(4)
      expect((await engine.get(table, { id: 5n }))![WRITE_EPOCH_COLUMN]).toBe(2n)
    })
  }, 20000)
}

it.skipIf(!process.env['SDE_POSTGRES_DSN'])('PostgreSQL uses one statement, preserves conflicts and nested rollback', async () => {
  await withRoles('postgres', async role => {
    const session = await native(role, 'postgres')
    await role.statement('CREATE TABLE insert_calls (n integer)')
    await role.grant('insert_calls')
    await role.statement('CREATE FUNCTION record_insert() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN INSERT INTO insert_calls VALUES (1); RETURN NULL; END $$')
    await role.statement('CREATE TRIGGER count_insert AFTER INSERT ON bulk_events FOR EACH STATEMENT EXECUTE FUNCTION record_insert()')
    const value = Timestamp.from('2026-09-14T00:00:00.123456Z')
    await session.saveMany('Event', [{ id: 1n, value }, { id: 2n, value }])
    expect(await role.statement('SELECT count(*) AS n FROM insert_calls')).toEqual([{ n: '1' }])
    await expect(session.saveMany('Event', [{ id: 3n, value }, { id: 1n, value }])).rejects.toThrow(EngineError)
    expect(await role.operator.count('bulk_events')).toBe(2)
    await session.transaction(['Event'], async () => {
      await session.saveMany('Event', [{ id: 3n, value }])
      await expect(session.transaction(['Event'], async () => {
        await session.saveMany('Event', [{ id: 4n, value }, { id: 1n, value }])
      })).rejects.toThrow(EngineError)
    })
    expect(await role.operator.count('bulk_events')).toBe(3)
    await expect(session.transaction(['Event'], async () => {
      await session.transaction(['Event'], async () => session.saveMany('Event', [{ id: 5n, value }]))
      throw new Error('outer rollback')
    })).rejects.toThrow('outer rollback')
    expect(await role.operator.count('bulk_events')).toBe(3)
    await expect(session.transaction(['Event'], async () => {
      await session.saveMany('Event', [{ id: 6n, value }])
      await expect(session.saveMany('Event', [{ id: 1n, value }])).rejects.toThrow(EngineError)
    })).rejects.toThrow(EngineError)
    expect(await role.operator.count('bulk_events')).toBe(3)
  })
}, 20000)

it.skipIf(!process.env['SDE_POSTGRES_DSN'])('writes nested JSON documents without caller mutation affecting commit', async () => {
  await withRoles('postgres', async role => {
    const session = await native(role, 'postgres', true)
    const input = [{ id: 1n, value: { nested: ['zażółć', { n: 7 }] } }]
    await session.transaction(['Event'], async () => {
      await session.saveMany('Event', input)
      input[0]!.value.nested[0] = 'changed'
    })
    expect((await role.operator.get('bulk_events', { id: 1n }))!['value']).toEqual({ nested: ['zażółć', { n: 7 }] })
  })
}, 20000)

for (const [sourceDialect, targetDialect] of [['postgres', 'clickhouse'], ['clickhouse', 'postgres']] as const) {
  it.skipIf(!process.env['SDE_POSTGRES_DSN'] || !process.env['SDE_CLICKHOUSE_DSN'])(`native bulk fan-out ${sourceDialect} -> ${targetDialect} respects source commit`, async () => {
    await withRoles(sourceDialect, async source => withRoles(targetDialect, async target => {
      const initial = await native(source, sourceDialect)
      const copy = await native(target, targetDialect)
      const model = initial.model
      const placement = loadMap({ contract: 3, model_version: model.version, map_version: 2, groups: {
        Event: { source: { id: 'source', engine: 'source', layout: initial.placement.groups['Event']!.source.layout },
          derived: [{ id: 'copy', engine: 'copy', layout: copy.placement.groups['Event']!.source.layout, lag_budget_ms: 30000 }],
          also_write: ['copy'] },
      } }, { model })
      const session = await Session.open(model, placement, { source: source.runtime, copy: target.runtime })
      const rows = [{ id: 1n, value: Timestamp.from('2026-09-14T00:00:00.123456Z') },
        { id: 2n, value: Timestamp.from('2026-09-14T00:00:00.123457Z') }]
      if (sourceDialect === 'postgres') {
        await session.transaction(['Event'], async () => {
          await session.transaction(['Event'], async () => session.saveMany('Event', rows))
          expect(await target.operator.count('bulk_events')).toBe(0)
        })
        await expect(session.transaction(['Event'], async () => {
          await session.saveMany('Event', [{ id: 99n, value: rows[0]!.value }]); throw new Error('rollback')
        })).rejects.toThrow('rollback')
      } else await session.saveMany('Event', rows)
      expect(await source.operator.keyRange('bulk_events', ['id'])).toEqual(rows)
      expect(await target.operator.keyRange('bulk_events', ['id'])).toEqual(rows)
    }))
  }, 20000)
}

/** Owned sessions release native runtime connections; the caller's independent adapter survives. */
import { expect, it } from 'vitest'
import { buildModel, entity, loadMap, Session, T } from '../src/index.js'
import { PostgresEngine } from '../src/engines/postgres.js'
import { ClickHouseEngine } from '../src/engines/clickhouse.js'
import { withRoles } from './_runtime-roles.js'

for (const dialect of ['postgres', 'clickhouse'] as const) {
  const enabled = process.env[dialect === 'postgres' ? 'SDE_POSTGRES_DSN' : 'SDE_CLICKHOUSE_DSN'] !== undefined
  it.skipIf(!enabled)(`owned Session.connect uses and releases a real ${dialect} connection`, async () => {
    await withRoles(dialect, async role => {
      const model = buildModel([entity('Event', { fields: { id: T.int64 }, key: ['id'] })])
      const placement = loadMap({ contract: 3, model_version: model.version, map_version: 1,
        groups: { Event: { source: { id: 'source', engine: 'db', layout: {
          tables: { Event: 'owned_events' }, columns: { Event: { id: dialect === 'postgres' ? 'bigint' : 'Int64' } },
        } } } } }, { model })
      await role.operator.ensureSchema(placement.groups['Event']!.source.layout, { keys: { Event: ['id'] } })
      await role.grant('owned_events')
      const engine = dialect === 'postgres' ? new PostgresEngine(role.runtimeDsn) : new ClickHouseEngine(role.runtimeDsn)
      const session = await Session.connect(model, placement, { db: () => engine })
      try {
        await session.save('Event', { id: 1n })
        expect(await session.get('Event', { id: 1n })).toEqual({ id: 1n })
      } finally { await session.close() }
      await expect(engine.get('owned_events', { id: 1n })).rejects.toThrow()
      expect(await role.runtime.get('owned_events', { id: 1n })).not.toBeNull()
    })
  }, 20000)
}

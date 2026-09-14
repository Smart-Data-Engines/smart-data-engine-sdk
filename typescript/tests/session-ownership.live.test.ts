/** Independent async work must never be acknowledged inside somebody else's transaction. */
import { expect, it } from 'vitest'
import { buildModel, entity, loadMap, ResourceBusy, ResourceClosed, Session, T } from '../src/index.js'
import { withRoles } from './_runtime-roles.js'

const live = process.env['SDE_POSTGRES_DSN'] !== undefined
function signal(): { promise: Promise<void>; release: () => void } {
  let release: () => void = () => {}
  const promise = new Promise<void>(resolve => { release = resolve })
  return { promise, release }
}
async function fixture(body: (first: Session, other: Session, role: Parameters<Parameters<typeof withRoles>[1]>[0]) => Promise<void>): Promise<void> {
  await withRoles('postgres', async role => {
    const model = buildModel([entity('Event', { fields: { id: T.int64, value: T.int32 }, key: ['id'] })])
    const placement = loadMap({ contract: 3, model_version: model.version, map_version: 1, groups: {
      Event: { source: { id: 'source', engine: 'pg', layout: { tables: { Event: 'events' }, columns: { Event: { id: 'bigint', value: 'integer' } } } } },
    } }, { model })
    await role.operator.ensureSchema(placement.groups['Event']!.source.layout, { keys: { Event: ['id'] } })
    await role.grant('events')
    const first = await Session.open(model, placement, { pg: role.runtime })
    const other = await Session.open(model, placement, { pg: role.runtime })
    await body(first, other, role)
  })
}

it.skipIf(!live).each([false, true])('refuses an independent async caller sharing a transaction (same session=%s)', async same => {
  await fixture(async (first, second, role) => {
    const other = same ? first : second
    const entered = signal(), release = signal(), abort = new Error('controlled rollback')
    const transaction = first.transaction(['Event'], async () => {
      await first.save('Event', { id: 1n, value: 11 }); entered.release(); await release.promise; throw abort
    }).catch(error => { if (error !== abort) throw error })
    await entered.promise
    try { await expect(other.save('Event', { id: 2n, value: 22 })).rejects.toBeInstanceOf(ResourceBusy) }
    finally { release.release(); await transaction }
    expect(await role.operator.get('events', { id: 1n })).toBeNull()
    await other.save('Event', { id: 2n, value: 22 })
    expect(await role.operator.get('events', { id: 2n })).toEqual({ id: 2n, value: 22 })
  })
}, 20000)

it.skipIf(!live)('refuses a different session even inside the owner async context', async () => {
  await fixture(async (first, other) => {
    await first.transaction(['Event'], async () => {
      await expect(other.save('Event', { id: 2n, value: 22 })).rejects.toBeInstanceOf(ResourceBusy)
    })
    await other.save('Event', { id: 2n, value: 22 })
  })
}, 20000)

it.skipIf(!live)('inner success never commits the outer transaction', async () => {
  await fixture(async (session, _other, role) => {
    const abort = new Error('controlled outer rollback')
    await expect(session.transaction(['Event'], async () => {
      await session.save('Event', { id: 1n, value: 11 })
      await session.transaction(['Event'], async () => { await session.save('Event', { id: 2n, value: 22 }) })
      throw abort
    })).rejects.toBe(abort)
    expect(await role.operator.count('events')).toBe(0)
  })
}, 20000)

it.skipIf(!live)('inner rollback retains the outer transaction and its later writes', async () => {
  await fixture(async (session, _other, role) => {
    const abort = new Error('controlled inner rollback')
    await session.transaction(['Event'], async () => {
      await session.save('Event', { id: 1n, value: 11 })
      await expect(session.transaction(['Event'], async () => {
        await session.save('Event', { id: 2n, value: 22 }); throw abort
      })).rejects.toBe(abort)
      await session.save('Event', { id: 3n, value: 33 })
    })
    expect(await role.operator.get('events', { id: 1n })).not.toBeNull()
    expect(await role.operator.get('events', { id: 2n })).toBeNull()
    expect(await role.operator.get('events', { id: 3n })).not.toBeNull()
  })
}, 20000)

it.skipIf(!live)('a task escaping its transaction cannot later write outside it', async () => {
  await fixture(async (session, _other, role) => {
    const release = signal()
    let escaped: Promise<void> = Promise.resolve()
    await session.transaction(['Event'], async () => {
      escaped = (async () => { await release.promise; await session.save('Event', { id: 1n, value: 11 }) })()
    })
    const refused = expect(escaped).rejects.toBeInstanceOf(ResourceClosed)
    release.release(); await refused
    expect(await role.operator.count('events')).toBe(0)
    await session.save('Event', { id: 2n, value: 22 })
  })
}, 20000)

it.skipIf(!live)('closing a session retains borrowed adapters and refuses later operations', async () => {
  await fixture(async (session, other, role) => {
    session.close(); session.close()
    await expect(session.save('Event', { id: 1n, value: 11 })).rejects.toBeInstanceOf(ResourceClosed)
    await expect(session.get('Event', { id: 1n })).rejects.toBeInstanceOf(ResourceClosed)
    await expect(session.transaction(['Event'], async () => {})).rejects.toBeInstanceOf(ResourceClosed)
    await other.save('Event', { id: 2n, value: 22 })
    expect(await role.operator.get('events', { id: 2n })).not.toBeNull()
  })
}, 20000)

it.skipIf(!live)('catching a statement error cannot report an aborted transaction as committed', async () => {
  await fixture(async (session, _other, role) => {
    await expect(session.transaction(['Event'], async () => {
      await session.save('Event', { id: 1n, value: 11 })
      await expect(session.save('Event', { id: 1n, value: 22 })).rejects.toThrow()
    })).rejects.toThrow()
    expect(await role.operator.get('events', { id: 1n })).toBeNull()
    await session.save('Event', { id: 2n, value: 22 })
    expect(await role.operator.get('events', { id: 2n })).not.toBeNull()
  })
}, 20000)

it.skipIf(!live)('an unawaited in-flight operation cannot enqueue a write after rollback', async () => {
  await fixture(async (session, _other, role) => {
    const native = role.runtime as unknown as { run(sql: string): Promise<unknown> }
    const insert = role.runtime.insert.bind(role.runtime), entered = signal()
    let escaped: Promise<void> = Promise.resolve(), escapedError: unknown
    role.runtime.insert = async (table, row) => {
      const sleeping = native.run('SELECT pg_sleep(0.2)')
      entered.release()
      await sleeping
      await insert(table, row)
    }
    try {
      await expect(session.transaction(['Event'], async () => {
        escaped = session.save('Event', { id: 1n, value: 11 }).catch(error => { escapedError = error })
        await entered.promise
      })).rejects.toBeInstanceOf(ResourceBusy)
      await escaped
      expect(await role.operator.get('events', { id: 1n })).toBeNull()
      expect(escapedError).toBeInstanceOf(ResourceClosed)
    } finally { role.runtime.insert = insert }
  })
}, 20000)

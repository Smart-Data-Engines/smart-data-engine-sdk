/** Partial owned startup and failed cleanup must not leak resources or replace the original error. */
import { expect, it } from 'vitest'
import { buildModel, EngineError, entity, loadMap, ResourceClosed, Session, T } from '../src/index.js'
import { MemoryEngine } from '../src/testing/memory.js'

class Engine extends MemoryEngine {
  opened = 0
  closed = 0
  failConnect = false
  failClose = false
  async connect(): Promise<void> { this.opened++; if (this.failConnect) throw new Error('connect failure') }
  async close(): Promise<void> { this.closed++; if (this.failClose) throw new Error('close failure') }
}
function fixture() {
  const model = buildModel([entity('Event', { fields: { id: T.int64 }, key: ['id'] })])
  const map = loadMap({ contract: 3, model_version: model.version, map_version: 1,
    groups: { Event: { source: { id: 'source', engine: 'a', layout: { auto: true } } } } }, { model })
  return { model, map }
}

it('closes all owned adapters once and refuses reuse', async () => {
  const { model, map } = fixture(), a = new Engine(), b = new Engine()
  const session = await Session.connect(model, map, { a: () => a, b: () => b })
  expect([a.opened, b.opened]).toEqual([1, 1])
  await session.save('Event', { id: 1n })
  await session.close(); await session.close()
  expect([a.closed, b.closed]).toEqual([1, 1])
  await expect(session.get('Event', { id: 1n })).rejects.toBeInstanceOf(ResourceClosed)
})

it('closes the failed second adapter and the first connected one', async () => {
  const { model, map } = fixture(), a = new Engine(), b = new Engine(); b.failConnect = true
  await expect(Session.connect(model, map, { a: () => a, b: () => b })).rejects.toThrow('connect failure')
  expect([a.closed, b.closed]).toEqual([1, 1])
})

it('closes resources after model/map binding validation fails', async () => {
  const { model, map } = fixture(), a = new Engine()
  await expect(Session.connect(model, map, { missing: () => a })).rejects.toBeInstanceOf(EngineError)
  expect([a.opened, a.closed]).toEqual([1, 1])
})

it('rejects a shared factory result and closes it only once', async () => {
  const { model, map } = fixture(), a = new Engine()
  await expect(Session.connect(model, map, { a: () => a, b: () => a })).rejects.toThrow('distinct')
  expect([a.opened, a.closed]).toEqual([1, 1])
})

it('tries every close and retains a failed close for explicit retry', async () => {
  const { model, map } = fixture(), a = new Engine(), b = new Engine(); b.failClose = true
  const session = await Session.connect(model, map, { a: () => a, b: () => b })
  await expect(session.close()).rejects.toThrow('close failure')
  expect([a.closed, b.closed]).toEqual([1, 1])
  b.failClose = false; await session.close()
  expect([a.closed, b.closed]).toEqual([1, 2])
})

it('does not replace startup failure with a cleanup failure', async () => {
  const { model, map } = fixture(), a = new Engine(), b = new Engine()
  a.failClose = true; b.failConnect = true
  await expect(Session.connect(model, map, { a: () => a, b: () => b })).rejects.toThrow('connect failure')
  expect([a.closed, b.closed]).toEqual([1, 1])
})

it('refuses concurrent cleanup instead of closing the same adapter twice', async () => {
  const { model, map } = fixture(), engine = new Engine()
  let release: () => void = () => {}
  const stopped = new Promise<void>(resolve => { release = resolve })
  engine.close = async () => { engine.closed++; await stopped }
  const session = await Session.connect(model, map, { a: () => engine })
  const first = session.close()
  try { await expect(session.close()).rejects.toThrow('cleanup is already in progress') }
  finally { release(); await first }
  expect(engine.closed).toBe(1)
})

it('refuses an operation in another colocation group inside the transaction', async () => {
  const model = buildModel(['Event', 'Audit'].map(name => entity(name, { fields: { id: T.int64 }, key: ['id'] })))
  const map = loadMap({ contract: 3, model_version: model.version, map_version: 1,
    groups: Object.fromEntries(['Event', 'Audit'].map(name => [name, { source: { id: name, engine: 'a', layout: { auto: true } } }])) }, { model })
  const engine = new Engine(), session = await Session.open(model, map, { a: engine })
  await session.transaction(['Event'], async () => {
    await expect(session.save('Audit', { id: 1n })).rejects.toThrow('outside')
    await session.save('Event', { id: 2n })
  })
  expect(await engine.get('audit', { id: 1n })).toBeNull()
  expect(await engine.get('event', { id: 2n })).not.toBeNull()
})

import { describe, expect, it } from 'vitest'
import { MigrationRefused, WriteFence } from '../src/index.js'
import { EPOCH_COLUMN, FENCE_PREFIX } from '../src/write-fence.js'
import { MemoryFences } from './_write-fence.js'

const PROJECT = '1'.repeat(32), HOLD = '2'.repeat(32)
function fixture() {
  const backend = new MemoryFences()
  return { backend, fence: new WriteFence(backend, 'events', { projectId: PROJECT }) }
}

describe('native generation protocol', () => {
  it('never releases old or future epochs across a generation change', async () => {
    const { backend, fence } = fixture()
    expect((await fence.prepare(1)).epoch).toBe(1)
    expect(await backend.accepts(0)).toBe(false)
    expect(await backend.accepts(1)).toBe(true)
    expect(await backend.accepts(2)).toBe(false)
    expect((await fence.freeze(HOLD)).closed).toBe(true)
    expect(await backend.accepts(1)).toBe(false)
    expect((await fence.advance(2)).closed).toBe(true)
    expect((await fence.release(HOLD)).epoch).toBe(2)
    expect(await backend.accepts(1)).toBe(false)
    expect(await backend.accepts(2)).toBe(true)
    expect(await backend.accepts(3)).toBe(false)
    await fence.freeze('3'.repeat(32))
    await expect(fence.advance(1)).rejects.toThrow('backwards')
  })
  it('drains on retry and releases only the named hold', async () => {
    const { backend, fence } = fixture()
    await fence.prepare(1); await fence.freeze(HOLD)
    const before = backend.calls.filter((call) => call[0] === 'drain').length
    await fence.freeze(HOLD)
    expect(backend.calls.filter((call) => call[0] === 'drain')).toHaveLength(before + 1)
    const other = '3'.repeat(32)
    await fence.freeze(other)
    expect((await fence.release(HOLD)).holds).toEqual([other])
    expect(await backend.accepts(1)).toBe(false)
    expect((await fence.release(other)).closed).toBe(false)
    expect((await fence.release(other)).closed).toBe(false)
  })
  it.each([1, 2, 3, 4, 5, 6, 7])('resumes preparation after side effect %i', async (boundary) => {
    const { backend, fence } = fixture()
    backend.failAfter = boundary
    await expect(fence.prepare(1)).rejects.toThrow('lost the response')
    backend.failAfter = undefined
    expect((await fence.prepare(1)).epoch).toBe(1)
    expect(await backend.accepts(1)).toBe(true)
    expect(await backend.accepts(0)).toBe(false)
    expect(await backend.accepts(2)).toBe(false)
  })
  it.each([1, 2, 3, 4])('keeps an interrupted epoch change closed at step %i', async (boundary) => {
    const { backend, fence } = fixture()
    await fence.prepare(1); await fence.freeze(HOLD)
    backend.failAfter = backend.calls.length + boundary
    await expect(fence.advance(2)).rejects.toThrow('lost the response')
    expect(await backend.accepts(1)).toBe(false)
    expect(await backend.accepts(2)).toBe(false)
    backend.failAfter = undefined
    expect((await fence.advance(2)).closed).toBe(true)
    await fence.release(HOLD)
    expect(await backend.accepts(2)).toBe(true)
    expect(await backend.accepts(1)).toBe(false)
  })
  it.each([0, -1, true, 1.5, 2 ** 53])('does not touch metadata for bad epoch %s', async (epoch) => {
    const { backend, fence } = fixture()
    await expect(fence.prepare(epoch as number)).rejects.toThrow('safe integer')
    expect(backend.calls).toEqual([])
  })
  it('refuses a different project and an unowned reserved column', async () => {
    const { backend, fence } = fixture()
    await fence.prepare(1)
    const before = { ...backend.constraints }
    await expect(new WriteFence(backend, 'events', { projectId: '4'.repeat(32) }).prepare(1)).rejects.toThrow('another project')
    expect(backend.constraints).toEqual(before)
    const unowned = fixture(); unowned.backend.column = 'valid'
    await expect(unowned.fence.prepare(1)).rejects.toThrow('not owned')
    expect(unowned.backend.calls).toEqual([])
  })
  it('does not treat a different generation as idempotent provisioning', async () => {
    const { backend, fence } = fixture()
    await fence.prepare(1)
    const before = backend.calls.length
    await expect(fence.prepare(2)).rejects.toThrow('cannot change')
    await expect(fence.advance(2)).rejects.toThrow('named write barrier')
    expect(backend.calls).toHaveLength(before)
  })
  it.each([
    `CHECK ((${EPOCH_COLUMN} <= 1)) NOT VALID`,
    `CHECK ((${EPOCH_COLUMN} >= 0)) NOT VALID`,
    'CHECK (("__sde_write_ epoch" >= 1)) NOT VALID',
    'CHECK (("other" >= 1)) NOT VALID',
    `CHECK ((${EPOCH_COLUMN} >= 1) OR true) NOT VALID`,
    'CHECK (true) NOT VALID',
  ])('checks the predicate as well as the constraint name: %s', async (predicate) => {
    const { backend, fence } = fixture()
    await fence.prepare(1)
    backend.constraints[FENCE_PREFIX + 'min_1'] = predicate
    await expect(fence.state()).rejects.toThrow(MigrationRefused)
  })
  it('reads the native bigint literal without rounding or changing its bound', async () => {
    const { backend, fence } = fixture()
    const epoch = 2 ** 53 - 1
    await fence.prepare(epoch)
    backend.constraints[FENCE_PREFIX + 'min_' + epoch] = `CHECK (("${EPOCH_COLUMN}" >= '${epoch}'::bigint)) NOT VALID`
    expect((await fence.state()).epoch).toBe(epoch)
  })
  it('never reopens or reuses a completed barrier identifier', async () => {
    const { backend, fence } = fixture()
    await fence.prepare(1); await fence.freeze(HOLD); await fence.advance(2); await fence.release(HOLD)
    const before = [...backend.calls]
    await expect(fence.freeze(HOLD)).rejects.toThrow('cannot be reused')
    expect(backend.calls).toEqual(before)
    expect(await backend.accepts(2)).toBe(true)
  })

})

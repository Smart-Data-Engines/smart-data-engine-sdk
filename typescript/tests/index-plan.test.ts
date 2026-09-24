/** An in-place build authorization keeps a loaded snapshot and binds the signed map in force. */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, it } from 'vitest'
import { canonicalBytes, IndexPlan, indexBuildName, loadIndexPlan, loadMap } from '../src/index.js'
import { modelFromNeutral } from '../src/testing/loader.js'

const CASE = resolve('..', 'conformance/vectors/migration/144-index-build-adds-several-after-the-indexes-in-force')
function fixture() {
  const raw = JSON.parse(readFileSync(`${CASE}/plan.json`, 'utf8')) as Record<string, any>
  const model = modelFromNeutral(JSON.parse(readFileSync(`${CASE}/model.json`, 'utf8')))
  const keys = JSON.parse(readFileSync(`${CASE}/keys.json`, 'utf8')) as Record<string, string>
  const publicKey = Object.fromEntries(Object.entries(keys).map(([name, value]) => [name, Buffer.from(value, 'base64')]))
  const options = { model, publicKey, projectId: '1'.repeat(32) }
  return { raw, options, plan: loadIndexPlan(raw, options) }
}
it('keeps the next map and its indexes after mutation of caller and returned records', () => {
  const { raw, plan } = fixture(), expected = plan.preparedPayload()
  const added = JSON.stringify(plan.added)
  raw.prepared.groups.Event.source.layout.indexes[1].columns = ['id']
  const returned = plan.asRecord() as Record<string, any>
  returned.prepared.groups.Event.source.layout.indexes[1].columns = ['id']
  expect(plan.preparedPayload()).toEqual(expected)
  expect(JSON.stringify(plan.added)).toBe(added)
})
it('adds only the indexes after the ones in force', () => {
  const { plan } = fixture()
  expect(plan.added.map(index => index['name'])).toEqual([indexBuildName(plan.indexId, 1), indexBuildName(plan.indexId, 2)])
  expect(plan.added[0]!['method']).toBe('brin')
})
it('does not grant provenance to copied or constructed plans', () => {
  const { plan } = fixture()
  const copied = Object.assign(Object.create(IndexPlan.prototype) as IndexPlan, plan)
  const constructed = new IndexPlan(plan.indexId, plan.projectId, plan.group, plan.current, plan.prepared,
    plan.buildBudgetMs, plan.added, plan.verifiedWith)
  for (const value of [copied, constructed]) expect(() => value.asRecord()).toThrow('immutable loaded')
})
it('checks the actual signed current map and its provenance', () => {
  const { raw, options, plan } = fixture()
  plan.checkCurrent(plan.current)
  expect(() => plan.checkCurrent(plan.prepared)).toThrow()
  expect(() => plan.checkCurrent({ ...plan.current })).toThrow()
  delete raw.current.signature
  const unsigned = loadMap(raw.current, { model: options.model })
  expect(unsigned.fingerprint).toBe(plan.current.fingerprint)
  expect(() => plan.checkCurrent(unsigned)).toThrow('signed current map')
})
it('keeps the canonical packet bytes', () => {
  const { raw, plan } = fixture()
  expect(canonicalBytes(plan.asRecord())).toEqual(canonicalBytes(raw))
})
it.each([0, -1, 1000000, true, 1.5])('refuses an invalid index position %s', position => {
  expect(() => indexBuildName('6'.repeat(32), position as number)).toThrow('position')
})
it('uses the portable build-id and position form', () => {
  expect(indexBuildName('6'.repeat(32), 1)).toBe(`sde_i_${'6'.repeat(32)}_000001`)
  expect(Buffer.byteLength(indexBuildName('6'.repeat(32), 999999))).toBeLessThan(64)
  expect(() => indexBuildName('G'.repeat(32), 1)).toThrow('index build index_id')
})

/** Staging keeps a loaded snapshot and cannot change current-map admission. */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, it } from 'vitest'
import { canonicalBytes, loadMap, loadStagingPlan, StagingPlan, stagingTableName } from '../src/index.js'
import { modelFromNeutral } from '../src/testing/loader.js'

const CASE = resolve('..', 'conformance/vectors/migration/099-staging-authorizes-one-fresh-copy')
function fixture() {
  const raw = JSON.parse(readFileSync(`${CASE}/plan.json`, 'utf8')) as Record<string, any>
  const model = modelFromNeutral(JSON.parse(readFileSync(`${CASE}/model.json`, 'utf8')))
  const keys = JSON.parse(readFileSync(`${CASE}/keys.json`, 'utf8')) as Record<string, string>
  const publicKey = Object.fromEntries(Object.entries(keys).map(([name, value]) => [name, Buffer.from(value, 'base64')]))
  const options = { model, publicKey, projectId: '1'.repeat(32) }
  return { raw, options, plan: loadStagingPlan(raw, options) }
}
it('keeps the prepared copy after mutation of caller and returned records', () => {
  const { raw, plan } = fixture(), expected = plan.preparedPayload()
  raw.prepared.groups.Event.derived[0].layout.tables.Event = 'other'
  const returned = plan.asRecord() as Record<string, any>
  returned.prepared.groups.Event.derived[0].layout.tables.Event = 'other'
  expect(plan.preparedPayload()).toEqual(expected)
})
it('does not grant provenance to copied or constructed plans', () => {
  const { plan } = fixture()
  const copied = Object.assign(Object.create(StagingPlan.prototype) as StagingPlan, plan)
  const constructed = new StagingPlan(plan.stageId, plan.projectId, plan.group, plan.current, plan.prepared, plan.verifiedWith)
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
it.each([0, -1, 1000000, true, 1.5])('refuses an invalid entity position %s', position => {
  expect(() => stagingTableName('5'.repeat(32), position as number)).toThrow('position')
})
it('uses the portable stage-id and entity-position form', () => {
  expect(stagingTableName('5'.repeat(32), 1)).toBe(`sde_m_${'5'.repeat(32)}_000001`)
  expect(Buffer.byteLength(stagingTableName('5'.repeat(32), 999999))).toBeLessThan(64)
})

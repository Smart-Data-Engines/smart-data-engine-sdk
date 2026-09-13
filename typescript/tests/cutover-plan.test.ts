/** A loaded packet keeps its exact candidates and requires the same signed current-map mode. */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { canonicalBytes, CutoverPlan, loadCutoverPlan, loadMap } from '../src/index.js'
import { modelFromNeutral } from '../src/testing/loader.js'

const CASE = resolve('..', 'conformance/vectors/migration/079-cutover-packet-authorizes-one-group')
function fixture() {
  const raw = JSON.parse(readFileSync(`${CASE}/plan.json`, 'utf8')) as Record<string, any>
  const model = modelFromNeutral(JSON.parse(readFileSync(`${CASE}/model.json`, 'utf8')))
  const encoded = JSON.parse(readFileSync(`${CASE}/keys.json`, 'utf8')) as Record<string, string>
  const publicKey = Object.fromEntries(Object.entries(encoded).map(([name, value]) => [name, Buffer.from(value, 'base64')]))
  const options = { model, projectId: '1'.repeat(32), publicKey }
  return { raw, options, plan: loadCutoverPlan(raw, options) }
}

describe('cutover packet provenance', () => {
  it('preserves candidates after caller and returned-record mutation', () => {
    const { raw, plan } = fixture(), expected = plan.candidatePayload('success')
    raw['success'].groups.Event.source.layout.tables.Event = 'other'
    const returned = plan.asRecord() as Record<string, any>
    returned['success'].groups.Event.source.layout.tables.Event = 'other'
    expect(plan.candidatePayload('success')).toEqual(expected)
    expect(plan.success.groups['Event']?.source.layout.tables['Event']).toBe('event_copy')
  })
  it('does not pass loader provenance to a copied or constructed packet', () => {
    const { plan } = fixture()
    const copied = Object.assign(Object.create(CutoverPlan.prototype) as CutoverPlan, plan)
    const created = new CutoverPlan(plan.planId, plan.projectId, plan.group, plan.before, plan.success,
      plan.abort, plan.verification, plan.pauseBudgetMs, plan.queryImpactDigest, plan.verifiedWith)
    for (const value of [copied, created]) {
      expect(() => value.checkCurrent(plan.before)).toThrow('immutable loaded plan')
      expect(() => value.asRecord()).toThrow('immutable loaded plan')
    }
  })
  it('matches the current before-map and its loader provenance', () => {
    const { plan } = fixture()
    plan.checkCurrent(plan.before)
    expect(() => plan.checkCurrent(plan.success)).toThrow('current placement map')
    expect(() => plan.checkCurrent({ ...plan.before })).toThrow('immutable loaded placement')
  })
  it('does not confuse an unsigned current map with signed admission', () => {
    const { raw, plan, options } = fixture(), current = structuredClone(raw['before'])
    delete current.signature
    const unsigned = loadMap(current, { model: options.model })
    expect(unsigned.fingerprint).toBe(plan.before.fingerprint)
    expect(() => plan.checkCurrent(unsigned)).toThrow('current placement map')
  })
  it('retains the canonical packet representation', () => {
    const { raw, plan } = fixture()
    expect(canonicalBytes(plan.asRecord())).toEqual(canonicalBytes(raw))
  })
  it.each([null, [], { alg: 'ed25519', value: 'AA==' }])('names malformed signature input %#', bad => {
    const { raw, options } = fixture(); raw['signature'] = bad
    expect(() => loadCutoverPlan(raw, options)).toThrow(/cutover signature/)
  })
  it('refuses noncanonical base64 padding with the same decoded signature', () => {
    const { raw, options } = fixture(), signature = raw['signature'].value as string
    const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
    const altered = signature.slice(0, 85) + alphabet[alphabet.indexOf(signature[85]!) + 1] + signature.slice(86)
    expect(Buffer.from(altered, 'base64')).toEqual(Buffer.from(signature, 'base64'))
    raw['signature'].value = altered
    expect(() => loadCutoverPlan(raw, options)).toThrow('canonical base64')
  })
})

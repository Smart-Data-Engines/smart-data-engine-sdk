/**
 * A generation-advance authorization keeps a loaded snapshot, binds the signed map in force, and its
 * refusals name an advance.
 *
 * The shared vectors pin the packet's rules by their words (`migration/201` and after). These are
 * what a vector cannot hold: what a loaded plan is as an object, and the refusals a packet signed
 * by the vectors' key cannot reach - the packet here is a vector's, signed again with a key made
 * for the test, so that a change reaches the rule it is about and not a signature that stopped
 * verifying.
 */
import { generateKeyPairSync, sign } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, it } from 'vitest'
import { ADVANCE_PROTOCOL, AdvancePlan, canonicalBytes, loadAdvancePlan, loadMap, MigrationRefused } from '../src/index.js'
import { modelFromNeutral } from '../src/testing/loader.js'

const CASE = resolve('..', 'conformance/vectors/migration/201-advance-raises-one-group-by-one-generation')
const PROJECT = '1'.repeat(32)
const MAX_EPOCH = 9_007_199_254_740_991
type Packet = Record<string, any>

function fixture() {
  const raw = JSON.parse(readFileSync(`${CASE}/plan.json`, 'utf8')) as Packet
  const model = modelFromNeutral(JSON.parse(readFileSync(`${CASE}/model.json`, 'utf8')))
  const keys = JSON.parse(readFileSync(`${CASE}/keys.json`, 'utf8')) as Record<string, string>
  const publicKey = Object.fromEntries(Object.entries(keys).map(([name, value]) => [name, Buffer.from(value, 'base64')]))
  const options = { model, publicKey, projectId: PROJECT }
  return { raw, model, options, plan: loadAdvancePlan(raw, options) }
}

/** Signs as the control plane signs: over the canonical bytes without the block. */
function signer() {
  const pair = generateKeyPairSync('ed25519')
  const publicKey = { k: pair.publicKey.export({ format: 'der', type: 'spki' }).subarray(-32) }
  function signed(document: Packet): void {
    delete document['signature']
    document['signature'] = { alg: 'ed25519', key_id: 'k', value: sign(null, canonicalBytes(document), pair.privateKey).toString('base64') }
  }
  /** The packet with both of its maps and its envelope signed again. */
  function resigned(input: Packet): Packet {
    const packet = structuredClone(input)
    for (const name of ['before', 'after']) {
      const document = packet[name]
      if (typeof document === 'object' && document !== null && !Array.isArray(document)) signed(document)
    }
    signed(packet)
    return packet
  }
  return { publicKey, resigned }
}

/** What the loader says to the vector's packet after `change`, signed again. */
function refusalOf(change: (packet: Packet) => void): string {
  const { raw, model } = fixture(), key = signer()
  change(raw)
  let refusal: unknown
  try { loadAdvancePlan(key.resigned(raw), { model, projectId: PROJECT, publicKey: key.publicKey }) } catch (error) { refusal = error }
  expect(refusal).toBeInstanceOf(MigrationRefused)
  return (refusal as Error).message
}
function inBothMaps(change: (map: Packet) => void): (packet: Packet) => void {
  return packet => { for (const name of ['before', 'after']) change(packet[name]) }
}

it('loads the packet signed again: the premise of every refusal below', () => {
  const { raw, model, plan } = fixture(), key = signer()
  const again = loadAdvancePlan(key.resigned(raw), { model, projectId: PROJECT, publicKey: key.publicKey })
  expect(again.verifiedWith).toBe('k')
  expect([again.group, again.sourceEpoch, again.nextEpoch]).toEqual([plan.group, 1, 2])
  // A map's fingerprint is of the map without its signature, so it is the vector's. The packet's
  // covers its maps as they were signed, so it is another packet's.
  expect(again.before.fingerprint).toBe(plan.before.fingerprint)
  expect(again.after.fingerprint).toBe(plan.after.fingerprint)
  expect(again.fingerprint).not.toBe(plan.fingerprint)
})
it('keeps the next map after mutation of caller and returned records', () => {
  const { raw, plan } = fixture(), expected = plan.afterPayload()
  raw['after'].groups.Event.write_epoch = 9
  const returned = plan.asRecord() as Packet
  returned['after'].groups.Event.write_epoch = 9
  expect(plan.afterPayload()).toEqual(expected)
  expect((plan.asRecord() as Packet)['after'].groups.Event.write_epoch).toBe(2)
  expect(plan.nextEpoch).toBe(2)
})
it('publishes the signed next map byte for byte', () => {
  const { raw, options, plan } = fixture()
  expect(Buffer.from(plan.afterPayload()).equals(canonicalBytes(raw['after']))).toBe(true)
  const published = loadMap(JSON.parse(Buffer.from(plan.afterPayload()).toString('utf8')), { ...options, requireSignature: true })
  expect(published.fingerprint).toBe(plan.after.fingerprint)
  expect(published.groups['Event']!.writeEpoch).toBe(plan.nextEpoch)
})
it('reads its generations from the two maps', () => {
  const { plan } = fixture()
  expect(plan.sourceEpoch).toBe(plan.before.groups[plan.group]!.writeEpoch)
  expect(plan.nextEpoch).toBe(plan.after.groups[plan.group]!.writeEpoch)
  expect([plan.sourceEpoch, plan.nextEpoch]).toEqual([1, 2])
  // Nothing else in the group moved, and the other group moved not at all.
  expect(plan.after.groups['Event']!.source).toEqual(plan.before.groups['Event']!.source)
  expect(plan.after.groups['Event']!.derived).toEqual(plan.before.groups['Event']!.derived)
  expect(plan.after.groups['Order']).toEqual(plan.before.groups['Order'])
})
it('does not grant provenance to copied or constructed plans', () => {
  const { plan } = fixture()
  const copied = Object.assign(Object.create(AdvancePlan.prototype) as AdvancePlan, plan)
  const constructed = new AdvancePlan(plan.advanceId, plan.projectId, plan.group, plan.before, plan.after,
    plan.pauseBudgetMs, plan.verifiedWith)
  for (const value of [copied, constructed]) {
    expect(() => value.asRecord()).toThrow('immutable loaded')
    expect(() => value.afterPayload()).toThrow('immutable loaded')
    expect(() => value.sourceEpoch).toThrow('immutable loaded')
    expect(() => value.nextEpoch).toThrow('immutable loaded')
    expect(() => value.checkCurrent(plan.before)).toThrow('immutable loaded')
    expect(value.fingerprint).toBeUndefined()
  }
})
it('is frozen once loaded', () => {
  const { plan } = fixture()
  expect(Object.isFrozen(plan)).toBe(true)
  expect(() => { (plan as { group: string }).group = 'Order' }).toThrow(TypeError)
})
it('checks the actual signed current map and its provenance', () => {
  const { raw, options, plan } = fixture()
  plan.checkCurrent(plan.before)
  expect(() => plan.checkCurrent(plan.after)).toThrow('signed current map')
  expect(() => plan.checkCurrent({ ...plan.before })).toThrow()
  delete raw['before'].signature
  const unsigned = loadMap(raw['before'], { model: options.model })
  expect(unsigned.fingerprint).toBe(plan.before.fingerprint)
  expect(() => plan.checkCurrent(unsigned)).toThrow('signed current map')
})
it('checks the local project of the current map', () => {
  const { raw, model } = fixture(), key = signer(), other = '9'.repeat(32)
  const theirs = structuredClone(raw)
  theirs['project_id'] = other
  for (const name of ['before', 'after']) theirs[name].project_id = other
  const ours = loadAdvancePlan(key.resigned(raw), { model, projectId: PROJECT, publicKey: key.publicKey })
  const foreign = loadAdvancePlan(key.resigned(theirs), { model, projectId: other, publicKey: key.publicKey })
  expect(() => ours.checkCurrent(foreign.before)).toThrow(MigrationRefused)
})
it('keeps the canonical packet bytes', () => {
  const { raw, plan } = fixture()
  expect(canonicalBytes(plan.asRecord())).toEqual(canonicalBytes(raw))
})

it.each([
  ['an array', (): unknown => [], 'advance authorization must be an object'],
  ['a short id', (raw: Packet): unknown => ({ ...raw, advance_id: 'x' }), 'advance advance_id must be 32 lowercase'],
  ['an upper-case id', (raw: Packet): unknown => ({ ...raw, advance_id: 'A'.repeat(32) }), 'advance advance_id must be 32 lowercase'],
  ['an id that is a number', (raw: Packet): unknown => ({ ...raw, advance_id: 7 }), 'advance advance_id must be 32 lowercase'],
  ['a project that is no id', (raw: Packet): unknown => ({ ...raw, project_id: 'x' }), 'advance project_id must be 32 lowercase'],
  ['an empty group', (raw: Packet): unknown => ({ ...raw, group: '' }), 'advance group must be a nonempty string'],
  ['a group that is a number', (raw: Packet): unknown => ({ ...raw, group: 5 }), 'advance group must be a nonempty string'],
  ['a signature that is text', (raw: Packet): unknown => ({ ...raw, signature: 'x' }), 'advance signature must be an object'],
  ['a signature without a value', (raw: Packet): unknown => ({ ...raw, signature: { alg: 'ed25519' } }), 'advance signatures have missing or unknown fields'],
  ['another algorithm', (raw: Packet): unknown => ({ ...raw, signature: { ...raw['signature'], alg: 'rsa' } }), 'advance signatures must use ed25519 or es256 and canonical base64'],
  ['a key id that is a number', (raw: Packet): unknown => ({ ...raw, signature: { ...raw['signature'], key_id: 5 } }), 'advance signature key_id must be a string'],
  ['no pause budget', (raw: Packet): unknown => { const { pause_budget_ms: _gone, ...rest } = raw; return rest }, 'advance authorization has missing or unknown fields'],
])('refuses %s as an advance, before any key is tried', (_name, change, words) => {
  const { raw, options } = fixture()
  let refusal: unknown
  try { loadAdvancePlan(change(raw), options) } catch (error) { refusal = error }
  expect(refusal).toBeInstanceOf(MigrationRefused)
  expect((refusal as Error).message).toContain(words)
  expect((refusal as Error).message).not.toMatch(/cutover|staging/)
})

it.each([0, -1, true, 1.5, '30000', null, MAX_EPOCH + 1])('refuses the pause budget %s', budget => {
  const { raw, options } = fixture()
  raw['pause_budget_ms'] = budget
  expect(() => loadAdvancePlan(raw, options)).toThrow('advance pause_budget_ms must be a positive safe integer')
})
it('takes the longest pause budget a number holds', () => {
  const { raw, model } = fixture(), key = signer()
  raw['pause_budget_ms'] = MAX_EPOCH
  const plan = loadAdvancePlan(key.resigned(raw), { model, projectId: PROJECT, publicKey: key.publicKey })
  expect(plan.pauseBudgetMs).toBe(MAX_EPOCH)
})
it.each([0, 2, true, '1', null])('reads one protocol, and not %s', protocol => {
  const { raw, options } = fixture()
  raw['protocol'] = protocol
  expect(() => loadAdvancePlan(raw, options)).toThrow('unsupported advance authorization kind or protocol')
})

/** A map rewritten to contract 3: no project and no generation. It loads as a map. */
function asContract3(document: Packet): void {
  document['contract'] = 3
  delete document['project_id']
  for (const group of Object.values(document['groups'] as Record<string, Packet>)) delete group['write_epoch']
}

it.each([
  ['maps without generations', inBothMaps(asContract3),
    'an advance requires map contract 4 or later: it changes a write generation, and an earlier map has none'],
  ['a map in force that is an array', (p: Packet): void => { p['before'] = [] }, 'advance before must be an object'],
  ['a next map that is text', (p: Packet): void => { p['after'] = 'map' }, 'advance after must be an object'],
  ['groups that are an array', (p: Packet): void => { p['after'].groups = [] }, 'advance groups must be an object'],
  ['a group that is an array', (p: Packet): void => { p['after'].groups.Event = [] }, 'advance group must be an object'],
  ['copies that are not an array', inBothMaps(m => { m['groups'].Event.derived = 'Event@ch' }), 'advance derived copies must be an array'],
  ['a source that is an array', inBothMaps(m => { m['groups'].Event.source = [] }), 'advance materialization must be an object'],
  ['a copy that is a name', inBothMaps(m => { m['groups'].Event.derived.push('Event@ch') }), 'advance materialization must be an object'],
  ['a layout that is an array', inBothMaps(m => { m['groups'].Event.source.layout = [] }), 'advance layout must be an object'],
  // A layout written out with no column in it is not one anybody can fence a table by.
  ['a layout without columns', inBothMaps(m => { m['groups'].Event.source.layout.columns = {} }), 'advance maps need explicit physical layouts'],
  // Of a copy as of the source.
  ['a copy left to be derived', inBothMaps(m => { m['groups'].Event.derived[0].layout = { auto: 1 } }), 'advance maps need explicit physical layouts'],
])('refuses %s by the rule\'s own words', (_name, change, words) => {
  expect(refusalOf(change)).toBe(words)
})

it('reports a map or encoding refusal inside it as the advance\'s', () => {
  expect(refusalOf(p => { p['after'].groups.Event.write_epoch = true })).toMatch(/^advance authorization refused: group 'Event'/)
  // A group the model has and the next map does not place: the map loader's rule, so neither map
  // of a packet can hold another set of groups than the model's.
  expect(refusalOf(p => { delete p['after'].groups.Order })).toMatch(/^advance authorization refused: the map does not place these groups/)
  const { raw, model } = fixture(), key = signer()
  const packet = key.resigned(raw)
  packet['after'].groups.Event.write_epoch = 2.5 // not canonical, so not signed
  expect(() => loadAdvancePlan(packet, { model, projectId: PROJECT, publicKey: key.publicKey })).toThrow(/^advance authorization refused: /)
})
it('verifies the envelope before it reads the maps', () => {
  const { raw, options } = fixture()
  raw['before'] = 'not a map'
  let refusal: unknown
  try { loadAdvancePlan(raw, options) } catch (error) { refusal = error }
  expect(refusal).toBeInstanceOf(MigrationRefused)
  expect((refusal as Error).message).toMatch(/^advance authorization refused: /)
  expect((refusal as Error).message).not.toContain('must be an object')
})
it('verifies nothing with a key the caller did not configure', () => {
  const { raw, options } = fixture(), stranger = signer()
  expect(() => loadAdvancePlan(stranger.resigned(raw), options)).toThrow(/^advance authorization refused: /)
})
it('exports the protocol it reads', () => {
  expect(ADVANCE_PROTOCOL).toBe(1)
})

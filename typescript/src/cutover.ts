/** Validate a signed local cutover authorization without adopting a map or touching an engine. */
import { createHash } from 'node:crypto'
import { CanonicalError, canonicalBytes, compareCodePoints } from './canonical.js'
import { MapError, MigrationRefused } from './errors.js'
import { checkMapProject, MAX_EPOCH } from './generation.js'
import type { LogicalModel } from './model.js'
import { fingerprintOf, loadMap, verifyMapSignature, type LoadOptions, type PlacementMap } from './placement.js'
import { enumerateShapes, shapeId } from './shapes.js'
import { VerificationRequest } from './verification.js'

export const CUTOVER_PROTOCOL = 1
const FIELDS = ['kind', 'protocol', 'plan_id', 'project_id', 'group', 'pause_budget_ms',
  'query_impact_digest', 'verification', 'before', 'success', 'abort', 'signature'].sort(compareCodePoints)
type Outcome = 'success' | 'abort'
type PublicKeys = NonNullable<LoadOptions['publicKey']>
const provenance = new WeakMap<CutoverPlan, { document: string; fingerprint: string }>()

function record(value: unknown, name: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new MigrationRefused(`cutover ${name} must be an object`)
  }
  return value as Record<string, unknown>
}
function hex(value: unknown, width: number, name: string): string {
  if (typeof value !== 'string' || !new RegExp(`^[0-9a-f]{${width}}$`).test(value)) {
    throw new MigrationRefused(`cutover ${name} must be ${width} lowercase hexadecimal digits`)
  }
  return value
}
function positive(value: unknown, name: string): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 1 || value > MAX_EPOCH) {
    throw new MigrationRefused(`cutover ${name} must be a positive safe integer`)
  }
  return value
}
function equal(left: unknown, right: unknown): boolean {
  return canonicalBytes(left).equals(canonicalBytes(right))
}
function signature(document: Record<string, unknown>): void {
  const value = record(document['signature'], 'signature')
  const names = Object.keys(value).sort(compareCodePoints)
  if (!equal(names, ['alg', 'value']) && !equal(names, ['alg', 'key_id', 'value'])) {
    throw new MigrationRefused('cutover signatures have missing or unknown fields')
  }
  if (value['alg'] !== 'ed25519' || typeof value['value'] !== 'string') {
    throw new MigrationRefused('cutover signatures must use ed25519 and canonical base64')
  }
  const decoded = Buffer.from(value['value'], 'base64')
  if (decoded.length !== 64 || decoded.toString('base64') !== value['value']) {
    throw new MigrationRefused('cutover signatures must use ed25519 and canonical base64')
  }
  if ('key_id' in value && typeof value['key_id'] !== 'string') {
    throw new MigrationRefused('cutover signature key_id must be a string')
  }
}
function except(value: Record<string, unknown>, keys: readonly string[]): Record<string, unknown> {
  return Object.fromEntries(Object.entries(value).filter(([name]) => !keys.includes(name)))
}

export class CutoverPlan {
  constructor(readonly planId: string, readonly projectId: string, readonly group: string,
    readonly before: PlacementMap, readonly success: PlacementMap, readonly abort: PlacementMap,
    readonly verification: VerificationRequest, readonly pauseBudgetMs: number,
    readonly queryImpactDigest: string, readonly verifiedWith: string | null) { Object.freeze(this) }

  private loaded() {
    const saved = provenance.get(this)
    if (saved === undefined) throw new MigrationRefused('cutover execution requires an immutable loaded plan')
    return saved
  }
  get fingerprint(): string | undefined { return provenance.get(this)?.fingerprint }
  get sourceEpoch(): number { this.loaded(); return this.before.groups[this.group]!.writeEpoch as number }
  get maintenanceEpoch(): number { return this.sourceEpoch + 1 }
  get activationEpoch(): number { return this.sourceEpoch + 2 }
  asRecord(): Record<string, unknown> { return JSON.parse(this.loaded().document) as Record<string, unknown> }
  candidatePayload(outcome: Outcome): Uint8Array {
    if (outcome !== 'success' && outcome !== 'abort') throw new MigrationRefused('cutover outcome must be success or abort')
    return canonicalBytes(this.asRecord()[outcome])
  }
  checkCurrent(current: PlacementMap): void {
    this.loaded()
    checkMapProject(current, this.projectId)
    if (!current.signed || fingerprintOf(current) !== fingerprintOf(this.before)) {
      throw new MigrationRefused('cutover plan does not name the current placement map')
    }
  }
}

function load(raw: unknown, model: LogicalModel, projectId: string, publicKey: PublicKeys): CutoverPlan {
  const body = record(structuredClone(raw), 'plan')
  if (!equal(Object.keys(body).sort(compareCodePoints), FIELDS)) {
    throw new MigrationRefused('cutover plan has missing or unknown fields')
  }
  if (typeof body['protocol'] !== 'number' || body['protocol'] !== CUTOVER_PROTOCOL) {
    throw new MigrationRefused('unsupported cutover plan protocol')
  }
  if (body['kind'] !== 'sde-cutover') throw new MigrationRefused('unsupported cutover document kind')
  const identity = hex(body['plan_id'], 32, 'plan_id'), local = hex(body['project_id'], 32, 'project_id')
  if (local !== projectId) throw new MigrationRefused('cutover plan belongs to another locally configured project')
  const budget = positive(body['pause_budget_ms'], 'pause_budget_ms')
  const approval = hex(body['query_impact_digest'], 64, 'query_impact_digest'), group = body['group']
  if (typeof group !== 'string' || group.length === 0) throw new MigrationRefused('cutover group must be a nonempty string')
  signature(body)
  const verifiedWith = verifyMapSignature(body, publicKey)
  const documents = Object.fromEntries(['before', 'success', 'abort'].map(name => [name, record(body[name], name)]))
  const maps: Record<string, PlacementMap> = {}
  for (const name of ['before', 'success', 'abort']) {
    const document = documents[name]!
    signature(document)
    for (const value of Object.values(record(document['groups'], 'groups'))) {
      const spot = record(value, 'group'), derived = spot['derived'] === undefined ? [] : spot['derived']
      if (!Array.isArray(derived)) throw new MigrationRefused('cutover derived copies must be an array')
      for (const value of [spot['source'], ...derived]) {
        const material = record(value, 'materialization'), layout = record(material['layout'], 'layout')
        if (layout['auto']) throw new MigrationRefused('cutover maps require explicit physical layouts')
      }
    }
    const parsed = loadMap(document, { model, publicKey, requireSignature: true })
    if (parsed.contract !== 4) throw new MigrationRefused('cutover protocol 1 requires placement map contract 4')
    checkMapProject(parsed, projectId)
    positive(parsed.mapVersion, 'map_version')
    maps[name] = parsed
  }
  const before = maps['before']!, success = maps['success']!, abort = maps['abort']!
  if (!(before.mapVersion < success.mapVersion && success.mapVersion < abort.mapVersion)) {
    throw new MigrationRefused('cutover versions must increase from before to success to abort')
  }
  const request = VerificationRequest.fromRecord(body['verification'])
  if (!request.requiresSignature) throw new MigrationRefused('cutover verification must require the signed before map')
  request.checkSession(before, projectId, group)
  const spot = before.groups[group]!
  if (spot.derived.length !== 1 || spot.alsoWrite.length !== 1 || spot.alsoWrite[0]!.id !== spot.derived[0]!.id) {
    throw new MigrationRefused('cutover requires exactly one derived copy maintained by fan-out')
  }
  const source = spot.source, target = spot.derived[0]!, epoch = spot.writeEpoch as number
  if (source.engine === target.engine) throw new MigrationRefused('cutover source and target must use different engine bindings')
  if (epoch > MAX_EPOCH - 2) throw new MigrationRefused('cutover needs two available write generations')
  const affected = new Set(enumerateShapes(model).filter(shape => shape.group === group).map(shapeId))
  if ([...affected].some(shape => (before.routing[shape] ?? source.id) !== source.id)) {
    throw new MigrationRefused('cutover before-map reads must still use the source')
  }
  const beforeRaw = documents['before']!, beforeGroups = record(beforeRaw['groups'], 'groups')
  const routes = Object.fromEntries(Object.entries(before.routing).filter(([shape]) => !affected.has(shape)))
  const stableKeys = ['signature', 'map_version', 'groups', 'routing']
  const stable = except(beforeRaw, stableKeys)
  for (const [name, material, terminalEpoch] of [['success', target, epoch + 2], ['abort', source, epoch + 1]] as const) {
    const document = documents[name]!, parsed = maps[name]!, groups = record(document['groups'], 'groups')
    if (!equal(except(document, stableKeys), stable) || !equal(Object.keys(parsed.groups).sort(compareCodePoints), Object.keys(before.groups).sort(compareCodePoints))) {
      throw new MigrationRefused('cutover cannot change other map attributes or groups')
    }
    if (!equal(parsed.routing, routes)) throw new MigrationRefused('cutover terminal routing must preserve the unaffected groups')
    for (const other of Object.keys(before.groups)) {
      if (other !== group && !equal(groups[other], beforeGroups[other])) throw new MigrationRefused('cutover cannot change an unaffected group')
    }
    const terminal = record(groups[group], 'terminal group')
    if (!equal(Object.keys(terminal).sort(compareCodePoints), ['source', 'write_epoch']) || terminal['write_epoch'] !== terminalEpoch) {
      throw new MigrationRefused('cutover terminal group must contain only its source and decision generation')
    }
    const candidate = record(terminal['source'], 'terminal source')
    if (typeof candidate['id'] !== 'string' || candidate['id'].length === 0) {
      throw new MigrationRefused('cutover terminal source must have a nonempty string id')
    }
    const originalGroup = record(beforeGroups[group], 'group')
    const original = record(name === 'abort' ? originalGroup['source'] : (originalGroup['derived'] as unknown[])[0], 'materialization')
    if (!equal(except(candidate, ['id']), except(original, ['id', 'lag_budget_ms'])) || parsed.groups[group]!.source.engine !== material.engine) {
      throw new MigrationRefused('cutover terminal map changed the authorized physical materialization')
    }
  }
  const plan = new CutoverPlan(identity, local, group, before, success, abort, request, budget, approval, verifiedWith)
  provenance.set(plan, { document: canonicalBytes(body).toString('utf8'),
    fingerprint: createHash('sha256').update(canonicalBytes(except(body, ['signature']))).digest('hex') })
  return plan
}

export function loadCutoverPlan(raw: unknown, options: { model: LogicalModel; projectId: string; publicKey: PublicKeys }): CutoverPlan {
  try { return load(raw, options.model, options.projectId, options.publicKey) }
  catch (error) {
    if (error instanceof MapError || error instanceof CanonicalError) throw new MigrationRefused(`cutover document refused: ${error.message}`)
    throw error
  }
}

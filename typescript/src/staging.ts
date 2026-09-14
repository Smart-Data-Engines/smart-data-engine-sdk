/** Signed preparation of one fresh copy without changing its current source. */
import { createHash } from 'node:crypto'
import { CanonicalError, canonicalBytes, compareCodePoints } from './canonical.js'
import { MapError, MigrationRefused } from './errors.js'
import { checkMapProject, MAX_EPOCH } from './generation.js'
import { colocationGroups } from './groups.js'
import { groupColumns } from './layout.js'
import type { LogicalModel } from './model.js'
import { fingerprintOf, loadMap, verifyMapSignature, type LoadOptions, type PlacementMap } from './placement.js'

export const STAGING_PROTOCOL = 1
const FIELDS = ['kind', 'protocol', 'stage_id', 'project_id', 'group', 'current', 'prepared', 'signature'].sort(compareCodePoints)
type PublicKeys = NonNullable<LoadOptions['publicKey']>
const provenance = new WeakMap<StagingPlan, { document: string; fingerprint: string }>()

function record(value: unknown, name: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new MigrationRefused(`staging ${name} must be an object`)
  }
  return value as Record<string, unknown>
}
function hex(value: unknown, width: number, name: string): string {
  if (typeof value !== 'string' || !new RegExp(`^[0-9a-f]{${width}}$`).test(value)) {
    throw new MigrationRefused(`staging ${name} must be ${width} lowercase hexadecimal digits`)
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
    throw new MigrationRefused('staging signatures have missing or unknown fields')
  }
  if (value['alg'] !== 'ed25519' || typeof value['value'] !== 'string') {
    throw new MigrationRefused('staging signatures must use ed25519 and canonical base64')
  }
  const decoded = Buffer.from(value['value'], 'base64')
  if (decoded.length !== 64 || decoded.toString('base64') !== value['value']) {
    throw new MigrationRefused('staging signatures must use ed25519 and canonical base64')
  }
  if ('key_id' in value && typeof value['key_id'] !== 'string') {
    throw new MigrationRefused('staging signature key_id must be a string')
  }
}
function except(value: Record<string, unknown>, keys: readonly string[]): Record<string, unknown> {
  return Object.fromEntries(Object.entries(value).filter(([name]) => !keys.includes(name)))
}

export function stagingTableName(stageId: string, position: number): string {
  hex(stageId, 32, 'stage_id')
  if (!Number.isInteger(position) || position < 1 || position > 999999) {
    throw new MigrationRefused('staging entity position must be an integer from 1 through 999999')
  }
  return `sde_m_${stageId}_${String(position).padStart(6, '0')}`
}

export class StagingPlan {
  constructor(readonly stageId: string, readonly projectId: string, readonly group: string,
    readonly current: PlacementMap, readonly prepared: PlacementMap, readonly verifiedWith: string | null) {
    Object.freeze(this)
  }
  private loaded() {
    const saved = provenance.get(this)
    if (saved === undefined) throw new MigrationRefused('staging requires an immutable loaded authorization')
    return saved
  }
  get fingerprint(): string | undefined { return provenance.get(this)?.fingerprint }
  asRecord(): Record<string, unknown> { return JSON.parse(this.loaded().document) as Record<string, unknown> }
  preparedPayload(): Uint8Array { return canonicalBytes(this.asRecord()['prepared']) }
  checkCurrent(current: PlacementMap): void {
    this.loaded()
    checkMapProject(current, this.projectId)
    if (!current.signed || fingerprintOf(current) !== fingerprintOf(this.current)) {
      throw new MigrationRefused('staging authorization does not name the signed current map')
    }
  }
}

function load(raw: unknown, model: LogicalModel, projectId: string, publicKey: PublicKeys): StagingPlan {
  const body = record(structuredClone(raw), 'authorization')
  if (!equal(Object.keys(body).sort(compareCodePoints), FIELDS)) throw new MigrationRefused('staging authorization has missing or unknown fields')
  if (typeof body['protocol'] !== 'number' || body['protocol'] !== STAGING_PROTOCOL || body['kind'] !== 'sde-stage') {
    throw new MigrationRefused('unsupported staging authorization kind or protocol')
  }
  const identity = hex(body['stage_id'], 32, 'stage_id'), local = hex(body['project_id'], 32, 'project_id')
  if (local !== projectId) throw new MigrationRefused('staging authorization belongs to another local project')
  const group = body['group']
  if (typeof group !== 'string' || group.length === 0) throw new MigrationRefused('staging group must be a nonempty string')
  signature(body)
  const verified = verifyMapSignature(body, publicKey)
  const maps: PlacementMap[] = []
  for (const name of ['current', 'prepared']) {
    const document = record(body[name], name)
    signature(document)
    for (const rawGroup of Object.values(record(document['groups'], 'groups'))) {
      const value = record(rawGroup, 'group'), copies = value['derived'] === undefined ? [] : value['derived']
      if (!Array.isArray(copies)) throw new MigrationRefused('staging derived copies must be an array')
      for (const rawMaterial of [value['source'], ...copies]) {
        const layout = record(record(rawMaterial, 'materialization')['layout'], 'layout')
        if (layout['auto']) throw new MigrationRefused('staging maps need explicit physical layouts')
      }
    }
    const parsed = loadMap(document, { model, publicKey, requireSignature: true })
    if (parsed.contract !== 4) throw new MigrationRefused('staging protocol 1 requires map contract 4')
    checkMapProject(parsed, projectId)
    for (const placed of Object.values(parsed.groups)) {
      for (const material of [placed.source, ...placed.derived]) {
        if (Object.keys(material.layout.tables).length === 0 || Object.keys(material.layout.columns).length === 0) {
          throw new MigrationRefused('staging maps need explicit physical layouts')
        }
      }
    }
    maps.push(parsed)
  }
  const current = maps[0]!, prepared = maps[1]!
  if (current.mapVersion >= prepared.mapVersion) throw new MigrationRefused('staging must allocate a newer prepared map')
  if (!(group in current.groups) || !equal(Object.keys(current.groups).sort(compareCodePoints), Object.keys(prepared.groups).sort(compareCodePoints))) {
    throw new MigrationRefused('staging cannot add or remove colocation groups')
  }
  const old = current.groups[group]!, next = prepared.groups[group]!
  if (old.derived.length !== 0 || old.alsoWrite.length !== 0) throw new MigrationRefused('staging begins with a source-only group')
  if (next.derived.length !== 1 || next.alsoWrite.length !== 1 || next.alsoWrite[0]!.id !== next.derived[0]!.id) {
    throw new MigrationRefused('staging prepares exactly one maintained copy')
  }
  const epoch = old.writeEpoch as number
  if (epoch > MAX_EPOCH - 2 || next.writeEpoch !== epoch) throw new MigrationRefused('staging retains the source generation and needs two spare generations')
  if (next.derived[0]!.engine === old.source.engine) throw new MigrationRefused('staging target must use another engine binding')
  const currentRaw = record(body['current'], 'current'), preparedRaw = record(body['prepared'], 'prepared')
  const oldGroups = record(currentRaw['groups'], 'groups'), newGroups = record(preparedRaw['groups'], 'groups')
  const oldGroup = record(oldGroups[group], 'group'), newGroup = record(newGroups[group], 'group')
  if (!equal(Object.keys(oldGroup).sort(compareCodePoints), ['source', 'write_epoch']) ||
      !equal(Object.keys(newGroup).sort(compareCodePoints), ['also_write', 'derived', 'source', 'write_epoch'])) {
    throw new MigrationRefused('staging group shape is not source-only to one maintained copy')
  }
  if (!equal(oldGroup['source'], newGroup['source'])) throw new MigrationRefused('staging cannot change the existing source')
  const ignored = ['signature', 'map_version', 'groups']
  if (!equal(except(currentRaw, ignored), except(preparedRaw, ignored)) || !equal(current.routing, prepared.routing)) {
    throw new MigrationRefused('staging cannot change routing or other map attributes')
  }
  for (const other of Object.keys(current.groups)) {
    if (other !== group && !equal(oldGroups[other], newGroups[other])) throw new MigrationRefused('staging cannot change an unaffected group')
  }
  const members = colocationGroups(model).find(value => value.name === group)!
  const expected = Object.fromEntries([...members.members].sort(compareCodePoints).map((entity, index) => [entity, stagingTableName(identity, index + 1)]))
  if (!equal(next.derived[0]!.layout.tables, expected)) throw new MigrationRefused('staging needs fresh physical names bound to its stage id and entity order')
  const columns = groupColumns(model, members)
  for (const material of [old.source, next.derived[0]!]) {
    if (!equal(Object.keys(material.layout.tables).sort(compareCodePoints), Object.keys(columns).sort(compareCodePoints)) ||
        !equal(Object.keys(material.layout.columns).sort(compareCodePoints), Object.keys(columns).sort(compareCodePoints))) {
      throw new MigrationRefused('staging layouts must cover exactly the group entities')
    }
    for (const [entity, fields] of Object.entries(columns)) {
      if (!equal(Object.keys(material.layout.columns[entity]!).sort(compareCodePoints), Object.keys(fields).sort(compareCodePoints))) {
        throw new MigrationRefused('staging layout columns must match the logical group')
      }
    }
  }
  const used = new Set(Object.values(current.groups).flatMap(placed => [placed.source, ...placed.derived].flatMap(material => Object.values(material.layout.tables))))
  if (Object.values(expected).some(name => used.has(name))) throw new MigrationRefused('staging cannot reuse a current physical name')
  const plan = new StagingPlan(identity, local, group, current, prepared, verified)
  provenance.set(plan, { document: canonicalBytes(body).toString('utf8'),
    fingerprint: createHash('sha256').update(canonicalBytes(except(body, ['signature']))).digest('hex') })
  return plan
}

export function loadStagingPlan(raw: unknown, options: { model: LogicalModel; projectId: string; publicKey: PublicKeys }): StagingPlan {
  try { return load(raw, options.model, options.projectId, options.publicKey) }
  catch (error) {
    if (error instanceof MapError || error instanceof CanonicalError) throw new MigrationRefused(`staging authorization refused: ${error.message}`)
    throw error
  }
}

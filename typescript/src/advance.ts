/**
 * Signed authorization to advance one group's write generation, and nothing else.
 *
 * Advancing a group's generation by one, with its placement as it was, makes one fact true from
 * then on: a row that still carries an earlier generation has not changed since. The packet names
 * the map in force and the map that follows it, both signed; the second is the first with a newer
 * version and the group's generation raised by one, byte for byte otherwise. Only the reference's
 * operator executes it. Every library loads it by the same rules (`docs/generation-advance.md`).
 */
import { createHash } from 'node:crypto'
import { CanonicalError, canonicalBytes, compareCodePoints } from './canonical.js'
import { MapError, MigrationRefused } from './errors.js'
import { checkMapProject, GENERATIONS_SINCE, MAX_EPOCH } from './generation.js'
import type { LogicalModel } from './model.js'
import { fingerprintOf, loadMap, verifyMapSignature, type LoadOptions, type PlacementMap } from './placement.js'

export const ADVANCE_PROTOCOL = 1
const NEEDS_LAYOUTS = 'advance maps need explicit physical layouts'
const FIELDS = ['kind', 'protocol', 'advance_id', 'project_id', 'group', 'pause_budget_ms', 'before', 'after', 'signature']
  .sort(compareCodePoints)
type PublicKeys = NonNullable<LoadOptions['publicKey']>
const provenance = new WeakMap<AdvancePlan, { document: string; fingerprint: string }>()

function record(value: unknown, name: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new MigrationRefused(`advance ${name} must be an object`)
  }
  return value as Record<string, unknown>
}
function hex(value: unknown, width: number, name: string): string {
  if (typeof value !== 'string' || !new RegExp(`^[0-9a-f]{${width}}$`).test(value)) {
    throw new MigrationRefused(`advance ${name} must be ${width} lowercase hexadecimal digits`)
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
    throw new MigrationRefused('advance signatures have missing or unknown fields')
  }
  if ((value['alg'] !== 'ed25519' && value['alg'] !== 'es256') || typeof value['value'] !== 'string') {
    throw new MigrationRefused('advance signatures must use ed25519 or es256 and canonical base64')
  }
  const decoded = Buffer.from(value['value'], 'base64')
  if (decoded.length !== 64 || decoded.toString('base64') !== value['value']) {
    throw new MigrationRefused('advance signatures must use ed25519 or es256 and canonical base64')
  }
  if ('key_id' in value && typeof value['key_id'] !== 'string') {
    throw new MigrationRefused('advance signature key_id must be a string')
  }
}
function except(value: Record<string, unknown>, keys: readonly string[]): Record<string, unknown> {
  return Object.fromEntries(Object.entries(value).filter(([name]) => !keys.includes(name)))
}

export class AdvancePlan {
  constructor(readonly advanceId: string, readonly projectId: string, readonly group: string,
    readonly before: PlacementMap, readonly after: PlacementMap, readonly pauseBudgetMs: number,
    readonly verifiedWith: string | null) {
    Object.freeze(this)
  }
  private loaded() {
    const saved = provenance.get(this)
    if (saved === undefined) throw new MigrationRefused('an advance requires an immutable loaded authorization')
    return saved
  }
  get fingerprint(): string | undefined { return provenance.get(this)?.fingerprint }
  /** The generation the group's rows carry before the advance. */
  get sourceEpoch(): number {
    this.loaded()
    return this.before.groups[this.group]!.writeEpoch as number
  }
  /** The generation every write of the group carries after it. */
  get nextEpoch(): number { return this.sourceEpoch + 1 }
  asRecord(): Record<string, unknown> { return JSON.parse(this.loaded().document) as Record<string, unknown> }
  /** The signed map to publish, as its canonical bytes. */
  afterPayload(): Uint8Array { return canonicalBytes(this.asRecord()['after']) }
  checkCurrent(current: PlacementMap): void {
    this.loaded()
    checkMapProject(current, this.projectId)
    if (!current.signed || fingerprintOf(current) !== fingerprintOf(this.before)) {
      throw new MigrationRefused('advance authorization does not name the signed current map')
    }
  }
}

function load(raw: unknown, model: LogicalModel, projectId: string, publicKey: PublicKeys): AdvancePlan {
  const body = record(structuredClone(raw), 'authorization')
  if (!equal(Object.keys(body).sort(compareCodePoints), FIELDS)) throw new MigrationRefused('advance authorization has missing or unknown fields')
  if (typeof body['protocol'] !== 'number' || body['protocol'] !== ADVANCE_PROTOCOL || body['kind'] !== 'sde-advance') {
    throw new MigrationRefused('unsupported advance authorization kind or protocol')
  }
  const identity = hex(body['advance_id'], 32, 'advance_id'), local = hex(body['project_id'], 32, 'project_id')
  if (local !== projectId) throw new MigrationRefused('advance authorization belongs to another local project')
  const group = body['group']
  if (typeof group !== 'string' || group.length === 0) throw new MigrationRefused('advance group must be a nonempty string')
  const budget = body['pause_budget_ms']
  if (typeof budget !== 'number' || !Number.isSafeInteger(budget) || budget < 1) {
    throw new MigrationRefused('advance pause_budget_ms must be a positive safe integer')
  }
  signature(body)
  const verified = verifyMapSignature(body, publicKey)
  const maps: PlacementMap[] = []
  for (const name of ['before', 'after']) {
    const document = record(body[name], name)
    signature(document)
    // The operator fences the tables these maps name. A layout left to be derived would be derived
    // by whichever library loads the map, so nobody signed the tables it names. The member is
    // refused whatever it says: whether a value asks for it is each language's own notion of a true
    // value (`[]` is one here and not in Python), and no signed map has a reason to carry it.
    for (const rawGroup of Object.values(record(document['groups'], 'groups'))) {
      const value = record(rawGroup, 'group'), copies = value['derived'] === undefined ? [] : value['derived']
      if (!Array.isArray(copies)) throw new MigrationRefused('advance derived copies must be an array')
      for (const rawMaterial of [value['source'], ...copies]) {
        const layout = record(record(rawMaterial, 'materialization')['layout'], 'layout')
        if (Object.hasOwn(layout, 'auto')) throw new MigrationRefused(NEEDS_LAYOUTS)
      }
    }
    const parsed = loadMap(document, { model, publicKey, requireSignature: true })
    if (parsed.contract < GENERATIONS_SINCE) {
      throw new MigrationRefused(
        `an advance requires map contract ${GENERATIONS_SINCE} or later: it changes a write generation, ` +
          'and an earlier map has none',
      )
    }
    checkMapProject(parsed, projectId)
    for (const placed of Object.values(parsed.groups)) {
      for (const material of [placed.source, ...placed.derived]) {
        if (Object.keys(material.layout.tables).length === 0 || Object.keys(material.layout.columns).length === 0) {
          throw new MigrationRefused(NEEDS_LAYOUTS)
        }
      }
    }
    maps.push(parsed)
  }
  const before = maps[0]!, after = maps[1]!
  const beforeRaw = record(body['before'], 'before'), afterRaw = record(body['after'], 'after')
  if (before.mapVersion >= after.mapVersion) throw new MigrationRefused('an advance must allocate a newer map')
  if (!(group in before.groups)) throw new MigrationRefused(`an advance names a group the map in force does not have: ${group}`)
  if (!equal(Object.keys(before.groups).sort(compareCodePoints), Object.keys(after.groups).sort(compareCodePoints))) {
    throw new MigrationRefused('an advance cannot add or remove colocation groups')
  }
  const epoch = before.groups[group]!.writeEpoch
  if (epoch === undefined) {
    throw new MigrationRefused(
      `group ${group} carries no write generation, so there is none to advance: its engine cannot fence writes`,
    )
  }
  if (epoch >= MAX_EPOCH) throw new MigrationRefused('an advance needs one available write generation')
  const oldGroups = record(beforeRaw['groups'], 'groups'), newGroups = record(afterRaw['groups'], 'groups')
  for (const other of Object.keys(before.groups)) {
    if (other !== group && !equal(oldGroups[other], newGroups[other])) throw new MigrationRefused('an advance cannot change an unaffected group')
  }
  const oldGroup = record(oldGroups[group], 'group'), newGroup = record(newGroups[group], 'group')
  if (newGroup['write_epoch'] !== epoch + 1) throw new MigrationRefused("an advance raises the group's write generation by exactly one")
  if (!equal(except(oldGroup, ['write_epoch']), except(newGroup, ['write_epoch']))) {
    throw new MigrationRefused('an advance cannot change where the group is placed or which copies it maintains')
  }
  const ignored = ['signature', 'map_version', 'groups']
  if (!equal(except(beforeRaw, ignored), except(afterRaw, ignored))) {
    throw new MigrationRefused('an advance cannot change routing or other map attributes')
  }
  const plan = new AdvancePlan(identity, local, group, before, after, budget, verified)
  provenance.set(plan, { document: canonicalBytes(body).toString('utf8'),
    fingerprint: createHash('sha256').update(canonicalBytes(except(body, ['signature']))).digest('hex') })
  return plan
}

/** Verify and load a generation-advance packet, or refuse it whole. */
export function loadAdvancePlan(raw: unknown, options: { model: LogicalModel; projectId: string; publicKey: PublicKeys }): AdvancePlan {
  try { return load(raw, options.model, options.projectId, options.publicKey) }
  catch (error) {
    if (error instanceof MapError || error instanceof CanonicalError) throw new MigrationRefused(`advance authorization refused: ${error.message}`)
    throw error
  }
}

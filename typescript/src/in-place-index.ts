/**
 * Signed authorization to build indexes on the tables in force, in place.
 *
 * A design that only adds indexes to a group's source used to run as a relayout: a fresh copy with
 * the index, then a cutover that copies and compares every row while the source's writes are
 * frozen - a pause linear in the table. Building the index on the live table moves no row and
 * pauses no write. This authorization binds that build: the exact map in force and the next map,
 * which differs from it only by indexes added to one group's source. The Python library executes
 * it; this loader holds both libraries to one reading of the packet.
 *
 * Protocol 2 also removes indexes the map in force declares: the removed ones leave the next map,
 * the others keep their order, and the new ones follow. The operator removes them after its decision.
 */
import { createHash } from 'node:crypto'
import { CanonicalError, canonicalBytes, compareCodePoints } from './canonical.js'
import { MapError, MigrationRefused } from './errors.js'
import { checkMapProject, GENERATIONS_SINCE } from './generation.js'
import type { LogicalModel } from './model.js'
import { fingerprintOf, loadMap, verifyMapSignature, type LoadOptions, type PlacementMap } from './placement.js'

/** Indexes added to one group's source, built on the tables in force; no copy, no cutover. */
export const INDEX_PROTOCOL = 1
/** Indexes added to and removed from one group's source, in place; at least one removed. */
export const INDEX_CHANGE_PROTOCOL = 2
/** A day. The budget bounds a build on a server that stopped answering; nothing is paused while
 * it runs, so it is not a pause budget and it is deliberately far longer than a cutover's. */
export const MAX_BUILD_BUDGET_MS = 86_400_000
const FIELDS = ['kind', 'protocol', 'index_id', 'project_id', 'group', 'current', 'prepared', 'build_budget_ms', 'signature']
  .sort(compareCodePoints)
type PublicKeys = NonNullable<LoadOptions['publicKey']>
type Index = Readonly<Record<string, unknown>>
const provenance = new WeakMap<IndexPlan, { document: string; fingerprint: string }>()

function record(value: unknown, name: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new MigrationRefused(`index build ${name} must be an object`)
  }
  return value as Record<string, unknown>
}
function hex(value: unknown, width: number, name: string): string {
  if (typeof value !== 'string' || !new RegExp(`^[0-9a-f]{${width}}$`).test(value)) {
    throw new MigrationRefused(`index build ${name} must be ${width} lowercase hexadecimal digits`)
  }
  return value
}
function equal(left: unknown, right: unknown): boolean {
  return canonicalBytes(left).equals(canonicalBytes(right))
}
function sortedKeys(value: Record<string, unknown>): string[] {
  return Object.keys(value).sort(compareCodePoints)
}
function signature(document: Record<string, unknown>): void {
  const value = record(document['signature'], 'signature')
  const names = sortedKeys(value)
  if (!equal(names, ['alg', 'value']) && !equal(names, ['alg', 'key_id', 'value'])) {
    throw new MigrationRefused('index build signatures have missing or unknown fields')
  }
  const malformed = 'index build signatures must use ed25519 and canonical base64'
  if (value['alg'] !== 'ed25519' || typeof value['value'] !== 'string') throw new MigrationRefused(malformed)
  const decoded = Buffer.from(value['value'], 'base64')
  if (decoded.length !== 64 || decoded.toString('base64') !== value['value']) throw new MigrationRefused(malformed)
  if ('key_id' in value && typeof value['key_id'] !== 'string') {
    throw new MigrationRefused('index build signature key_id must be a string')
  }
}
function except(value: Record<string, unknown>, keys: readonly string[]): Record<string, unknown> {
  return Object.fromEntries(Object.entries(value).filter(([name]) => !keys.includes(name)))
}
function withoutIndexes(material: Record<string, unknown>): Record<string, unknown> {
  return { ...material, layout: except(record(material['layout'], 'layout'), ['indexes']) }
}
function indexesOf(material: Record<string, unknown>): unknown[] {
  const value = record(material['layout'], 'layout')['indexes']
  return Array.isArray(value) ? value : []
}
/** Every table and index name any layout of a map document uses. */
function names(document: Record<string, unknown>): Set<string> {
  const found = new Set<string>()
  for (const rawGroup of Object.values(record(document['groups'], 'groups'))) {
    const value = record(rawGroup, 'group')
    const copies = Array.isArray(value['derived']) ? value['derived'] : []
    for (const material of [value['source'], ...copies]) {
      const layout = record(record(material, 'materialization')['layout'], 'layout')
      for (const table of Object.values(record(layout['tables'] ?? {}, 'tables'))) found.add(String(table))
      for (const index of indexesOf(record(material, 'materialization'))) found.add(String(record(index, 'index')['name']))
    }
  }
  return found
}

/** The physical name of the `position`-th index an in-place build adds (one-based). */
export function indexBuildName(indexId: string, position: number): string {
  hex(indexId, 32, 'index_id')
  if (!Number.isInteger(position) || position < 1 || position > 999999) {
    throw new MigrationRefused('index build position must be an integer from 1 through 999999')
  }
  return `sde_i_${indexId}_${String(position).padStart(6, '0')}`
}

export class IndexPlan {
  constructor(readonly indexId: string, readonly projectId: string, readonly group: string,
    readonly current: PlacementMap, readonly prepared: PlacementMap, readonly buildBudgetMs: number,
    /** The new index definitions, in position order, exactly as the prepared map carries them. */
    readonly added: readonly Index[], readonly verifiedWith: string | null,
    /** Protocol 2: the definitions in force the next map drops, in their order in force. */
    readonly removed: readonly Index[] = []) {
    Object.freeze(this)
  }
  private loaded() {
    const saved = provenance.get(this)
    if (saved === undefined) throw new MigrationRefused('an index build requires an immutable loaded authorization')
    return saved
  }
  get fingerprint(): string | undefined { return provenance.get(this)?.fingerprint }
  get protocol(): number { return this.asRecord()['protocol'] as number }
  asRecord(): Record<string, unknown> { return JSON.parse(this.loaded().document) as Record<string, unknown> }
  preparedPayload(): Uint8Array { return canonicalBytes(this.asRecord()['prepared']) }
  checkCurrent(current: PlacementMap): void {
    this.loaded()
    checkMapProject(current, this.projectId)
    if (!current.signed || fingerprintOf(current) !== fingerprintOf(this.current)) {
      throw new MigrationRefused('index build authorization does not name the signed current map')
    }
  }
}

function load(raw: unknown, model: LogicalModel, projectId: string, publicKey: PublicKeys): IndexPlan {
  const body = record(structuredClone(raw), 'authorization')
  if (!equal(sortedKeys(body), FIELDS)) throw new MigrationRefused('index build authorization has missing or unknown fields')
  if (typeof body['protocol'] !== 'number' || ![INDEX_PROTOCOL, INDEX_CHANGE_PROTOCOL].includes(body['protocol']) ||
      body['kind'] !== 'sde-index') {
    throw new MigrationRefused('unsupported index build authorization kind or protocol')
  }
  const protocol = body['protocol']
  const identity = hex(body['index_id'], 32, 'index_id'), local = hex(body['project_id'], 32, 'project_id')
  if (local !== projectId) throw new MigrationRefused('index build authorization belongs to another local project')
  const group = body['group']
  if (typeof group !== 'string' || group.length === 0) throw new MigrationRefused('index build group must be a nonempty string')
  const budget = body['build_budget_ms']
  if (typeof budget !== 'number' || !Number.isInteger(budget) || budget < 1 || budget > MAX_BUILD_BUDGET_MS) {
    throw new MigrationRefused(`index build budget must be an integer from 1 through ${MAX_BUILD_BUDGET_MS} ms`)
  }
  signature(body)
  const verified = verifyMapSignature(body, publicKey)
  const maps: PlacementMap[] = []
  for (const name of ['current', 'prepared']) {
    const document = record(body[name], name)
    signature(document)
    for (const rawGroup of Object.values(record(document['groups'], 'groups'))) {
      const value = record(rawGroup, 'group'), copies = value['derived'] === undefined ? [] : value['derived']
      if (!Array.isArray(copies)) throw new MigrationRefused('index build derived copies must be an array')
      for (const rawMaterial of [value['source'], ...copies]) {
        const layout = record(record(rawMaterial, 'materialization')['layout'], 'layout')
        if (layout['auto']) throw new MigrationRefused('index build maps need explicit physical layouts')
      }
    }
    const parsed = loadMap(document, { model, publicKey, requireSignature: true })
    if (parsed.contract < GENERATIONS_SINCE) {
      throw new MigrationRefused(`index build protocol ${protocol} requires map contract ${GENERATIONS_SINCE} or later`)
    }
    checkMapProject(parsed, projectId)
    for (const placed of Object.values(parsed.groups)) {
      for (const material of [placed.source, ...placed.derived]) {
        if (Object.keys(material.layout.tables).length === 0 || Object.keys(material.layout.columns).length === 0) {
          throw new MigrationRefused('index build maps need explicit physical layouts')
        }
      }
    }
    maps.push(parsed)
  }
  const current = maps[0]!, prepared = maps[1]!
  // The prepared map may raise the contract, because an index method first appears here - a BRIN
  // index under a contract-4 current map. It may not lower it: a lower number would tell an older
  // library it may ignore keys that the current map already relies on.
  if (prepared.contract < current.contract) throw new MigrationRefused('an index build cannot lower the placement map contract')
  if (current.mapVersion >= prepared.mapVersion) throw new MigrationRefused('an index build must allocate a newer prepared map')
  if (!(group in current.groups) || !equal(Object.keys(current.groups).sort(compareCodePoints), Object.keys(prepared.groups).sort(compareCodePoints))) {
    throw new MigrationRefused('an index build cannot add or remove colocation groups')
  }
  const old = current.groups[group]!, next = prepared.groups[group]!
  if (old.derived.length !== 0 || old.alsoWrite.length !== 0 || next.derived.length !== 0 || next.alsoWrite.length !== 0) {
    throw new MigrationRefused('an index build begins and ends with a source-only group')
  }
  // Nothing is fenced: the tables stay and every running process keeps writing to them.
  if (next.writeEpoch !== old.writeEpoch) throw new MigrationRefused("an index build keeps the source's write generation")
  const currentRaw = record(body['current'], 'current'), preparedRaw = record(body['prepared'], 'prepared')
  const oldGroups = record(currentRaw['groups'], 'groups'), newGroups = record(preparedRaw['groups'], 'groups')
  const oldGroup = record(oldGroups[group], 'group'), newGroup = record(newGroups[group], 'group')
  if (!equal(sortedKeys(oldGroup), ['source', 'write_epoch']) || !equal(sortedKeys(newGroup), ['source', 'write_epoch'])) {
    throw new MigrationRefused('an index build group is a source and its write generation only')
  }
  const oldSource = record(oldGroup['source'], 'source'), newSource = record(newGroup['source'], 'source')
  if (!equal(withoutIndexes(oldSource), withoutIndexes(newSource))) {
    throw new MigrationRefused('an index build changes nothing about the source but its indexes; a new key order, ' +
      'partition, table or engine is a relayout or a move')
  }
  const inForce = indexesOf(oldSource), after = indexesOf(newSource)
  const nameOf = (index: unknown) => String(record(index, 'index')['name'])
  let kept = inForce, removed: unknown[] = []
  if (protocol === INDEX_PROTOCOL) {
    if (after.length <= kept.length || !equal(after.slice(0, kept.length), kept)) {
      throw new MigrationRefused('an index build keeps every index in force, in order, and adds at least one after them')
    }
  } else {
    const remaining = new Set(after.map(nameOf))
    kept = inForce.filter(index => remaining.has(nameOf(index)))
    removed = inForce.filter(index => !remaining.has(nameOf(index)))
    if (removed.length === 0) throw new MigrationRefused('index build protocol 2 removes at least one index in force')
    if (!equal(after.slice(0, kept.length), kept)) {
      throw new MigrationRefused('an index change keeps the other indexes in force, in order, before the new ones')
    }
  }
  const added = after.slice(kept.length)
  added.forEach((index, offset) => {
    if (record(index, 'index')['name'] !== indexBuildName(identity, offset + 1)) {
      throw new MigrationRefused('new indexes need fresh names bound to the index build id and their position')
    }
  })
  const used = names(currentRaw)
  if (added.some(index => used.has(String(record(index, 'index')['name'])))) {
    throw new MigrationRefused('an index build cannot reuse a name the current map uses')
  }
  const ignored = ['signature', 'map_version', 'groups', 'contract']
  if (!equal(except(currentRaw, ignored), except(preparedRaw, ignored)) || !equal(current.routing, prepared.routing)) {
    throw new MigrationRefused('an index build cannot change routing or other map attributes')
  }
  for (const other of Object.keys(current.groups)) {
    if (other !== group && !equal(oldGroups[other], newGroups[other])) throw new MigrationRefused('an index build cannot change an unaffected group')
  }
  // From the loaded map, which freezes nested structures, not from the caller's objects.
  const gone = new Set(removed.map(nameOf))
  const plan = new IndexPlan(identity, local, group, current, prepared, budget, next.source.layout.indexes.slice(kept.length),
    verified, old.source.layout.indexes.filter(index => gone.has(String(index['name']))))
  provenance.set(plan, { document: canonicalBytes(body).toString('utf8'),
    fingerprint: createHash('sha256').update(canonicalBytes(except(body, ['signature']))).digest('hex') })
  return plan
}

export function loadIndexPlan(raw: unknown, options: { model: LogicalModel; projectId: string; publicKey: PublicKeys }): IndexPlan {
  try { return load(raw, options.model, options.projectId, options.publicKey) }
  catch (error) {
    if (error instanceof MapError || error instanceof CanonicalError) throw new MigrationRefused(`index build authorization refused: ${error.message}`)
    throw error
  }
}

/**
 * The placement map: where every group lives, and where every operation goes.
 *
 * The only instruction this library takes from outside, and therefore the one input that refuses
 * rather than degrades. A bad signature, a mismatched model version, an unknown contract version:
 * all of them stop the library from starting, because the map decides where data is written.
 *
 * An unsigned map is valid - that is the no-account mode, and it is a supported way to use this
 * library rather than a gap. What is refused is a map carrying a signature, and therefore claiming to
 * come from us, when there is no key to check the claim against.
 */

import { createPublicKey, verify as verifySignature } from 'node:crypto'

import { canonicalBytes } from './canonical.js'
import { MapError } from './errors.js'
import { colocationGroups } from './groups.js'
import type { LogicalModel } from './model.js'
import { CONTRACT, entityOf } from './model.js'
import { enumerateShapes, shapeId } from './shapes.js'

export interface PhysicalLayout {
  readonly tables: Readonly<Record<string, string>>
  readonly columns: Readonly<Record<string, Readonly<Record<string, string>>>>
  readonly indexes: readonly Readonly<Record<string, unknown>>[]
  readonly partitionBy: Readonly<Record<string, string>>
}

export interface Materialization {
  readonly id: string
  readonly engine: string
  readonly layout: PhysicalLayout
  readonly lagBudgetMs: number | null
}

export interface GroupPlacement {
  readonly group: string
  readonly source: Materialization
  readonly derived: readonly Materialization[]
  /**
   * Derived copies that writes are **also** sent to, additionally and never authoritatively.
   *
   * This is how a migration reaches a library, and the reason no phase name appears anywhere in a
   * map. A library does not need to know what `DUAL_WRITE` means; it needs to know where writes go
   * and where reads go, and both were already things a map says. The phase in the document as well
   * would be a second representation of a fact the fan-out and the routing table already carry.
   *
   * This runtime has no engine adapters, so it never performs the fan-out - and it parses and
   * refuses exactly as the reference implementation does, because a fan-out target read differently
   * in two languages is a row written to one copy and not the other.
   */
  readonly alsoWrite: readonly Materialization[]
}

export interface PlacementMap {
  readonly contract: number
  readonly modelVersion: string
  readonly mapVersion: number
  readonly groups: Readonly<Record<string, GroupPlacement>>
  readonly routing: Readonly<Record<string, string>>
  readonly signed: boolean
}

export interface LoadOptions {
  readonly model?: LogicalModel
  /** Raw 32-byte Ed25519 public key. */
  readonly publicKey?: Uint8Array
  readonly requireSignature?: boolean
}

export const MAP_CONTRACT = 2
/**
 * The placement map's format version, which is not the IR's - see `CONTRACT`.
 *
 * Two because the map gained `also_write`, which a contract-1 library would ignore while a
 * contract-2 one honours it: the same document, two different sets of engines written to, and the
 * difference decided by which version happens to be installed. A loosening bumps the number.
 */

export const ALSO_WRITE_SINCE = 2
/**
 * The contract that introduced `also_write`.
 *
 * A document declaring an earlier contract and carrying the key is refused. That is a *tightening*
 * - no bump - and it exists because of who makes the mistake: a producer that grew the key and
 * forgot to raise the number, which is what happened on the control-plane side of the very change
 * that added it.
 */

export const MAP_CONTRACT_FLOOR = 1
/**
 * The oldest map format this library still reads.
 *
 * Backwards compatible, forwards strict, and the asymmetry is knowledge rather than kindness: every
 * contract-1 document is a valid contract-2 one with a key absent, which reads as "no dual write"
 * and is a complete meaning. What came after this library cannot be known, so a higher number is
 * refused rather than interpreted.
 */

export const WATERMARK_TABLE = 'sde_map_state'
/**
 * The table the Python library keeps its own bookkeeping in: the highest map version applied
 * against an engine, which is what stops an older signed map being loaded over a newer one.
 *
 * This runtime has no engine adapters, so it never writes that table - and it refuses a layout
 * naming it anyway. The rule is a *parsing* rule, and a parsing rule that holds in one language
 * and not the other is exactly the divergence the conformance suite exists to catch: one map, two
 * libraries, accepted here and refused there. Nothing in this file could have discovered that on
 * its own.
 */

const AUTO = Symbol('auto-layout')
type MaybeLayout = PhysicalLayout | typeof AUTO

function asRecord(value: unknown, where: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new MapError(`${where}: expected an object`)
  }
  return value as Record<string, unknown>
}

function readLayout(raw: unknown, where: string): MaybeLayout {
  const body = asRecord(raw, `${where}: layout`)
  if (body['auto'] === true) {
    if (Object.keys(body).length !== 1) {
      throw new MapError(
        `${where}: a layout is either auto or explicit, not both. Two sources of truth for a schema ` +
          'is how a column ends up existing in one place and not the other.',
      )
    }
    return AUTO
  }
  const tables = body['tables']
  if (typeof tables !== 'object' || tables === null || Object.keys(tables).length === 0) {
    throw new MapError(
      `${where}: layout needs a non-empty 'tables' mapping entity to table name, or {"auto": true} ` +
        'to have one derived from the model',
    )
  }
  const reserved = Object.entries(tables as Record<string, string>)
    .filter(([, table]) => table === WATERMARK_TABLE)
    .map(([entity]) => entity)
    .sort()
  if (reserved.length > 0) {
    throw new MapError(
      `${where}: ${JSON.stringify(reserved)} would be stored in a table called ` +
        `'${WATERMARK_TABLE}', which this library keeps its own bookkeeping in - the highest map ` +
        'version applied against an engine, which is what stops an older map from being loaded ' +
        'over a newer one. A client table under that name would be read as bookkeeping and ' +
        'written to as bookkeeping. Rename the table; the name is yours to choose everywhere else.',
    )
  }
  return {
    tables: tables as Record<string, string>,
    columns: (body['columns'] ?? {}) as PhysicalLayout['columns'],
    indexes: (body['indexes'] ?? []) as PhysicalLayout['indexes'],
    partitionBy: (body['partition_by'] ?? {}) as Record<string, string>,
  }
}

/**
 * The derived copies writes are additionally sent to. Four refusals, each with its failure.
 *
 * Validated when the map loads rather than at the first write, for the reason the routing table
 * was: a map is handed over once and obeyed for months, so a defect in it should be found when it
 * arrives and not by the request that happens to touch it. During a migration that request is a
 * write, and the failure is a write that goes to one engine when the map says two.
 */
function readAlsoWrite(
  raw: unknown,
  where: string,
  contract: number,
  source: Materialization,
  derived: readonly Materialization[],
): readonly Materialization[] {
  if (raw === undefined || raw === null) return []
  if (contract < ALSO_WRITE_SINCE) {
    throw new MapError(
      `${where}: this map declares format contract ${contract} and uses 'also_write', which ` +
        `contract ${ALSO_WRITE_SINCE} introduced. Refused rather than honoured, and the reason is ` +
        'who makes this mistake: a producer that grew the key and forgot to raise the number. ' +
        'Honouring it would mean a contract-1 library ignoring the fan-out while a contract-2 one ' +
        'performs it - the same document, two sets of engines written to - which is the whole ' +
        'failure the version exists to prevent.',
    )
  }
  if (!Array.isArray(raw) || raw.length === 0) {
    throw new MapError(
      `${where}: 'also_write' is a non-empty list of materialisation ids, or absent. Absent means ` +
        'writes go to the source alone; an empty list would be a claim that fan-out was ' +
        'considered and none chosen, which is a stronger thing to say and not what the planner ' +
        'means when it omits the key.',
    )
  }
  const byId = new Map(derived.map((m) => [m.id, m]))
  const out: Materialization[] = []
  const seen = new Set<string>()
  for (const entry of raw) {
    if (typeof entry !== 'string') {
      throw new MapError(
        `${where}: 'also_write' holds materialisation ids, not ${JSON.stringify(entry)}`,
      )
    }
    if (entry === source.id) {
      throw new MapError(
        `${where}: 'also_write' names the source '${entry}'. The source is where writes already ` +
          'land - listing it would either write the row twice or read as though the source were ' +
          'somehow optional, and both are worse than the refusal.',
      )
    }
    if (seen.has(entry)) {
      throw new MapError(
        `${where}: 'also_write' names '${entry}' twice. Two identical fan-out targets is either a ` +
          'duplicated row or a document nobody meant to write.',
      )
    }
    const found = byId.get(entry)
    if (found === undefined) {
      throw new MapError(
        `${where}: 'also_write' names '${entry}', which is not a derived materialisation of this ` +
          `group. It has ${JSON.stringify([...byId.keys()].sort())}. A fan-out target that does ` +
          'not exist is a write with nowhere to go, and during a migration that is a row the copy ' +
          'never receives.',
      )
    }
    seen.add(entry)
    out.push(found)
  }
  return out
}

function readMaterialization(
  raw: unknown,
  where: string,
  isSource: boolean,
): { readonly mat: Omit<Materialization, 'layout'>; readonly layout: MaybeLayout } {
  const body = asRecord(raw, where)
  for (const required of ['id', 'engine', 'layout']) {
    if (!(required in body)) throw new MapError(`${where}: materialisation is missing '${required}'`)
  }
  const lag = body['lag_budget_ms']
  if (isSource && lag !== undefined && lag !== null) {
    throw new MapError(
      `${where}: the source materialisation cannot have a lag budget. The source is where writes ` +
        'land, so it is by definition not behind anything.',
    )
  }
  if (!isSource && (lag === undefined || lag === null)) {
    throw new MapError(
      `${where}: a derived materialisation needs lag_budget_ms. Without it nobody - not the client, ` +
        'not the monitoring - can tell whether it is healthy or hours behind.',
    )
  }
  return {
    mat: {
      id: String(body['id']),
      engine: String(body['engine']),
      lagBudgetMs: isSource ? null : Number(lag),
    },
    layout: readLayout(body['layout'], where),
  }
}

/**
 * Derive the obvious schema for a group.
 *
 * Tier 0 does not create schema, so this only has to produce the same *names* the Python
 * implementation does - which is what a routing decision depends on. Column types are a Tier 2
 * concern and are deliberately left empty rather than guessed at half-right.
 */
function defaultLayout(model: LogicalModel, members: readonly string[]): PhysicalLayout {
  const tables: Record<string, string> = {}
  for (const member of members) {
    entityOf(model, member)
    tables[member] = member
      .normalize('NFC')
      .replace(/(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])/g, '_')
      .toLowerCase()
  }
  return { tables, columns: {}, indexes: [], partitionBy: {} }
}

function verifyMapSignature(raw: Record<string, unknown>, publicKey: Uint8Array): void {
  const signature = asRecord(raw['signature'], 'signature')
  if (signature['alg'] !== 'ed25519') {
    throw new MapError('only ed25519 signatures are understood')
  }
  const value = Buffer.from(String(signature['value']), 'base64')
  const { signature: _omit, ...rest } = raw
  const payload = canonicalBytes(rest)

  // Node wants a KeyObject; a raw 32-byte Ed25519 key becomes one via a minimal DER wrapper. The
  // alternative is asking callers for PEM, which is a worse interface for a key that is 32 bytes.
  const der = Buffer.concat([
    Buffer.from('302a300506032b6570032100', 'hex'),
    Buffer.from(publicKey),
  ])
  const key = createPublicKey({ key: der, format: 'der', type: 'spki' })

  if (!verifySignature(null, payload, key, value)) {
    throw new MapError(
      "the map's signature does not verify. This is refused rather than warned about: the map " +
        'decides where your data is written.',
    )
  }
}

/**
 * Every routing entry must name a materialisation that exists, in the shape's own group.
 *
 * This lived in `materializationById` - that is, at the first read that routed through the broken
 * entry. Deferring it there turns a mistake in a document we hand over into an error inside the
 * client's request path at a moment nobody can predict: only the shapes routing through that entry
 * fail, so a run that never issues those operations is green and the map looks applied.
 *
 * Two levels, because the model is optional. Without one: the target must be an id declared
 * somewhere in this map. With one: it must be declared in the group the shape belongs to - the
 * check that matters, since ids are unique only *within* a group.
 */
function checkRoutingTargets(
  routing: Record<string, unknown>,
  groups: Record<string, GroupPlacement>,
  model: LogicalModel | undefined,
): void {
  const declared: Record<string, Set<string>> = Object.create(null)
  const everywhere = new Set<string>()
  for (const [name, placement] of Object.entries(groups)) {
    const ids = new Set([placement.source, ...placement.derived].map((m) => m.id))
    declared[name] = ids
    for (const id of ids) everywhere.add(id)
  }

  const entries = Object.entries(routing).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))

  for (const [shapeIdent, target] of entries) {
    if (typeof target !== 'string') {
      throw new MapError(
        `the routing entry for shape '${shapeIdent}' is not a materialisation id. Routing maps a ` +
          'shape id to one id, and anything else is a table nobody can look up.',
      )
    }
    if (!everywhere.has(target)) {
      throw new MapError(
        `the routing table sends shape '${shapeIdent}' to materialisation '${target}', and no ` +
          'group in this map declares one with that id. The map is internally inconsistent: ' +
          `declared ids are ${JSON.stringify([...everywhere].sort())}.`,
      )
    }
  }

  if (!model) return

  const shapes = new Map(enumerateShapes(model).map((shape) => [shapeId(shape), shape]))
  for (const [shapeIdent, target] of entries) {
    const shape = shapes.get(shapeIdent)
    if (!shape) {
      throw new MapError(
        `the routing table has an entry for shape '${shapeIdent}', and this model does not produce ` +
          'that shape. The model version matched, so the two sides enumerated shapes differently - ' +
          "which is the divergence that puts one library's write in a table another library never " +
          'looks at.',
      )
    }
    if (!declared[shape.group]?.has(target as string)) {
      throw new MapError(
        `the routing table sends shape '${shapeIdent}' - which belongs to group '${shape.group}' - ` +
          `to materialisation '${target as string}', which that group does not declare. ` +
          'Materialisation ids are unique only within a group, so this would read ' +
          `${shape.entity} out of a copy that does not hold it.`,
      )
    }
  }
}

export function loadMap(raw: unknown, options: LoadOptions = {}): PlacementMap {
  const body = asRecord(raw, 'a placement map')

  const contract = body['contract']
  if (typeof contract !== 'number' || !Number.isInteger(contract)) {
    throw new MapError(
      `this map declares format contract ${JSON.stringify(contract)}, which is not a version ` +
        `number. This library reads ${MAP_CONTRACT_FLOOR} to ${MAP_CONTRACT}.`,
    )
  }
  if (contract < MAP_CONTRACT_FLOOR) {
    throw new MapError(
      `this map declares format contract ${contract} and the oldest this library still reads is ` +
        `${MAP_CONTRACT_FLOOR}.`,
    )
  }
  if (contract > MAP_CONTRACT) {
    throw new MapError(
      `this map declares format contract ${contract} and this library implements ` +
        `${MAP_CONTRACT}. Refusing rather than guessing: a version this library does not know may ` +
        'say something with a key it has never heard of, and the difference between two contract ' +
        'versions is exactly the kind of thing that would otherwise be interpreted as a missing ' +
        'field meaning zero. Upgrade the library.',
    )
  }

  const modelVersion = body['model_version']
  if (typeof modelVersion !== 'string' || modelVersion.length === 0) {
    throw new MapError('the map does not say which model version it is for')
  }
  if (options.model && options.model.version !== modelVersion) {
    throw new MapError(
      `this map is for model version ${modelVersion} and your declared model is ` +
        `${options.model.version}. Something changed in your entities; ask for a new map rather than ` +
        'running against this one, because the difference cannot be guessed.',
    )
  }

  const signaturePresent = body['signature'] !== undefined && body['signature'] !== null
  if (options.requireSignature === true && !signaturePresent) {
    throw new MapError('a signature was required and this map has none')
  }
  if (signaturePresent) {
    if (!options.publicKey) {
      throw new MapError(
        'this map is signed, which is a claim that it came from us, and no public key was provided ' +
          'to check that claim. Either pass the key, or use an unsigned map - an unsigned map is a ' +
          'supported mode, an unverifiable claim is not.',
      )
    }
    verifyMapSignature(body, options.publicKey)
  }

  const groupsRaw = body['groups']
  if (typeof groupsRaw !== 'object' || groupsRaw === null) {
    throw new MapError('the map places no groups')
  }

  const modelGroups = options.model ? colocationGroups(options.model) : []
  const membersOf = new Map(modelGroups.map((g) => [g.name, g.members]))

  const groups: Record<string, GroupPlacement> = {}
  for (const [name, value] of Object.entries(groupsRaw as Record<string, unknown>)) {
    const where = `group '${name}'`
    const placement = asRecord(value, where)
    if (!('source' in placement)) {
      throw new MapError(`${where}: needs a 'source' materialisation`)
    }

    const resolve = (layout: MaybeLayout, what: string): PhysicalLayout => {
      if (layout !== AUTO) return layout
      const members = membersOf.get(name)
      if (!members) {
        throw new MapError(
          `${where}.${what}: a layout asked to be derived with {"auto": true}, but no model was ` +
            'supplied to derive it from. Pass model to loadMap().',
        )
      }
      return defaultLayout(options.model!, members)
    }

    const sourceRead = readMaterialization(placement['source'], `${where}.source`, true)
    const source: Materialization = {
      ...sourceRead.mat,
      layout: resolve(sourceRead.layout, 'source'),
    }

    const derivedRaw = (placement['derived'] ?? []) as unknown[]
    const derived: Materialization[] = derivedRaw.map((entry, i) => {
      const read = readMaterialization(entry, `${where}.derived[${i}]`, false)
      return { ...read.mat, layout: resolve(read.layout, `derived[${i}]`) }
    })

    const ids = [source, ...derived].map((m) => m.id)
    if (new Set(ids).size !== ids.length) {
      throw new MapError(`${where}: two materialisations share an id`)
    }

    // A derived copy in the same engine, naming the same tables, is the source with a second name in
    // the map: its lag would always read as zero and a read routed to it would silently be a read of
    // the source. Found by a Python test; refused here for the same reason.
    const sourceTables = new Set(Object.values(source.layout.tables))
    for (const candidate of derived) {
      if (candidate.engine !== source.engine) continue
      const shared = Object.values(candidate.layout.tables)
        .filter((t) => sourceTables.has(t))
        .sort()
      if (shared.length > 0) {
        throw new MapError(
          `${where}: materialisation '${candidate.id}' is in the same engine as the source and ` +
            `reuses its tables ${JSON.stringify(shared)}. That is not a copy of the group, it is ` +
            'the original with a second name in the map.',
        )
      }
    }

    groups[name] = {
      group: name,
      source,
      derived,
      alsoWrite: readAlsoWrite(placement['also_write'], where, contract, source, derived),
    }
  }

  if (options.model) {
    const declaredNames = modelGroups.map((g) => g.name)
    const missing = declaredNames.filter((name) => !(name in groups)).sort()
    if (missing.length > 0) {
      throw new MapError(
        `the map does not place these groups: ${JSON.stringify(missing)}. Every group in the model ` +
          'needs a home before anything can run.',
      )
    }
    // And the other direction. Checking one of the two reads as complete: Python fell through to a
    // dict lookup and produced a bare KeyError, this one accepted the map in silence. Two
    // languages, one missing check, two different wrong answers.
    const unknown = Object.keys(groups)
      .filter((name) => !declaredNames.includes(name))
      .sort()
    if (unknown.length > 0) {
      throw new MapError(
        `the map places groups this model does not have: ${JSON.stringify(unknown)}. The model ` +
          'version matched, so this is not a stale map: the two sides derived colocation groups ' +
          'differently, and a group nobody declared has no entities to hold.',
      )
    }
  }

  const routingRaw = body['routing'] ?? {}
  if (typeof routingRaw !== 'object' || routingRaw === null) {
    throw new MapError("'routing' must be a mapping from shape id to materialisation id")
  }

  checkRoutingTargets(routingRaw as Record<string, unknown>, groups, options.model)

  return {
    contract: CONTRACT,
    modelVersion,
    mapVersion: Number(body['map_version'] ?? 0),
    groups,
    routing: routingRaw as Record<string, string>,
    signed: signaturePresent,
  }
}

export function placementOf(map: PlacementMap, group: string): GroupPlacement {
  const found = map.groups[group]
  if (!found) {
    throw new MapError(
      `no placement for group '${group}'. Every group in the model needs one: a group with nowhere ` +
        'to live is not a slow path, it is an unanswerable operation.',
    )
  }
  return found
}

export function materializationById(placement: GroupPlacement, id: string): Materialization {
  const found = [placement.source, ...placement.derived].find((m) => m.id === id)
  if (!found) {
    throw new MapError(
      `group '${placement.group}' has no materialisation '${id}', but the routing table points at ` +
        'it. The map is internally inconsistent.',
    )
  }
  return found
}

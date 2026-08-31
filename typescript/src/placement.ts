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
  return {
    tables: tables as Record<string, string>,
    columns: (body['columns'] ?? {}) as PhysicalLayout['columns'],
    indexes: (body['indexes'] ?? []) as PhysicalLayout['indexes'],
    partitionBy: (body['partition_by'] ?? {}) as Record<string, string>,
  }
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

  if (body['contract'] !== CONTRACT) {
    throw new MapError(
      `this map declares format contract ${JSON.stringify(body['contract'])} and this library ` +
        `implements ${CONTRACT}. Refusing rather than guessing: the difference between two contract ` +
        'versions is exactly the kind of thing that would otherwise be interpreted as a missing ' +
        'field meaning zero.',
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

    groups[name] = { group: name, source, derived }
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

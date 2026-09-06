/**
 * Declaring a model, and turning it into canonical bytes plus a version.
 *
 * The declaration is explicit because it has to be - see `types.ts`. What that buys, unexpectedly, is
 * that this file is shorter and clearer than its Python counterpart: there is no annotation
 * resolution, no forward references, no scope in which a name might or might not be visible. The
 * whole class of bugs Python needed `localns` for does not exist here.
 *
 * Two rules from the contract run through everything below:
 *
 * - Arrays are sorted wherever order carries no meaning, **by code point**, not with `.sort()`.
 * - Where order does carry meaning it is an explicit `position` inside each element. Composite keys
 *   are the case that matters: `(tenant, id)` and `(id, tenant)` are different keys, and a reader
 *   should not have to know which arrays here are load-bearing.
 */

import { canonicalBytes, compareCodePoints, digest16 } from './canonical.js'
import { DeclarationError } from './errors.js'
import type { FieldType, NeutralType } from './types.js'
import { checkType } from './types.js'

/**
 * The **IR's** format version, and the one the conformance vectors are pinned to.
 *
 * Separate from `MAP_CONTRACT` since the placement map gained a key the IR did not. One counter for
 * two artefacts means every artefact's version moves when any one of them changes - and `contract`
 * is *inside* the IR, whose digest is `model_version`, so bumping it for a change to the map format
 * would give every client a new model version and invalidate every issued map. For a key in a
 * different document.
 */
export const CONTRACT = 1

export interface FieldSpec {
  readonly name: string
  readonly type: NeutralType
  readonly nullable: boolean
}

export interface RelationSpec {
  readonly name: string
  readonly source: string
  readonly target: string
}

export interface EntitySpec {
  readonly name: string
  readonly fields: readonly FieldSpec[]
  readonly key: readonly string[]
  readonly pii: readonly string[]
  readonly residency: string | null
}

export interface CostCeiling {
  readonly amount: string
  readonly currency: string
}

export interface LogicalModel {
  readonly entities: readonly EntitySpec[]
  readonly relations: readonly RelationSpec[]
  readonly atomic: readonly (readonly string[])[]
  readonly costCeiling: CostCeiling | null
  readonly ir: Record<string, unknown>
  readonly version: string
}

/** What `entity()` takes. Everything except `fields` is optional, and absence means no constraint. */
export interface EntityDeclaration {
  readonly fields: Readonly<Record<string, FieldType>>
  readonly relations?: Readonly<Record<string, { readonly to: string }>>
  readonly key?: readonly string[]
  readonly pii?: readonly string[]
  readonly residency?: string
  readonly atomicWith?: readonly string[]
}

export interface Entity extends EntityDeclaration {
  readonly name: string
}

/** Declare an entity. */
export function entity(name: string, declaration: EntityDeclaration): Entity {
  if (!name) throw new DeclarationError('an entity needs a name')
  if (Object.keys(declaration.fields).length === 0) {
    throw new DeclarationError(
      `${name} has no fields. An entity that stores nothing cannot be placed, and a relation-only ` +
        'entity is usually a join table that wants to be one.',
    )
  }
  return { name, ...declaration }
}

/** Declare a relation target. `relations: { user: ref('User') }`. */
export function ref(to: string): { readonly to: string } {
  return { to }
}

function resolveKey(ent: Entity, fieldNames: Set<string>): readonly string[] {
  if (ent.key !== undefined) {
    if (ent.key.length === 0) throw new DeclarationError(`${ent.name}: key is empty`)
    const missing = ent.key.filter((k) => !fieldNames.has(k))
    if (missing.length > 0) {
      throw new DeclarationError(
        `${ent.name}: key names ${JSON.stringify(missing)}, which are not fields of ${ent.name}. A ` +
          'relation cannot be part of a key: the key has to be storable in the entity itself.',
      )
    }
    return ent.key
  }
  if (fieldNames.has('id')) return ['id']
  throw new DeclarationError(
    `${ent.name} has no 'id' field and no key. Every entity needs a key: without one there is no ` +
      'way to address a row, no way to migrate it and no way to verify a migration moved it.',
  )
}

/**
 * Merge pairwise `atomicWith` declarations into sorted groups.
 *
 * Symmetric even when declared on one side, and transitive: if A is atomic with B and B with C then
 * all three commit together, because nothing else is implementable on one engine's transaction.
 */
function normaliseAtomic(entities: readonly Entity[]): readonly (readonly string[])[] {
  const parent = new Map<string, string>()
  for (const ent of entities) parent.set(ent.name, ent.name)

  const find = (x: string): string => {
    let cur = x
    while (parent.get(cur) !== cur) {
      const next = parent.get(cur)!
      parent.set(cur, parent.get(next)!)
      cur = parent.get(cur)!
    }
    return cur
  }
  const union = (a: string, b: string): void => {
    const ra = find(a)
    const rb = find(b)
    if (ra === rb) return
    // Attach to the smaller root so the representative never depends on visit order.
    const [small, large] = compareCodePoints(ra, rb) <= 0 ? [ra, rb] : [rb, ra]
    parent.set(large, small)
  }

  const known = new Set(entities.map((e) => e.name))
  const touched = new Set<string>()
  for (const ent of entities) {
    for (const other of ent.atomicWith ?? []) {
      if (!known.has(other)) {
        throw new DeclarationError(
          `${ent.name}: atomicWith names ${JSON.stringify(other)}, which is not a declared entity`,
        )
      }
      if (other === ent.name) {
        throw new DeclarationError(`${ent.name}: atomicWith names itself, which says nothing`)
      }
      union(ent.name, other)
      touched.add(ent.name)
      touched.add(other)
    }
  }

  const buckets = new Map<string, string[]>()
  for (const name of [...touched].sort(compareCodePoints)) {
    const root = find(name)
    const bucket = buckets.get(root)
    if (bucket) bucket.push(name)
    else buckets.set(root, [name])
  }
  return [...buckets.values()]
    .map((members) => [...members].sort(compareCodePoints))
    .sort((a, b) => compareCodePoints(a[0]!, b[0]!))
}

/** Assemble already-resolved specs into a model. Shared by `buildModel` and the vector loader. */
/**
 * The seven refusals of format-contract §4a, in the order that section writes them.
 *
 * Here rather than at each front door, because there are two - `buildModel` and the neutral-JSON
 * loader the conformance vectors use - and each of them enforced a different subset. The vectors
 * therefore ran a weaker validator than any application does, which is the suite's own failure
 * mode: a vector that passes without reaching the code it describes takes the place of one that
 * would have.
 */
function refuseADeclarationThatIsNotAModel(
  entities: readonly EntitySpec[],
  relations: readonly RelationSpec[],
): void {
  if (entities.length === 0) {
    throw new DeclarationError('no entities declared, so there is no model to build')
  }

  const known = new Set<string>()
  for (const spec of entities) {
    if (spec.fields.length === 0) {
      throw new DeclarationError(
        `${spec.name} has no fields. An entity that stores nothing cannot be placed, so there is ` +
          'nothing for a map to say about it.',
      )
    }
    if (known.has(spec.name)) {
      throw new DeclarationError(
        `two entities are called ${JSON.stringify(spec.name)}. Entity names reach the canonical ` +
          "IR and the colocation graph, so a duplicate makes 'which entity is this' unanswerable " +
          'in the document whose job is to answer it.',
      )
    }
    known.add(spec.name)
  }

  const relationsOf = new Map<string, Set<string>>()
  for (const rel of relations) {
    for (const side of [rel.source, rel.target]) {
      if (!known.has(side)) {
        throw new DeclarationError(
          `relation '${rel.name}' names unknown entity '${side}'`,
        )
      }
    }
    const named = relationsOf.get(rel.source) ?? new Set<string>()
    if (named.has(rel.name)) {
      throw new DeclarationError(
        `${rel.source}.${rel.name} is declared twice. Two relations of one name on one entity are ` +
          'two edges the colocation graph cannot tell apart.',
      )
    }
    named.add(rel.name)
    relationsOf.set(rel.source, named)
  }

  for (const spec of entities) {
    const names = spec.fields.map((f) => f.name)
    const duplicates = [...new Set(names.filter((n, i) => names.indexOf(n) !== i))].sort(
      compareCodePoints,
    )
    if (duplicates.length > 0) {
      throw new DeclarationError(
        `${spec.name} declares the fields ${JSON.stringify(duplicates)} more than once. The ` +
          "layout would have two columns of one name, and the refusal would arrive from the " +
          'client\'s engine at CREATE TABLE.',
      )
    }
    const known_fields = new Set(names)
    if (spec.key.length === 0) {
      throw new DeclarationError(
        `${spec.name} declares no key. A key is what makes a row addressable, migratable and ` +
          'verifiable - a backfill compares rows by it - so an entity without one is a group that ' +
          'cannot be moved, and that is worth knowing when the model is declared rather than in ' +
          'the middle of a migration. No key is invented for you.',
      )
    }
    const missing = spec.key.filter((k) => !known_fields.has(k))
    if (missing.length > 0) {
      throw new DeclarationError(
        `${spec.name}: key names ${JSON.stringify(missing)}, which are not fields of ${spec.name}`,
      )
    }
    const repeated = [
      ...new Set(spec.key.filter((k, i) => spec.key.indexOf(k) !== i)),
    ].sort(compareCodePoints)
    if (repeated.length > 0) {
      throw new DeclarationError(
        `${spec.name}: key names ${JSON.stringify(repeated)} more than once, which is a ` +
          'composite key with one column in two positions.',
      )
    }
    const badPii = spec.pii.filter((f) => !known_fields.has(f))
    if (badPii.length > 0) {
      throw new DeclarationError(
        `${spec.name}: pii names ${JSON.stringify(badPii)}, which are not fields of ` +
          `${spec.name}. A pii entry that is not a field silently protects nothing, and the ` +
          'exclusion of personal data from a derived copy is meant to be readable rather than ' +
          'trusted.',
      )
    }
  }
}

export function assemble(
  entities: readonly EntitySpec[],
  relations: readonly RelationSpec[],
  atomic: readonly (readonly string[])[],
  costCeiling: CostCeiling | null,
): LogicalModel {
  refuseADeclarationThatIsNotAModel(entities, relations)

  const specs = [...entities].sort((a, b) => compareCodePoints(a.name, b.name))
  const rels = [...relations].sort(
    (a, b) =>
      compareCodePoints(a.source, b.source) ||
      compareCodePoints(a.name, b.name) ||
      compareCodePoints(a.target, b.target),
  )

  const ir: Record<string, unknown> = {
    contract: CONTRACT,
    entities: specs.map((s) => ({
      name: s.name,
      fields: [...s.fields]
        .sort((a, b) => compareCodePoints(a.name, b.name))
        .map((f) => ({ name: f.name, type: f.type, nullable: f.nullable })),
      key: s.key.map((field, position) => ({ field, position })),
      pii: [...s.pii].sort(compareCodePoints),
      residency: s.residency,
    })),
    relations: rels.map((r) => ({ name: r.name, from: r.source, to: r.target })),
    atomic: atomic.map((group) => [...group]),
    cost_ceiling: costCeiling === null ? null : { ...costCeiling },
  }

  return {
    entities: specs,
    relations: rels,
    atomic,
    costCeiling,
    ir,
    version: digest16(ir),
  }
}

/** Build a model from declared entities. */
export function buildModel(
  entities: readonly Entity[],
  options: { readonly costCeiling?: CostCeiling } = {},
): LogicalModel {
  if (entities.length === 0) {
    throw new DeclarationError('no entities declared, so there is no model to build')
  }
  // Uniqueness, pii and the key are checked in `assemble` now, which both front doors go through.
  // A guarantee that holds at one of two doors holds for one of two callers, and the conformance
  // vectors come through the other one.
  const names = new Set<string>(entities.map((e) => e.name))

  const specs: EntitySpec[] = []
  const relations: RelationSpec[] = []

  for (const ent of entities) {
    const fieldNames = new Set(Object.keys(ent.fields))
    const fields: FieldSpec[] = Object.entries(ent.fields).map(([name, ft]) => ({
      name,
      type: checkType(ft.type, `${ent.name}.${name}`),
      nullable: ft.nullable,
    }))

    for (const [name, relation] of Object.entries(ent.relations ?? {})) {
      if (!names.has(relation.to)) {
        throw new DeclarationError(
          `${ent.name}.${name} points at ${relation.to}, which is not a declared entity. Include ` +
            'it in the model you are building.',
        )
      }
      // No refusal for a field and a relation sharing a name. This check used to be here and it
      // contradicted the contract: §2a says the digest collision between the two is intended, and
      // `hashing/003-reserved-object-keys` declares exactly that shape - so this library refused a
      // model its own conformance vector pins. It is legal because a relation reaches a layout as
      // `<relation>_<target key field>` and never under its own name, so there is no column for it
      // to collide with.
      relations.push({ name, source: ent.name, target: relation.to })
    }

    specs.push({
      name: ent.name,
      fields,
      key: resolveKey(ent, fieldNames),
      pii: ent.pii ?? [],
      residency: ent.residency ?? null,
    })
  }

  return assemble(specs, relations, normaliseAtomic(entities), options.costCeiling ?? null)
}

/**
 * The model as the neutral form of format-contract §4a: the inverse of `modelFromNeutral`.
 *
 * It exists because the control plane takes a client's model as *that* document while a client
 * declares it in whichever way their language is comfortable with. Without this, a client has to
 * write their model a second time by hand, and the two copies then drift.
 *
 * **The IR is not this document.** They differ exactly where a key is written; see `checkShape` in
 * the neutral loader.
 */
export function neutralDeclaration(model: LogicalModel): Record<string, unknown> {
  const entities = model.entities.map((spec) => {
    const entity: Record<string, unknown> = {
      name: spec.name,
      fields: spec.fields.map((f) => ({
        name: f.name,
        type: f.type,
        ...(f.nullable ? { nullable: true } : {}),
      })),
      key: [...spec.key],
    }
    if (spec.pii.length > 0) entity['pii'] = [...spec.pii]
    if (spec.residency !== null) entity['residency'] = spec.residency
    return entity
  })

  const document: Record<string, unknown> = { entities }
  if (model.relations.length > 0) {
    document['relations'] = model.relations.map((r) => ({
      name: r.name,
      from: r.source,
      to: r.target,
    }))
  }
  if (model.atomic.length > 0) document['atomic'] = model.atomic.map((g) => [...g])
  if (model.costCeiling !== null) document['cost_ceiling'] = { ...model.costCeiling }
  return document
}

export function irBytes(model: LogicalModel): Buffer {
  return canonicalBytes(model.ir)
}

export function entityOf(model: LogicalModel, name: string): EntitySpec {
  const found = model.entities.find((e) => e.name === name)
  if (!found) throw new DeclarationError(`${name} is not in this model`)
  return found
}

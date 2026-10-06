/**
 * Building a model from neutral JSON, so that the conformance vectors can be shared.
 *
 * Every library needs this. A vector cannot contain TypeScript declarations any more than it can
 * contain Python decorators, so the declaration in a vector is plain JSON and each implementation
 * needs a way to turn that into its own model type.
 *
 * It deliberately does not re-implement the encoding: it produces specs and hands them to
 * `assemble`. A loader with its own copy of the IR construction would make the vectors verify a code
 * path no application ever executes, which is the most expensive kind of green test - it looks like
 * coverage and is the absence of it.
 */

import { compareCodePoints } from '../canonical.js'
import { DeclarationError } from '../errors.js'
import type {
  CostCeiling,
  EntitySpec,
  FieldSpec,
  LogicalModel,
  RelationSpec,
} from '../model.js'
import { assemble, checkedCostCeiling } from '../model.js'
import { checkType } from '../types.js'

interface NeutralField {
  readonly name: string
  readonly type: string
  readonly nullable?: boolean
}

interface NeutralEntity {
  readonly name: string
  readonly fields?: readonly NeutralField[]
  readonly key?: readonly string[]
  readonly pii?: readonly string[]
  readonly residency?: string | null
}

interface NeutralRelation {
  readonly name: string
  readonly from: string
  readonly to: string
}

export interface NeutralModel {
  readonly entities?: readonly NeutralEntity[]
  readonly relations?: readonly NeutralRelation[]
  readonly atomic?: readonly (readonly string[])[]
  readonly cost_ceiling?: CostCeiling | null
}

/** A value as a refusal shows it: absent is `undefined`, anything else its JSON. */
function describe(value: unknown): string {
  return value === undefined ? 'undefined' : JSON.stringify(value)
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * The shapes a person can plausibly hand this function, named rather than crashed on.
 *
 * The first one is the whole reason this exists. The IR (§4) and the neutral form (§4a) are close
 * enough to be confused - our own control-plane tests declared a model's IR for weeks and got away
 * with it, because nothing read the stored document back - and they differ exactly where a key is
 * written. Vector `errors/036`.
 *
 * The rest are the reference's, which this port had not carried over - a field list that is not a
 * list, a field without a name or a type, `pii` that is not a list of names - and two the C++
 * library found, each a value two libraries read two ways: `nullable` that is not a boolean was
 * `=== true` here and truthy there, and a `residency` that is not a name reached the IR as whatever
 * it was.
 */
function checkShape(raw: Record<string, unknown>, name: string): void {
  const fields = raw['fields']
  if (!Array.isArray(fields)) {
    throw new DeclarationError(`${name}: 'fields' is a list, and this one is ${describe(fields)}`)
  }
  for (const field of fields as unknown[]) {
    if (!isObject(field) || typeof field['name'] !== 'string') {
      throw new DeclarationError(`${name}: a field is an object with a name, not ${describe(field)}`)
    }
    if (typeof field['type'] !== 'string') {
      throw new DeclarationError(`${name}.${field['name']}: a field needs a type`)
    }
    if ('nullable' in field && typeof field['nullable'] !== 'boolean') {
      throw new DeclarationError(
        `${name}.${field['name']}: 'nullable' is true or false, not ${describe(field['nullable'])}`,
      )
    }
  }

  const key = raw['key']
  if (key !== undefined && key !== null && !Array.isArray(key)) {
    throw new DeclarationError(`${name}: 'key' is a list of field names, not ${describe(key)}`)
  }
  for (const part of (Array.isArray(key) ? key : []) as unknown[]) {
    if (isObject(part) && 'field' in part && 'position' in part) {
      throw new DeclarationError(
        `${name}: 'key' holds ${JSON.stringify(part)}, which is the IR's key form rather ` +
          'than the neutral one. The IR records a position because array order is not ' +
          'load-bearing anywhere else in it; a declaration states a key as a list of field names, ' +
          'in order. If you meant to hand over a model you already built, neutralDeclaration() ' +
          'produces this document from it.',
      )
    }
    if (typeof part !== 'string') {
      throw new DeclarationError(`${name}: 'key' names fields as strings, not ${describe(part)}`)
    }
  }

  const pii = raw['pii']
  if (
    pii !== undefined &&
    pii !== null &&
    (!Array.isArray(pii) || (pii as unknown[]).some((v) => typeof v !== 'string'))
  ) {
    throw new DeclarationError(`${name}: 'pii' is a list of field names, not ${describe(pii)}`)
  }

  const residency = raw['residency']
  if (residency !== undefined && residency !== null && typeof residency !== 'string') {
    throw new DeclarationError(
      `${name}: 'residency' is a jurisdiction's name or null, not ${describe(residency)}`,
    )
  }
}

/** `relations`, each an object of three strings. A missing `to` reached `names.has(undefined)`. */
function readRelations(raw: unknown, names: ReadonlySet<string>): RelationSpec[] {
  if (raw === undefined || raw === null) return []
  if (!Array.isArray(raw)) {
    throw new DeclarationError(`'relations' is a list of {name, from, to}, not ${describe(raw)}`)
  }
  const relations: RelationSpec[] = []
  for (const relation of raw as unknown[]) {
    if (
      !isObject(relation) ||
      !['name', 'from', 'to'].every((key) => typeof relation[key] === 'string')
    ) {
      throw new DeclarationError(
        `a relation is {"name", "from", "to"}, each a string, not ${describe(relation)}`,
      )
    }
    const { name, from, to } = relation as { name: string; from: string; to: string }
    for (const side of [from, to]) {
      if (!names.has(side)) {
        throw new DeclarationError(`relation '${name}' names unknown entity '${side}'`)
      }
    }
    relations.push({ name, source: from, target: to })
  }
  return relations
}

/**
 * Atomic groups as declared: names checked, members distinct, two at least, no overlap.
 *
 * Section 4 says the IR's groups are merged and transitive. This loader copied them as written and
 * sorted their members by UTF-16 unit, so two overlapping groups reached the IR unmerged - another
 * `model_version` than the same atomicity written as one group - and an astral member sorted where
 * no other library puts it. Refused rather than merged, and sorted by code point.
 */
function readAtomic(raw: unknown, names: ReadonlySet<string>): string[][] {
  if (raw === undefined || raw === null) return []
  if (!Array.isArray(raw)) {
    throw new DeclarationError(`'atomic' is a list of groups of entity names, not ${describe(raw)}`)
  }
  const placed = new Set<string>()
  const groups: string[][] = []
  for (const group of raw as unknown[]) {
    if (!Array.isArray(group) || (group as unknown[]).some((member) => typeof member !== 'string')) {
      throw new DeclarationError(`an atomic group is a list of entity names, not ${describe(group)}`)
    }
    const members = [...(group as string[])].sort(compareCodePoints)
    const unknown = members.filter((m) => !names.has(m))
    if (unknown.length > 0) {
      throw new DeclarationError(`atomic group names unknown entities ${JSON.stringify(unknown)}`)
    }
    if (new Set(members).size !== members.length || members.length < 2) {
      throw new DeclarationError(
        'an atomic group names two or more distinct entities; one that names fewer, or one ' +
          'twice, says nothing a placement could act on',
      )
    }
    for (const member of members) {
      if (placed.has(member)) {
        throw new DeclarationError(
          `atomic groups overlap on '${member}'. Atomicity is transitive, so overlapping groups ` +
            "are one group: declare it once, with every member, as a library's own neutral " +
            'declaration does',
        )
      }
      placed.add(member)
    }
    groups.push(members)
  }
  // Disjoint and each sorted, so their first members order the groups.
  return groups.sort((a, b) => compareCodePoints(a[0]!, b[0]!))
}

/**
 * Build a model from a vector's `model.json`.
 *
 * The neutral form states keys as a plain list, because that is what a human writes. Turning it into
 * the positioned form the IR uses is this library's job - which is the point: if the vector carried
 * the positioned form, passing it would only prove we can copy JSON.
 */
export function modelFromNeutral(data: NeutralModel): LogicalModel {
  const document: unknown = data
  if (!isObject(document)) {
    throw new DeclarationError("a neutral model declaration is an object with an 'entities' list")
  }
  const rawEntities = document['entities']
  if (!Array.isArray(rawEntities)) {
    throw new DeclarationError("a neutral model declaration needs an 'entities' list")
  }

  const entities: EntitySpec[] = []
  for (const raw of rawEntities as unknown[]) {
    if (!isObject(raw)) {
      throw new DeclarationError(`an entity is an object, and this one is ${describe(raw)}`)
    }
    const name = raw['name']
    if (typeof name !== 'string' || name === '') {
      throw new DeclarationError(`an entity needs a name, and this one has ${describe(name)}`)
    }
    checkShape(raw, name)
    const entity = raw as unknown as NeutralEntity
    const fields: FieldSpec[] = (entity.fields ?? []).map((f) => ({
      name: f.name,
      type: checkType(f.type, `${name}.${f.name}`),
      nullable: f.nullable === true,
    }))

    // No default. This read `raw.key ?? ['id']` and the Python port spelled the same thing with
    // `or`, which differs on exactly one input: `"key": []` stayed keyless here and invented a key
    // there, so one declaration had two model versions and no vector could see it. The rule is
    // format-contract §4a now and the refusal is in `assemble`, where both front doors meet.
    const key = entity.key ?? []

    entities.push({
      name,
      fields,
      key,
      pii: entity.pii ?? [],
      residency: entity.residency ?? null,
    })
  }

  const names = new Set(entities.map((e) => e.name))
  const relations = readRelations(document['relations'], names)
  const atomic = readAtomic(document['atomic'], names)
  return assemble(entities, relations, atomic, checkedCostCeiling(document['cost_ceiling']))
}

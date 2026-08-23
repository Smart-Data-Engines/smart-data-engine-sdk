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

import { DeclarationError } from '../errors.js'
import type {
  CostCeiling,
  EntitySpec,
  FieldSpec,
  LogicalModel,
  RelationSpec,
} from '../model.js'
import { assemble } from '../model.js'
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

/**
 * Build a model from a vector's `model.json`.
 *
 * The neutral form states keys as a plain list, because that is what a human writes. Turning it into
 * the positioned form the IR uses is this library's job - which is the point: if the vector carried
 * the positioned form, passing it would only prove we can copy JSON.
 */
export function modelFromNeutral(data: NeutralModel): LogicalModel {
  const entities: EntitySpec[] = []

  for (const raw of data.entities ?? []) {
    const fields: FieldSpec[] = (raw.fields ?? []).map((f) => ({
      name: f.name,
      type: checkType(f.type, `${raw.name}.${f.name}`),
      nullable: f.nullable === true,
    }))
    if (fields.length === 0) throw new DeclarationError(`${raw.name} has no fields`)

    const key = raw.key ?? ['id']
    const known = new Set(fields.map((f) => f.name))
    const missing = key.filter((k) => !known.has(k))
    if (missing.length > 0) {
      throw new DeclarationError(
        `${raw.name}: key names ${JSON.stringify(missing)}, which are not fields`,
      )
    }

    entities.push({
      name: raw.name,
      fields,
      key,
      pii: raw.pii ?? [],
      residency: raw.residency ?? null,
    })
  }

  const names = new Set(entities.map((e) => e.name))
  const relations: RelationSpec[] = []
  for (const raw of data.relations ?? []) {
    for (const side of [raw.from, raw.to]) {
      if (!names.has(side)) {
        throw new DeclarationError(
          `relation '${raw.name}' names unknown entity '${side}'`,
        )
      }
    }
    relations.push({ name: raw.name, source: raw.from, target: raw.to })
  }

  const atomic = (data.atomic ?? []).map((group) => [...group].sort())
  for (const group of atomic) {
    const unknown = group.filter((m) => !names.has(m))
    if (unknown.length > 0) {
      throw new DeclarationError(`atomic group names unknown entities ${JSON.stringify(unknown)}`)
    }
  }

  return assemble(entities, relations, atomic, data.cost_ceiling ?? null)
}

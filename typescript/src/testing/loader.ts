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
/**
 * The shapes a person can plausibly hand this function, named rather than crashed on.
 *
 * The first one is the whole reason this exists. The IR (§4) and the neutral form (§4a) are close
 * enough to be confused - our own control-plane tests declared a model's IR for weeks and got away
 * with it, because nothing read the stored document back - and they differ exactly where a key is
 * written. Vector `errors/036`.
 */
function checkShape(raw: NeutralEntity): void {
  for (const part of raw.key ?? []) {
    if (typeof part === 'object' && part !== null && 'field' in part && 'position' in part) {
      throw new DeclarationError(
        `${raw.name}: 'key' holds ${JSON.stringify(part)}, which is the IR's key form rather ` +
          'than the neutral one. The IR records a position because array order is not ' +
          'load-bearing anywhere else in it; a declaration states a key as a list of field names, ' +
          'in order. If you meant to hand over a model you already built, neutralDeclaration() ' +
          'produces this document from it.',
      )
    }
    if (typeof part !== 'string') {
      throw new DeclarationError(
        `${raw.name}: 'key' names fields as strings, not ${JSON.stringify(part)}`,
      )
    }
  }
}

export function modelFromNeutral(data: NeutralModel): LogicalModel {
  const entities: EntitySpec[] = []

  for (const raw of data.entities ?? []) {
    checkShape(raw)
    const fields: FieldSpec[] = (raw.fields ?? []).map((f) => ({
      name: f.name,
      type: checkType(f.type, `${raw.name}.${f.name}`),
      nullable: f.nullable === true,
    }))

    // No default. This read `raw.key ?? ['id']` and the Python port spelled the same thing with
    // `or`, which differs on exactly one input: `"key": []` stayed keyless here and invented a key
    // there, so one declaration had two model versions and no vector could see it. The rule is
    // format-contract §4a now and the refusal is in `assemble`, where both front doors meet.
    const key = raw.key ?? []

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

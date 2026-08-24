/**
 * Operation shapes: the finite set of things an application can ask of a model.
 *
 * A shape is assembled from the structure of an operation and never sees its arguments, so there is
 * nowhere for a value to come from. That is what makes telemetry safe by construction rather than by
 * redaction, and it is a large part of why the API is an entity API rather than SQL.
 */

import { compareCodePoints, digest16 } from './canonical.js'
import type { Group } from './groups.js'
import { colocationGroups, groupOf } from './groups.js'
import type { LogicalModel } from './model.js'

export const SHAPE_KINDS = [
  'point_read',
  'range_read',
  'aggregate',
  'full_scan',
  'relation_walk',
  'write',
  'bulk_write',
] as const

export type ShapeKind = (typeof SHAPE_KINDS)[number]

/**
 * Types over which a range predicate is meaningful. Ranges over strings and uuids are legal in every
 * engine and almost never what anybody means, so they are not enumerated.
 */
const ORDERED_PREFIXES = ['int32', 'int64', 'float32', 'float64', 'decimal', 'date', 'timestamp']

export interface OperationShape {
  readonly group: string
  readonly kind: ShapeKind
  readonly entity: string
  readonly fields: readonly string[]
  readonly target: string | null
}

export function shapeIr(shape: OperationShape): Record<string, unknown> {
  return {
    group: shape.group,
    kind: shape.kind,
    entity: shape.entity,
    fields: [...shape.fields],
    target: shape.target,
  }
}

/**
 * Identifiers are memoised per shape object.
 *
 * Not premature. Routing reads a shape's identifier on every operation, and computing it means
 * building an object, canonically encoding it and running SHA-256 over the result. The Python
 * implementation had exactly this on its hot path and the overhead test measured 41 microseconds
 * median to resolve one route - sixteen percent of a PostgreSQL round trip, against a budget of one
 * percent. Shapes are enumerated once per model and live for the process, so a WeakMap keyed by the
 * object is both correct and free after the first call.
 *
 * A WeakMap rather than a field on the interface so that `shapeId` keeps working on a shape literal
 * built by hand, which the conformance runner and anyone reading a vector will do.
 */
const idCache = new WeakMap<OperationShape, string>()

export function shapeId(shape: OperationShape): string {
  const cached = idCache.get(shape)
  if (cached !== undefined) return cached
  const computed = digest16(shapeIr(shape))
  idCache.set(shape, computed)
  return computed
}

function isOrdered(type: string): boolean {
  return ORDERED_PREFIXES.some((prefix) => type.startsWith(prefix))
}

export function enumerateShapes(model: LogicalModel): readonly OperationShape[] {
  const groups: readonly Group[] = colocationGroups(model)
  const shapes: OperationShape[] = []

  for (const spec of model.entities) {
    const group = groupOf(groups, spec.name).name

    shapes.push({
      group,
      entity: spec.name,
      target: null,
      kind: 'point_read',
      fields: [...spec.key].sort(compareCodePoints),
    })
    for (const kind of ['write', 'bulk_write', 'full_scan', 'aggregate'] as const) {
      shapes.push({ group, entity: spec.name, target: null, kind, fields: [] })
    }

    for (const field of spec.fields) {
      if (isOrdered(field.type)) {
        shapes.push({
          group,
          entity: spec.name,
          target: null,
          kind: 'range_read',
          fields: [field.name],
        })
      }
    }
  }

  for (const relation of model.relations) {
    shapes.push({
      group: groupOf(groups, relation.source).name,
      kind: 'relation_walk',
      entity: relation.source,
      fields: [relation.name],
      target: relation.target,
    })
  }

  // By (group, entity, kind, fields, target) rather than by identifier, so a human reading a
  // placement map sees related shapes together instead of scattered by hash.
  return shapes.sort(
    (a, b) =>
      compareCodePoints(a.group, b.group) ||
      compareCodePoints(a.entity, b.entity) ||
      compareCodePoints(a.kind, b.kind) ||
      compareCodePoints(a.fields.join(' '), b.fields.join(' ')) ||
      compareCodePoints(a.target ?? '', b.target ?? ''),
  )
}

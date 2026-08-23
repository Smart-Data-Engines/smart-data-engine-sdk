/**
 * Routing: a lookup and three conditions, and deliberately nothing more.
 *
 * The library does not decide where an operation goes. The planner decided, ahead of time, for every
 * shape the model admits, and put the answers in the placement map. This reads them.
 *
 * That division is exactly why four libraries are affordable. Decisions need telemetry, history, a
 * cost model and an explanation, and they have to be reproducible - all of which live on the control
 * plane, once. If the library decided anything, that judgement would have to be reimplemented in
 * every language and kept identical forever.
 *
 * The three conditions that are not lookups are correctness, not judgement:
 *
 * 1. Writes go to the source materialisation.
 * 2. An operation inside a transaction that has already written goes to the source, because a derived
 *    copy is behind by design and cannot show the write the caller just made.
 * 3. An operation asking for no staleness goes to the source, for the same reason.
 */

import type { Materialization, PlacementMap } from './placement.js'
import { materializationById, placementOf } from './placement.js'
import type { OperationShape } from './shapes.js'
import { shapeId } from './shapes.js'

const WRITE_KINDS = new Set(['write', 'bulk_write'])

export interface ResolveOptions {
  readonly inWriteTransaction?: boolean
  readonly fresh?: boolean
}

export function resolve(
  map: PlacementMap,
  shape: OperationShape,
  options: ResolveOptions = {},
): Materialization {
  const placement = placementOf(map, shape.group)

  if (WRITE_KINDS.has(shape.kind)) return placement.source
  if (options.inWriteTransaction === true || options.fresh === true) return placement.source

  const target = map.routing[shapeId(shape)]
  if (target === undefined) {
    // Not an error. A map with no routing table is the normal shape of a hand-written one, and the
    // source is always a correct answer - merely sometimes a slower one.
    return placement.source
  }
  return materializationById(placement, target)
}

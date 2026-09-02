/**
 * Smart Data Engine - client library for TypeScript.
 *
 * You declare entities and relations. We decide which database engine each colocation group lives in,
 * what its physical layout is there, and when it should move - and your code never names a table or
 * an engine, which is exactly what lets us change both without touching it.
 *
 * This implementation is Tier 0 of the capability tiers: model, canonical IR and version, colocation
 * groups, operation shapes and ids, placement maps with signature verification and the refusals that
 * go with them, and routing. Telemetry (Tier 1) and schema plus migration participation (Tier 2) are
 * not here yet, and the README says so rather than implying otherwise.
 *
 * It also implements hashed identifiers (section 2a), which is a mode rather than a tier: a complete
 * Tier 0 library may omit it, but one that offers it has to derive the same digests as every other, or
 * two services on one model compute two model versions and each refuses the other's map.
 *
 * Unlike Python, the model is declared explicitly rather than read from annotations - TypeScript's
 * types are erased before the code runs. That is not a workaround; it is why this was the right second
 * implementation. Anything the format contract left implicit had nowhere to hide.
 */

export { CanonicalError, canonicalBytes, canonicalString, compareCodePoints, digest16 } from './canonical.js'
export { DeclarationError, EngineError, MapError, ModelPlanningError, SdeError } from './errors.js'
export type { NameMap } from './hashing.js'
export { DIGEST_CHARS, hashIdentifiers } from './hashing.js'
export type { Group } from './groups.js'
export { colocationGroups, groupOf } from './groups.js'
export type {
  CostCeiling,
  Entity,
  EntityDeclaration,
  EntitySpec,
  FieldSpec,
  LogicalModel,
  RelationSpec,
} from './model.js'
export { assemble, buildModel, CONTRACT, entity, entityOf, irBytes, ref } from './model.js'
export type {
  GroupPlacement,
  LoadOptions,
  Materialization,
  PhysicalLayout,
  PlacementMap,
} from './placement.js'
export {
  ALSO_WRITE_SINCE,
  BACKFILL_TABLE,
  MAP_CONTRACT,
  MAP_CONTRACT_FLOOR,
  RESERVED_TABLES,
  WATERMARK_TABLE,
  loadMap,
  materializationById,
  placementOf,
} from './placement.js'
export type { ResolveOptions } from './routing.js'
export { resolve } from './routing.js'
export type { OperationShape, ShapeKind } from './shapes.js'
export { enumerateShapes, SHAPE_KINDS, shapeId, shapeIr } from './shapes.js'
export type { FieldType, NeutralType } from './types.js'
export { checkType, NEUTRAL_TYPES, T } from './types.js'

/** The format contract version this library implements. */
export const CONTRACT_VERSION = 1

/** The capability tier this library reaches. See docs/format-contract.md, section 9. */
export const TIER = 0

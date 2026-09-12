/**
 * Smart Data Engine - client library for TypeScript.
 *
 * You declare entities and relations. We decide which database engine each colocation group lives in,
 * what its physical layout is there, and when it should move - and your code never names a table or
 * an engine, which is exactly what lets us change both without touching it.
 *
 * This implementation reaches Tier 2 of the capability tiers, for PostgreSQL and ClickHouse: the
 * model, canonical IR and version, colocation groups, operation shapes and ids, placement maps with
 * signature verification and the refusals that go with them, routing, telemetry, schema rendering
 * and application, dual write, and backfill with verification.
 *
 * The shared vectors for Tier 1 and Tier 2 were written **before** this library claimed those
 * tiers, which is what §10 of the format contract requires and is the interesting fact about the
 * claim. Closing that gap found five defects in the reference implementation.
 *
 * **Everything below Tier 2 is synchronous and touches no socket; Tier 2 is asynchronous.** That is
 * the runtime's shape rather than a preference: a synchronous wrapper around Node's drivers means
 * blocking the event loop. The engine adapters are behind their own subpath exports, so importing
 * this module resolves no driver - pinned by a test over the import closure, because that absence
 * is what makes the no-account mode free.
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
export {
  DeclarationError,
  EngineError,
  MapError,
  MapRolledBack,
  MigrationRefused,
  ModelPlanningError,
  SdeError,
} from './errors.js'
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
export {
  assemble,
  buildModel,
  CONTRACT,
  entity,
  entityOf,
  irBytes,
  neutralDeclaration,
  ref,
} from './model.js'
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
export { groupColumns } from './layout.js'
export type {
  BackfillOptions,
  BackfillProgress,
  Difference,
  EntityProgress,
  Migratable,
  VerifyOptions,
  VerifyReport,
} from './migration.js'
export {
  backfill,
  backfillForAHuman,
  backfillRecord,
  CHUNK_ROWS,
  DIALECT_PRECISION,
  entityProgressRecord,
  keyColumns,
  MIGRATABLE_IS_TOTAL,
  PRECISION_INDEPENDENT,
  sameWidth,
  verify,
  verifyForAHuman,
  verifyRecord,
} from './migration.js'
export type { Engine, Row, SessionOptions } from './session.js'
export { Session, tableFor } from './session.js'
export type { Protection, WatermarkCheck, WatermarkStore } from './watermark.js'
export {
  enforceForwardOnly,
  WATERMARK_STORE_IS_TOTAL,
  watermarkRecord,
} from './watermark.js'
export { MIGRATABLE_MEMBERS, satisfies, WATERMARK_MEMBERS } from './capabilities.js'
export type { ResolveOptions } from './routing.js'
export { resolve } from './routing.js'
export type { CompatibilityViews, Dialect, SchemaOptions, ViewOptions } from './schema.js'
export {
  compatibilityViews,
  DIALECTS,
  FIXED_SCHEMA,
  QUOTE,
  schemaIsFixed,
  schemaStatements,
} from './schema.js'
export type { OperationShape, ShapeKind } from './shapes.js'
export { enumerateShapes, SHAPE_KINDS, shapeId, shapeIr, WRITE_KINDS } from './shapes.js'
export { guard, internalFailures, resetInternalFailures } from './internal.js'
export type {
  CopyFreshness,
  FanOutOptions,
  FeatureOptions,
  GroupFeatures,
  RecordOptions,
  Window,
} from './telemetry.js'
export {
  BUCKET_BASE_NS,
  BUCKET_COUNT,
  copyFreshnessRecord,
  FanOutStats,
  featuresRecord,
  FIELD_LIST_IS_TOTAL,
  hasTimeDimension,
  Histogram,
  MEASURED_FIELDS,
  Recorder,
  ShapeStats,
  windowCopies,
  windowFeatures,
  windowRecord,
} from './telemetry.js'
export type { FieldType, NeutralType } from './types.js'
export { checkType, NEUTRAL_TYPES, T } from './types.js'
export { Timestamp } from './timestamp.js'

/** The format contract version this library implements. */
export const CONTRACT_VERSION = 1

/** The capability tier this library reaches. See docs/format-contract.md, section 9. */
export const TIER = 2

export { VerificationRequest, verificationRequest } from './verification.js'

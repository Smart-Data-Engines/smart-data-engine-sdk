"""Smart Data Engine - client library.

You declare entities and relations. We decide which database engine each colocation group lives in,
what its physical layout is there, and when it should move - and your code never names a table or an
engine, which is exactly what lets us change both without touching it.

    from datetime import datetime from decimal import Decimal from typing import Annotated from uuid
    import UUID import sde

    @sde.entity
    class User:
        id: UUID email: str

        class Meta:
            pii = ["email"]

    @sde.entity
    class Order:
        id: UUID user: sde.Ref[User] total: Annotated[Decimal, sde.precision(12, 2)] created_at:
        datetime

        class Meta:
            atomic_with = ["Payment"] residency = "EU"

Everything about storage is our decision. The four things you declare - atomicity, residency,
personal data and a cost ceiling - are the ones that cannot be read from traffic no matter how long
we watch it.

This library is Apache-2.0 and it works without an account: hand it a placement map you wrote
yourself and it will route, create schema and run, with no key and no network. That is a supported
mode, not a loophole.
"""

from __future__ import annotations

from .bulk import MAX_BATCH_ROWS, MAX_BATCH_VALUES, BulkWritable
from .canonical import CanonicalError, canonical_bytes, canonical_str, digest16
from .capabilities import members_of, satisfies
from .cutover import CUTOVER_PROTOCOL, CUTOVER_RELAYOUT_PROTOCOL, CutoverPlan, load_cutover_plan
from .entity import Ref, clear_registry, entity, registry
from .errors import (
    BulkWriteRefused,
    DeclarationError,
    EngineError,
    MapError,
    MapRolledBack,
    MigrationRefused,
    ModelPlanningError,
    ResourceBusy,
    ResourceClosed,
    SdeError,
)
from .explain import Cost, Explains, PlanFinding, QueryPlan, QueryPlanRefused, explain
from .frozen_verification import FrozenVerifyReport, verify_frozen
from .groups import Group, colocation_groups, group_of
from .hashing import NameMap, hash_identifiers, load_or_create_salt
from .index_build import (
    INDEX_PROTOCOL,
    IndexPlan,
    IndexReceipt,
    index_build_name,
    load_index_plan,
)
from .infer import InferredModel, Note, infer_model, infer_models
from .inspection import InspectionContext
from .internal import internal_failures, reset_internal_failures
from .layout import (
    DIALECTS,
    FIXED_SCHEMA,
    ORDERBOOK_KEY,
    ORDERBOOK_SHAPE,
    ORDERBOOK_TABLE,
    DerivedLayout,
    can_store,
    default_layout,
    denormalized_layout,
    fixed_schema_mismatch,
    group_columns,
    snake_case,
    stored_types,
)
from .local_cutover import CutoverReceipt, CutoverRecoveryRequired, LocalCutover, load_local_map
from .migration import (
    BACKFILL_TABLE,
    CHUNK_ROWS,
    DIALECT_PRECISION,
    PRECISION_INDEPENDENT,
    BackfillProgress,
    Difference,
    EntityProgress,
    Migratable,
    VerifyReport,
    backfill,
    precision_refusal,
    verify,
)
from .model import CONTRACT, LogicalModel, build_model, neutral_declaration
from .physical import PHYSICAL_DESIGN_SINCE, PhysicalFinding
from .placement import (
    ALSO_WRITE_SINCE,
    MAP_CONTRACT,
    MAP_CONTRACT_FLOOR,
    RESERVED_TABLES,
    GroupPlacement,
    Materialization,
    PhysicalLayout,
    PlacementMap,
    load_map,
)
from .provisioning import prepare_schema
from .query import (
    MAX_PAGE_ROWS,
    NumericSummary,
    Queryable,
    QueryRefused,
    Range,
    ScanPage,
    Summarizable,
)
from .routing import Router, resolve
from .schema import CompatibilityViews, compatibility_views, schema_is_fixed, schema_statements
from .session import Engine, ManagedEngine, Session
from .shapes import SHAPE_KINDS, WRITE_KINDS, OperationShape, enumerate_shapes
from .staging import (
    STAGING_PROTOCOL,
    STAGING_RELAYOUT_PROTOCOL,
    StagingPlan,
    StagingReceipt,
    load_staging_plan,
    staging_table_name,
)
from .telemetry import (
    MEASURED_FIELDS,
    CopyFreshness,
    FanOutStats,
    GroupFeatures,
    Histogram,
    Recorder,
    ShapeStats,
    Window,
    has_time_dimension,
)
from .types import Float32, Int32, Json, Timestamp, precision
from .watermark import (
    WATERMARK_TABLE,
    Protection,
    WatermarkCheck,
    WatermarkStore,
    enforce_forward_only,
)
from .write_fence import EPOCH_COLUMN as WRITE_EPOCH_COLUMN
from .write_fence import FenceState, WriteFence

__version__ = "0.1.0.dev0"

__all__ = [
    "ALSO_WRITE_SINCE",
    "BACKFILL_TABLE",
    "CHUNK_ROWS",
    "CONTRACT",
    "CUTOVER_PROTOCOL",
    "CUTOVER_RELAYOUT_PROTOCOL",
    "DIALECTS",
    "DIALECT_PRECISION",
    "FIXED_SCHEMA",
    "INDEX_PROTOCOL",
    "MAP_CONTRACT",
    "MAP_CONTRACT_FLOOR",
    "MAX_BATCH_ROWS",
    "MAX_BATCH_VALUES",
    "MAX_PAGE_ROWS",
    "MEASURED_FIELDS",
    "ORDERBOOK_KEY",
    "ORDERBOOK_SHAPE",
    "ORDERBOOK_TABLE",
    "PHYSICAL_DESIGN_SINCE",
    "PRECISION_INDEPENDENT",
    "RESERVED_TABLES",
    "SHAPE_KINDS",
    "STAGING_PROTOCOL",
    "STAGING_RELAYOUT_PROTOCOL",
    "WATERMARK_TABLE",
    "WRITE_EPOCH_COLUMN",
    "WRITE_KINDS",
    "BackfillProgress",
    "BulkWritable",
    "BulkWriteRefused",
    "CanonicalError",
    "CompatibilityViews",
    "CopyFreshness",
    "Cost",
    "CutoverPlan",
    "CutoverReceipt",
    "CutoverRecoveryRequired",
    "DeclarationError",
    "DerivedLayout",
    "Difference",
    "Engine",
    "EngineError",
    "EntityProgress",
    "Explains",
    "FanOutStats",
    "FenceState",
    "Float32",
    "FrozenVerifyReport",
    "Group",
    "GroupFeatures",
    "GroupPlacement",
    "Histogram",
    "IndexPlan",
    "IndexReceipt",
    "InferredModel",
    "InspectionContext",
    "Int32",
    "Json",
    "LocalCutover",
    "LogicalModel",
    "ManagedEngine",
    "MapError",
    "MapRolledBack",
    "Materialization",
    "Migratable",
    "MigrationRefused",
    "ModelPlanningError",
    "NameMap",
    "Note",
    "NumericSummary",
    "OperationShape",
    "PhysicalFinding",
    "PhysicalLayout",
    "PlacementMap",
    "PlanFinding",
    "Protection",
    "QueryPlan",
    "QueryPlanRefused",
    "QueryRefused",
    "Queryable",
    "Range",
    "Recorder",
    "Ref",
    "ResourceBusy",
    "ResourceClosed",
    "Router",
    "ScanPage",
    "SdeError",
    "Session",
    "ShapeStats",
    "StagingPlan",
    "StagingReceipt",
    "Summarizable",
    "Timestamp",
    "VerificationRequest",
    "VerifyReport",
    "WatermarkCheck",
    "WatermarkStore",
    "Window",
    "WriteFence",
    "__version__",
    "backfill",
    "build_model",
    "can_store",
    "canonical_bytes",
    "canonical_str",
    "clear_registry",
    "colocation_groups",
    "compatibility_views",
    "default_layout",
    "denormalized_layout",
    "digest16",
    "enforce_forward_only",
    "entity",
    "enumerate_shapes",
    "explain",
    "fixed_schema_mismatch",
    "group_columns",
    "group_of",
    "has_time_dimension",
    "hash_identifiers",
    "index_build_name",
    "infer_model",
    "infer_models",
    "internal_failures",
    "load_cutover_plan",
    "load_index_plan",
    "load_local_map",
    "load_map",
    "load_or_create_salt",
    "load_staging_plan",
    "members_of",
    "neutral_declaration",
    "precision",
    "precision_refusal",
    "prepare_schema",
    "registry",
    "reset_internal_failures",
    "resolve",
    "satisfies",
    "schema_is_fixed",
    "schema_statements",
    "snake_case",
    "staging_table_name",
    "stored_types",
    "verification_request",
    "verify",
    "verify_frozen",
]

from .verification import VerificationRequest, verification_request

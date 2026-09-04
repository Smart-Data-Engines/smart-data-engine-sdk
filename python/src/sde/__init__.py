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

from .canonical import CanonicalError, canonical_bytes, canonical_str, digest16
from .capabilities import members_of, satisfies
from .entity import Ref, clear_registry, entity, registry
from .errors import (
    DeclarationError,
    EngineError,
    MapError,
    MapRolledBack,
    MigrationRefused,
    ModelPlanningError,
    SdeError,
)
from .groups import Group, colocation_groups, group_of
from .hashing import NameMap, hash_identifiers, load_or_create_salt
from .infer import InferredModel, Note, infer_model, infer_models
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
    verify,
)
from .model import CONTRACT, LogicalModel, build_model
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
from .routing import Router, resolve
from .schema import CompatibilityViews, compatibility_views, schema_is_fixed, schema_statements
from .session import Engine, Session
from .shapes import SHAPE_KINDS, WRITE_KINDS, OperationShape, enumerate_shapes
from .telemetry import (
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

__version__ = "0.1.0.dev0"

__all__ = [
    "ALSO_WRITE_SINCE",
    "BACKFILL_TABLE",
    "CHUNK_ROWS",
    "CONTRACT",
    "DIALECTS",
    "DIALECT_PRECISION",
    "FIXED_SCHEMA",
    "MAP_CONTRACT",
    "MAP_CONTRACT_FLOOR",
    "ORDERBOOK_KEY",
    "ORDERBOOK_SHAPE",
    "ORDERBOOK_TABLE",
    "PRECISION_INDEPENDENT",
    "RESERVED_TABLES",
    "SHAPE_KINDS",
    "WATERMARK_TABLE",
    "WRITE_KINDS",
    "BackfillProgress",
    "CanonicalError",
    "CompatibilityViews",
    "DeclarationError",
    "DerivedLayout",
    "Difference",
    "Engine",
    "EngineError",
    "EntityProgress",
    "Float32",
    "Group",
    "GroupFeatures",
    "GroupPlacement",
    "Histogram",
    "InferredModel",
    "Int32",
    "Json",
    "LogicalModel",
    "MapError",
    "MapRolledBack",
    "Materialization",
    "Migratable",
    "MigrationRefused",
    "ModelPlanningError",
    "NameMap",
    "Note",
    "OperationShape",
    "PhysicalLayout",
    "PlacementMap",
    "Protection",
    "Recorder",
    "Ref",
    "Router",
    "SdeError",
    "Session",
    "ShapeStats",
    "Timestamp",
    "VerifyReport",
    "WatermarkCheck",
    "WatermarkStore",
    "Window",
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
    "fixed_schema_mismatch",
    "group_columns",
    "group_of",
    "has_time_dimension",
    "hash_identifiers",
    "infer_model",
    "infer_models",
    "internal_failures",
    "load_map",
    "load_or_create_salt",
    "members_of",
    "precision",
    "registry",
    "reset_internal_failures",
    "resolve",
    "satisfies",
    "schema_is_fixed",
    "schema_statements",
    "snake_case",
    "stored_types",
    "verify",
]

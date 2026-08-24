"""Operation shapes: the finite set of things an application can ask of a model.

This is where the entity API pays for itself twice over.

First, security. A shape is built from an API call, so there is nowhere for a literal to come from.
Contrast the SQL route, where you receive a string containing values and have to strip them out with
a parser you hope covers every dialect corner - and where one missed case means a customer's data in
our telemetry. Here the value never enters the shape, because the shape is assembled from the
operation's structure and never sees the arguments.

Second, planning. Because the API is finite, the set of shapes is *enumerable from the model*. The
planner can compute a routing decision for every shape ahead of time and put the answers in the
placement map, which is what lets the library look routing up instead of deciding it. A library that
decides is a library whose decisions have to be tested in four languages.

The enumeration below is deliberately conservative. It covers what the thin slice can execute and
nothing more: adding a shape kind is cheap, while shipping a shape the runtime cannot honour means
the map promises a route for an operation that then fails.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from .canonical import digest16
from .groups import Group, colocation_groups, group_of
from .model import LogicalModel

__all__ = ["SHAPE_KINDS", "OperationShape", "enumerate_shapes"]

SHAPE_KINDS: Final[tuple[str, ...]] = (
    "point_read",
    "range_read",
    "aggregate",
    "full_scan",
    "relation_walk",
    "write",
    "bulk_write",
)

# Types over which a range predicate is meaningful. Ranges over strings and uuids are legal SQL and
# almost never what anybody means, so they are not enumerated; if telemetry ever shows one, that is
# a signal to revisit this list rather than to widen it speculatively.
_ORDERED_PREFIXES: Final[tuple[str, ...]] = (
    "int32",
    "int64",
    "float32",
    "float64",
    "decimal",
    "date",
    "timestamp",
)


@dataclass(frozen=True)
class OperationShape:
    """One kind of operation, without any of its values."""

    group: str
    kind: str
    entity: str
    fields: tuple[str, ...]
    target: str | None = None

    # Computed once, in __post_init__, and stored. It used to be a property, which meant a SHA-256
    # over a freshly built and canonically encoded dict on every access - and routing reads it up to
    # three times per operation. The overhead test measured 41 microseconds median to resolve one
    # route, sixteen percent of a PostgreSQL round trip, against a budget of one percent. The same
    # mistake as building a key four times per write, which the engine paid for once already.
    id: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # object.__setattr__ because the dataclass is frozen. The alternative - a memo keyed by the
        # shape - would put a dictionary lookup back on the hot path to avoid a hash, which is the
        # wrong trade when shapes are enumerated once per model and live for the process.
        object.__setattr__(self, "id", digest16(self.as_ir()))

    def as_ir(self) -> dict[str, Any]:
        # Sorted fields, explicit nulls: the shape is hashed, so its encoding has to be as stable as
        # the model's.
        return {
            "group": self.group,
            "kind": self.kind,
            "entity": self.entity,
            "fields": list(self.fields),
            "target": self.target,
        }


def _is_ordered(neutral_type: str) -> bool:
    return neutral_type.startswith(_ORDERED_PREFIXES)


def enumerate_shapes(model: LogicalModel) -> tuple[OperationShape, ...]:
    """Every shape the model admits, in a deterministic order.

    Ordering is by ``(group, entity, kind, fields, target)`` rather than by identifier, so that a
    human reading a placement map or a diff between two of them sees related shapes together instead
    of scattered by hash.
    """
    groups: tuple[Group, ...] = colocation_groups(model)
    shapes: list[OperationShape] = []

    for spec in model.entities:
        group = group_of(groups, spec.name).name

        shapes.append(
            OperationShape(
                group=group, kind="point_read", entity=spec.name, fields=tuple(sorted(spec.key))
            )
        )
        shapes.append(OperationShape(group=group, kind="write", entity=spec.name, fields=()))
        shapes.append(OperationShape(group=group, kind="bulk_write", entity=spec.name, fields=()))
        shapes.append(OperationShape(group=group, kind="full_scan", entity=spec.name, fields=()))
        shapes.append(OperationShape(group=group, kind="aggregate", entity=spec.name, fields=()))

        for spec_field in spec.fields:
            if _is_ordered(spec_field.type):
                shapes.append(
                    OperationShape(
                        group=group, kind="range_read", entity=spec.name, fields=(spec_field.name,)
                    )
                )

    for relation in model.relations:
        group = group_of(groups, relation.source).name
        shapes.append(
            OperationShape(
                group=group,
                kind="relation_walk",
                entity=relation.source,
                fields=(relation.name,),
                target=relation.target,
            )
        )

    shapes.sort(key=lambda s: (s.group, s.entity, s.kind, s.fields, s.target or ""))
    return tuple(shapes)

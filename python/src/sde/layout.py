"""Deriving a default physical layout from a model.

There is a boundary question here worth answering explicitly, because getting it wrong would either
leak the paid part into the open one or make the open one useless on its own.

The interesting decisions - which engine a group goes to, which indexes earn their cost, when to
partition, when a second materialisation pays for itself, when to move - are the planner's, and the
planner is the part clients pay for. None of that is here.

What *is* here is the boring, total function from a model to a schema that stores it: table names,
column names, column types, a primary key, foreign key columns for relations. That has to be in the
library, because the library has to work without an account (requirement 12.5). A hand-written
placement map that says ``"layout": {"auto": true}`` gets this, and everything runs with no key and
no network. A map from the planner carries an explicit layout instead, and then this is not used.

The derivation is deterministic and documented, because it reaches a client's database as DDL and
they will read it.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Final

from .errors import DeclarationError
from .groups import Group
from .model import LogicalModel
from .placement import PhysicalLayout

__all__ = ["POSTGRES_TYPES", "default_layout", "snake_case"]

_CAMEL_BOUNDARY: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

POSTGRES_TYPES: Final[Mapping[str, str]] = {
    "bool": "boolean",
    "int32": "integer",
    "int64": "bigint",
    "float32": "real",
    "float64": "double precision",
    "string": "text",
    "bytes": "bytea",
    "uuid": "uuid",
    "date": "date",
    "timestamp": "timestamp",
    "timestamptz": "timestamptz",
    "json": "jsonb",
}


def snake_case(name: str) -> str:
    """``OrderLine`` -> ``order_line``, and NFC-normalised.

    Normalisation matters for the same reason it matters in the canonical encoding: an identifier
    written with a combining accent and one written composed would otherwise produce two different
    table names for the same entity, and only one of them would have the data in it.
    """
    normalised = unicodedata.normalize("NFC", name)
    return _CAMEL_BOUNDARY.sub("_", normalised).lower()


def _column_type(neutral: str) -> str:
    if neutral.startswith("decimal("):
        digits, scale = neutral[len("decimal(") : -1].split(",")
        return f"numeric({digits},{scale})"
    try:
        return POSTGRES_TYPES[neutral]
    except KeyError:
        raise DeclarationError(
            f"no PostgreSQL type for {neutral!r}. Every member of the neutral vocabulary needs "
            "one; "
            "this is a gap in the adapter rather than a problem with the model."
        ) from None


def default_layout(
    model: LogicalModel, group: Group, *, dialect: str = "postgres"
) -> PhysicalLayout:
    """The obvious schema for a group in one engine.

    Foreign key columns are named ``<relation>_<target key field>``, which is the convention nearly
    every ORM uses, so a client looking at their own database recognises what they are seeing. A
    relation to an entity with a composite key produces one column per key field.
    """
    if dialect != "postgres":
        raise DeclarationError(
            f"no default layout for dialect {dialect!r} yet. Adding one is an adapter's job, and "
            "it "
            "has to be added deliberately rather than approximated from this one."
        )

    tables: dict[str, str] = {}
    columns: dict[str, dict[str, str]] = {}
    indexes: list[dict[str, object]] = []

    for entity_name in group.members:
        spec = model.entity(entity_name)
        tables[entity_name] = snake_case(entity_name)

        cols: dict[str, str] = {}
        for field in spec.fields:
            cols[field.name] = _column_type(field.type)

        for relation in model.relations:
            if relation.source != entity_name:
                continue
            target = model.entity(relation.target)
            for key_field in target.key:
                key_spec = next(f for f in target.fields if f.name == key_field)
                # <relation>_<key field>, for a single-field key and for a composite one alike. An
                # earlier version branched on the key's arity and produced the same name in both
                # arms, which ruff spotted as a useless condition - correctly, and it was a leftover
                # from an idea about naming that turned out not to be worth the inconsistency.
                cols[f"{relation.name}_{key_field}"] = _column_type(key_spec.type)

        columns[entity_name] = cols

        # One index, and only because a foreign key without one turns every relation walk into a
        # sequential scan. Anything beyond this is a planner decision with a cost attached, and the
        # library has no business guessing at it.
        for relation in model.relations:
            if relation.source != entity_name:
                continue
            target = model.entity(relation.target)
            indexes.append(
                {
                    "entity": entity_name,
                    "name": f"{snake_case(entity_name)}_{relation.name}_idx",
                    "columns": [f"{relation.name}_{k}" for k in target.key],
                }
            )

    return PhysicalLayout(tables=tables, columns=columns, indexes=tuple(indexes))

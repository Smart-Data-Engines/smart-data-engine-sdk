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

__all__ = [
    "CLICKHOUSE_TYPES",
    "FIXED_SCHEMA",
    "ORDERBOOK_KEY",
    "ORDERBOOK_SHAPE",
    "ORDERBOOK_TABLE",
    "ORDERBOOK_TYPES",
    "POSTGRES_TYPES",
    "can_store",
    "default_layout",
    "fixed_schema_mismatch",
    "group_columns",
    "snake_case",
    "stored_types",
]

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

CLICKHOUSE_TYPES: Final[Mapping[str, str]] = {
    "bool": "Bool",
    "int32": "Int32",
    "int64": "Int64",
    "float32": "Float32",
    "float64": "Float64",
    "string": "String",
    # `bytes` and `json` are deliberately absent, and their absence is a *refusal* rather than an
    # oversight. `_column_type` raises for an unmapped neutral type, so a model with either field
    # cannot have a ClickHouse layout derived - which means the planner cannot place that group here
    # and the failure lands at map-build time, in our process, loudly. Both were measured before
    # being given up on:
    #
    # `bytes`: a ClickHouse `String` stores the bytes correctly - `hex()` and `length()` on the
    # server confirm all four bytes of b"\x00\x01\xff\xfe" arrive intact. The read lies. The driver
    # decodes a `String` column to `str`, cannot decode invalid UTF-8, and returns the hex text
    # "0001fffe" instead. Nothing distinguishes a binary `String` column from a text one on the way
    # back, so the adapter cannot correct it, and silently handing a client hex text where they
    # wrote bytes is the kind of corruption that surfaces years later in a checksum.
    #
    # `json`: PostgreSQL `jsonb` returns a parsed `dict`; a ClickHouse `String` returns the
    # original text. The same field would change Python type when its group moved, which breaks the
    # promise the whole product is built on. The native `JSON` type would fix it and was
    # experimental in the server versions in scope, and a signed placement map has to outlive a
    # server upgrade. Resolving this properly means deciding what the neutral `json` type promises
    # on the way *back* - a dict or the exact text - and changing both adapters together. It is a
    # task, not a mapping.
    "uuid": "UUID",
    # Date32 rather than Date: Date covers 1970-2149, which is a range a business date can leave.
    # Silently clamping a date is worse than storing four bytes more.
    "date": "Date32",
    # Millisecond precision, stated. ClickHouse's plain DateTime is second-resolution, so a client
    # who wrote 12:00:00.500 would read back 12:00:00 - a lossy round trip that no test with
    # whole-second fixtures would notice.
    "timestamp": "DateTime64(3)",
    "timestamptz": "DateTime64(3, 'UTC')",
}


def snake_case(name: str) -> str:
    """``OrderLine`` -> ``order_line``, and NFC-normalised.

    Normalisation matters for the same reason it matters in the canonical encoding: an identifier
    written with a combining accent and one written composed would otherwise produce two different
    table names for the same entity, and only one of them would have the data in it.
    """
    normalised = unicodedata.normalize("NFC", name)
    return _CAMEL_BOUNDARY.sub("_", normalised).lower()


ORDERBOOK_TYPES: Final[Mapping[str, str]] = {
    # Only the types the fixed shape uses, and their engine spelling is the C type the API takes.
    # Everything else is absent, and the absence is the same refusal ClickHouse makes for `bytes`:
    # a neutral type with no entry here cannot be given a column, so the group cannot be placed.
    # It matters less here than there, because a model with an extra *field* is already refused by
    # `fixed_schema_mismatch` - but the two refusals have to agree, and the cheapest way to make
    # them agree is for this table to hold exactly the types the shape names.
    "string": "char*",
    "int32": "uint32_t",
    "int64": "int64_t",
}

_DIALECT_TYPES: Final[Mapping[str, Mapping[str, str]]] = {
    "postgres": POSTGRES_TYPES,
    "clickhouse": CLICKHOUSE_TYPES,
    "orderbook": ORDERBOOK_TYPES,
}

# The dialects this library knows, as one public list. It exists because the control plane kept its
# own vocabulary - `postgresql` there, `postgres` here - and the two agreed only for as long as
# nothing joined them. The first code that needed both spellings to match was the one rendering DDL
# for an engine named in the registry, and it failed at runtime rather than at any earlier point.
DIALECTS: Final[tuple[str, ...]] = ("clickhouse", "orderbook", "postgres")

_DECIMAL: Final[Mapping[str, str]] = {
    "postgres": "numeric({digits},{scale})",
    "clickhouse": "Decimal({digits}, {scale})",
}


# ── An engine whose schema is not ours to choose ─────────────────────────────────────────────────
#
# Everything above answers "what columns should this entity get here". The orderbook engine answers
# it first: it stores L2 depth in one shape, fixed in C++, and there is no CREATE TABLE to send it.
# So the relationship inverts. For PostgreSQL and ClickHouse the client declares a model and we
# decide the physical schema; here the engine has already decided, and either the client's model
# *is* that shape or the group cannot go there.
#
# That is stated rather than smoothed over, because smoothing it over has only bad forms. Mapping
# the client's field names onto the engine's - `at` onto `timestamp_ns`, `qty` onto `quantity` -
# would mean guessing which declared field is the price from what it is called, and reasoning from a
# name is the one thing this product refuses everywhere else. Accepting a wider model and dropping
# the extra fields would lose data in an engine chosen for not losing any.
#
# The consequence is a constraint on the model, and the refusal names the whole expected shape so it
# is actionable in one read.

ORDERBOOK_TABLE: Final[str] = "orderbook"
"""The engine's own name for its storage. One table, and we did not name it."""

ORDERBOOK_SHAPE: Final[Mapping[str, str]] = {
    # The address. Not columns in a result row - the engine's query language takes them in the FROM
    # clause, `FROM 'BTCUSDT'.'binance'` - but they are fields of the entity and part of its key,
    # because two rows differing only in symbol are different rows.
    "symbol": "string",
    "exchange": "string",
    # The row.
    "timestamp_ns": "int64",
    "side": "string",
    "level": "int32",
    "price": "int64",
    "quantity": "int64",
    "order_count": "int32",
    # Nullable in the model and *unknown* rather than zero when the engine cannot supply it. The
    # engine returns 0 for a row whose sequence number it does not have, which is a safe sentinel
    # there because its own numbering starts at 1 - and it is not safe here, because a client
    # comparing sequence numbers cannot tell a sentinel from a value. The adapter converts.
    "sequence_number": "int64",
}
"""The nine fields an entity must declare, by name and by neutral type, to live in this engine.

``price`` and ``quantity`` are integers in the engine's sub-unit, not decimals. That is the engine's
choice and it is the right one for an orderbook - a decimal per level per update is a rounding
question in the hot path - but it means a client who declared ``decimal(12,2)`` is declaring a
different model, and this refuses rather than converting. Converting would put our arithmetic
between the client's price and their storage.
"""

ORDERBOOK_KEY: Final[tuple[str, ...]] = (
    "symbol",
    "exchange",
    "timestamp_ns",
    "side",
    "level",
)
"""The key, in the order the engine addresses by. Positional and load-bearing, as in ClickHouse."""

FIXED_SCHEMA: Final[frozenset[str]] = frozenset({"orderbook"})
"""Dialects whose physical schema the engine imposes rather than accepting from us.

A named set rather than a check on the dialect string, so that the three places that have to behave
differently - the layout, the DDL renderer and the control plane's eligibility filter - agree by
construction instead of each testing for one name.
"""

_FIXED_SHAPES: Final[Mapping[str, tuple[str, Mapping[str, str], tuple[str, ...]]]] = {
    "orderbook": (ORDERBOOK_TABLE, ORDERBOOK_SHAPE, ORDERBOOK_KEY),
}

assert set(_FIXED_SHAPES) == set(FIXED_SCHEMA), sorted(set(_FIXED_SHAPES) ^ set(FIXED_SCHEMA))


def fixed_schema_mismatch(
    columns: Mapping[str, Mapping[str, str]], *, dialect: str
) -> str | None:
    """Why a group cannot live in a fixed-schema engine, or ``None`` if it can.

    ``columns`` is entity name to column name to neutral type - what :func:`group_columns` returns.
    ``None`` for a dialect that is not fixed-schema, because "no objection" is the honest answer to
    a question that does not apply: the caller asks this once per engine and branching on the
    dialect at the call site would put the set of fixed-schema engines in two places.

    A type check alone is not enough and that is the whole reason this exists. The orderbook shape
    is made of ``string``, ``int32`` and ``int64``, all of which every engine can store - so a model
    of two integers and a string passes representability and then fails while the layout is built,
    which is the failure this function was added to stop happening twice.
    """
    fixed = _FIXED_SHAPES.get(dialect)
    if fixed is None:
        return None
    _, shape, _ = fixed

    if len(columns) != 1:
        return (
            f"this engine stores one thing, and this group has {len(columns)} entities "
            f"({sorted(columns)}). A colocation group is what shares an engine, so a group of two "
            f"cannot go somewhere with room for one."
        )
    entity, declared = next(iter(columns.items()))

    missing = sorted(set(shape) - set(declared))
    extra = sorted(set(declared) - set(shape))
    wrong = sorted(
        f"{name} is declared {declared[name]!r} and this engine stores {shape[name]!r}"
        for name in sorted(set(shape) & set(declared))
        if declared[name] != shape[name]
    )
    if not (missing or extra or wrong):
        return None

    problems = []
    if missing:
        problems.append(f"{entity} declares no {missing}")
    if extra:
        problems.append(f"{entity} declares {extra}, which this engine has nowhere to put")
    if wrong:
        problems.append("; ".join(wrong))
    expected = ", ".join(f"{name}: {kind}" for name, kind in shape.items())
    return (
        f"{'. '.join(problems)}. This engine's schema is fixed in the engine and not chosen by us, "
        f"so a model either is that shape or cannot be stored here. The shape is exactly: "
        f"{expected}."
    )


def _column_type(neutral: str, dialect: str) -> str:
    """One neutral type, one engine type, and no defaulting.

    A missing entry raises rather than falling back to a string, because a type nobody mapped is a
    gap in an adapter and the honest place to find that out is here - not in a client's database,
    where the column already exists with the wrong type and changing it is a migration.
    """
    types = _DIALECT_TYPES[dialect]
    if neutral.startswith("decimal("):
        digits, scale = neutral[len("decimal(") : -1].split(",")
        template = _DECIMAL.get(dialect)
        if template is None:
            # A DeclarationError rather than a KeyError, because this is the same answer as any
            # other unmapped neutral type - the engine cannot give this column a type - and
            # `can_store` distinguishes "cannot store" from "does not parse" by which exception it
            # sees. The orderbook engine stores prices as integers in a sub-unit; a decimal there
            # would need our arithmetic between the client's price and their storage.
            raise DeclarationError(
                f"no {dialect} type for {neutral!r}. This engine has no decimal type at all, so "
                f"this is a gap in the model for this engine rather than in the adapter."
            )
        return template.format(digits=digits, scale=scale)
    try:
        return types[neutral]
    except KeyError:
        raise DeclarationError(
            f"no {dialect} type for {neutral!r}. Every member of the neutral vocabulary needs one; "
            "this is a gap in the adapter rather than a problem with the model."
        ) from None


def can_store(neutral: str, *, dialect: str) -> bool:
    """Whether this dialect has a column type for this neutral type.

    The one public question about representability, and it exists because the answer was previously
    only obtainable by trying: a group with a ``bytes`` field placed in ClickHouse produced a
    valid scoring decision and then raised out of ``default_layout``. The refusal was correct and it
    landed in the wrong place - at map-build time, where it reads as our defect rather than as a
    reason one engine was not a candidate.

    Implemented by asking the renderer rather than by a second table. A representability check that
    could disagree with the type mapping would be worse than none: it would let a placement be
    approved and then fail to apply, which is the failure the byte contract exists to prevent.

    A malformed type - ``decimal(x)`` - is not answered ``False``. "This engine cannot store it"
    is a claim about the engine; a type that parses nowhere is a claim about the model, and the
    model's own validation owns it. So that raises through.
    """
    if dialect not in _DIALECT_TYPES:
        raise DeclarationError(
            f"no type table for dialect {dialect!r}; this library knows {sorted(_DIALECT_TYPES)}. "
            f"Answering False would say 'that engine cannot store it' about an engine this library "
            f"has never heard of."
        )
    try:
        _column_type(neutral, dialect)
    except DeclarationError:
        return False
    return True


def _neutral_columns(model: LogicalModel, group: Group) -> dict[str, dict[str, str]]:
    """Every column a group's tables need, in the neutral vocabulary, before any dialect.

    Factored out of ``default_layout`` because two things need it and a second copy of the loop is
    how they stop agreeing. ``default_layout`` maps each of these through ``_column_type``;
    ``stored_types`` asks which of them an engine can represent at all.

    A group has a foreign-key column for every relation whose source is a member. Today those add no
    *type* the group did not already have, because a relation unions its two ends into one
    colocation group, so the target's key fields are declared fields of a member. That is a fact
    about how groups are formed rather than about layouts, and this function does not rely on it -
    which is the point of deriving the set here instead of from ``spec.fields`` at the call site.

    Insertion order is the declared field order, then relations in declared order. Callers that need
    a stable ordering get one without sorting; callers that sort - the DDL renderer does - are
    unaffected.
    """
    out: dict[str, dict[str, str]] = {}
    for entity_name in group.members:
        spec = model.entity(entity_name)
        cols: dict[str, str] = {field.name: field.type for field in spec.fields}
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
                cols[f"{relation.name}_{key_field}"] = key_spec.type
        out[entity_name] = cols
    return out


def group_columns(model: LogicalModel, group: Group) -> Mapping[str, Mapping[str, str]]:
    """Every column a group's tables need, per entity, in the neutral vocabulary.

    The public form of the derivation the layout uses. Exposed because the control plane has two
    questions to ask before it picks an engine - can this engine store these types, and does this
    group *fit* an engine whose shape is fixed - and the second one needs the column names, not just
    the types.
    """
    return _neutral_columns(model, group)


def stored_types(model: LogicalModel, group: Group) -> tuple[str, ...]:
    """The neutral types a group's tables need, sorted and deduplicated.

    For the control plane, which has to answer "can this engine hold this group" *before* it picks
    one. Without this the only available answer was to place the group and watch ``default_layout``
    raise - a correct refusal in the wrong place, because it arrives after the decision is made and
    reads as our defect rather than as the reason an engine was not a candidate.

    Paired with :func:`can_store`, which is asked once per type. Both come from the same column
    derivation the layout uses, so an engine reported able to hold a group is one whose layout can
    actually be rendered - and that equivalence is what the control plane relies on when it excludes
    an engine instead of discovering the problem while building the map.
    """
    return tuple(
        sorted(
            {
                column_type
                for cols in _neutral_columns(model, group).values()
                for column_type in cols.values()
            }
        )
    )


def default_layout(
    model: LogicalModel, group: Group, *, dialect: str = "postgres"
) -> PhysicalLayout:
    """The obvious schema for a group in one engine.

    Foreign key columns are named ``<relation>_<target key field>``, which is the convention nearly
    every ORM uses, so a client looking at their own database recognises what they are seeing. A
    relation to an entity with a composite key produces one column per key field.
    """
    if dialect not in _DIALECT_TYPES:
        raise DeclarationError(
            f"no default layout for dialect {dialect!r}. Adding one is an adapter's job, and it "
            f"has "
            f"to be added deliberately rather than approximated from an existing one - the known "
            f"dialects are {sorted(_DIALECT_TYPES)}."
        )

    neutral = _neutral_columns(model, group)

    if dialect in FIXED_SCHEMA:
        mismatch = fixed_schema_mismatch(neutral, dialect=dialect)
        if mismatch is not None:
            raise DeclarationError(mismatch)
        table, shape, _ = _FIXED_SHAPES[dialect]
        entity = next(iter(neutral))
        # The engine's own table name and the engine's own column names, typed through the same
        # `_column_type` as everything else so that an unmapped neutral type still raises here
        # rather than producing a column with a plausible C type nobody chose.
        return PhysicalLayout(
            tables={entity: table},
            columns={entity: {name: _column_type(kind, dialect) for name, kind in shape.items()}},
            indexes=(),
        )

    tables: dict[str, str] = {}
    columns: dict[str, dict[str, str]] = {}
    indexes: list[dict[str, object]] = []

    for entity_name in group.members:
        tables[entity_name] = snake_case(entity_name)
        columns[entity_name] = {
            column: _column_type(neutral_type, dialect)
            for column, neutral_type in neutral[entity_name].items()
        }

        # One index, and only because a foreign key without one turns every relation walk into a
        # sequential scan. Anything beyond this is a planner decision with a cost attached, and the
        # library has no business guessing at it.
        #
        # None at all for ClickHouse, and that is not an omission. It has no B-tree: `CREATE INDEX`
        # there builds a *data-skipping* index, which needs a type and a granularity and helps only
        # when the data is already ordered so whole granules can be ruled out. Choosing those is a
        # planner decision with a cost attached, exactly like the ones above that this function
        # refuses to make. What a MergeTree does have is its `ORDER BY`, which is the primary index
        # and is derived from the declared key by the adapter - so the useful index exists, it is
        # simply not expressed as a row in this list.
        if dialect != "clickhouse":
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

"""The layout, per dialect, without needing a server.

The slices in `test_postgres_slice.py`, `test_clickhouse_slice.py` and `test_engine_agreement.py`
check that a schema we derived is one a real engine accepts. These check the derivation itself,
which matters separately for one reason: the control plane calls `default_layout` to build the map
it signs, and it does that without a database anywhere near it. A wrong type here becomes a signed
document.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from typing import Annotated

import pytest

import sde
from sde.errors import DeclarationError
from sde.layout import CLICKHOUSE_TYPES, POSTGRES_TYPES, default_layout
from sde.types import NEUTRAL_TYPES


def _model() -> sde.LogicalModel:
    sde.clear_registry()

    @sde.entity
    class User:
        id: uuid.UUID
        email: str

    @sde.entity
    class Order:
        id: uuid.UUID
        user: sde.Ref[User]
        total: Annotated[decimal.Decimal, sde.precision(12, 2)]
        placed_at: dt.datetime
        note: str | None

    return sde.build_model(User, Order)


def test_postgres_maps_the_whole_neutral_vocabulary() -> None:
    """Every neutral type has a PostgreSQL type. This is the reference dialect, so no gaps."""
    missing = sorted(NEUTRAL_TYPES - set(POSTGRES_TYPES))
    assert not missing, f"no PostgreSQL type for {missing}"


def test_clickhouse_maps_everything_except_two_named_gaps() -> None:
    """The gaps are refusals, and this is where the reason lives.

    An unmapped neutral type makes `default_layout` raise for that dialect, which makes the planner
    unable to place a group containing such a field there - the constraint arrives as an exception
    in our process at map-build time rather than as a wrong value in a client's database.
    """
    missing = sorted(NEUTRAL_TYPES - set(CLICKHOUSE_TYPES))
    assert missing == ["bytes", "json"], (
        f"the ClickHouse gaps are now {missing}. `bytes`: a ClickHouse String stores the bytes "
        "correctly but the driver decodes the column to str on the way back and returns hex text "
        "for "
        "anything that is not valid UTF-8, and nothing distinguishes a binary String from a text "
        "one. "
        f"`json`: PostgreSQL returns a parsed dict and a String returns the original text, so the "
        f"field would change Python type when its group moved."
    )


@pytest.mark.parametrize("gap", ["bytes", "json"])
def test_a_field_of_an_unmapped_type_cannot_get_a_clickhouse_layout(gap: str) -> None:
    """The refusal, end to end from a declaration.

    This is the mechanism that turns "we cannot represent this faithfully" into "the planner will
    not put this group there", and it needs no new machinery: an unmapped type raises.
    """
    sde.clear_registry()

    if gap == "bytes":

        @sde.entity
        class Thing:
            id: uuid.UUID
            payload: bytes
    else:

        @sde.entity
        class Thing:  # type: ignore[no-redef]
            id: uuid.UUID
            payload: dict

    model = sde.build_model(Thing)
    group = sde.colocation_groups(model)[0]

    # PostgreSQL is fine with both.
    assert default_layout(model, group, dialect="postgres").columns["Thing"]["payload"]

    with pytest.raises(DeclarationError, match=f"no clickhouse type for '{gap}'"):
        default_layout(model, group, dialect="clickhouse")


def test_the_two_dialects_agree_on_names_and_differ_only_on_types() -> None:
    """Table and column names are the client's schema; types are the engine's.

    A migration between engines must not rename anything, or every saved analyst query breaks for a
    reason that has nothing to do with the move. So the names are asserted identical and the types
    are asserted different - both directions, because "identical" passing by accident is the failure
    that would make this test decorative.
    """
    model = _model()
    for group in sde.colocation_groups(model):
        pg = default_layout(model, group, dialect="postgres")
        ch = default_layout(model, group, dialect="clickhouse")

        assert pg.tables == ch.tables
        assert set(pg.columns) == set(ch.columns)
        for entity in pg.columns:
            assert set(pg.columns[entity]) == set(ch.columns[entity]), (
                f"{entity} has different columns in the two dialects, which would make a move a "
                f"rename"
            )
            assert pg.columns[entity] != ch.columns[entity], (
                f"{entity} has identical types in both dialects, which cannot be right - "
                f"`numeric(12,2)` and `Decimal(12, 2)` are not the same string"
            )


def test_clickhouse_gets_no_index_definitions_and_postgres_does() -> None:
    """Not an omission. See `sde.layout` - a ClickHouse index needs a type and a granularity."""
    model = _model()
    group = next(g for g in sde.colocation_groups(model) if "Order" in g.members)

    assert default_layout(model, group, dialect="postgres").indexes, (
        "a foreign key without an index turns every relation walk into a sequential scan"
    )
    assert default_layout(model, group, dialect="clickhouse").indexes == ()


def test_an_unknown_dialect_is_refused_by_name() -> None:
    model = _model()
    group = sde.colocation_groups(model)[0]
    with pytest.raises(DeclarationError, match="no default layout for dialect 'duckdb'"):
        default_layout(model, group, dialect="duckdb")


def test_decimal_precision_survives_into_both_dialects() -> None:
    """Money, in the two syntaxes. A `numeric` that lost its scale is a column that loses cents."""
    model = _model()
    group = next(g for g in sde.colocation_groups(model) if "Order" in g.members)

    assert default_layout(model, group, dialect="postgres").columns["Order"]["total"] == (
        "numeric(12,2)"
    )
    assert default_layout(model, group, dialect="clickhouse").columns["Order"]["total"] == (
        "Decimal(12, 2)"
    )

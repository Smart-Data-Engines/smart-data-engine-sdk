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
from sde.layout import CLICKHOUSE_TYPES, POSTGRES_TYPES, _column_type, default_layout
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


# ── can_store: the one public question about representability ──────────────────────────────────


def test_can_store_agrees_with_the_type_mapping_for_the_whole_vocabulary() -> None:
    """The property that makes the question worth exposing at all.

    A representability check that could answer True where the renderer raises would let a placement
    be approved and then fail to apply - which is worse than not being able to ask, because the
    failure moves from planning time into the client's request path. So it is not checked against
    a hand-written list of what each engine supports; that list is the thing that drifts. It is
    checked against the function that types the columns, across the whole neutral vocabulary.
    """
    vocabulary = [*sorted(NEUTRAL_TYPES), "decimal(12,2)", "decimal(38,10)"]
    for dialect in sde.DIALECTS:
        for neutral in vocabulary:
            try:
                _column_type(neutral, dialect)
            except DeclarationError:
                typed = False
            else:
                typed = True
            assert sde.can_store(neutral, dialect=dialect) is typed, (
                f"can_store({neutral!r}, dialect={dialect!r}) disagrees with _column_type"
            )


def test_the_two_refusals_clickhouse_makes_are_visible_without_trying_to_build_a_layout() -> None:
    """Before this, the only way to learn either was to place the group and watch it raise."""
    assert sde.can_store("bytes", dialect="postgres") is True
    assert sde.can_store("json", dialect="postgres") is True
    assert sde.can_store("bytes", dialect="clickhouse") is False
    assert sde.can_store("json", dialect="clickhouse") is False


def test_an_unknown_dialect_raises_rather_than_answering_false() -> None:
    """False would be a claim about an engine this library has never heard of."""
    with pytest.raises(DeclarationError, match="no type table for dialect"):
        sde.can_store("int64", dialect="duckdb")


def test_a_malformed_type_raises_through_rather_than_reporting_unstorable() -> None:
    '''"This engine cannot store it" and "that type parses nowhere" are different findings.'''
    with pytest.raises((ValueError, KeyError)):
        sde.can_store("decimal(oops)", dialect="postgres")


# ── stored_types: what the control plane asks before it picks an engine ─────────────────────────


def _model_with_a_clickhouse_hostile_field() -> sde.LogicalModel:
    sde.clear_registry()

    @sde.entity
    class Attachment:
        id: uuid.UUID
        blob: bytes

    @sde.entity
    class Message:
        id: uuid.UUID
        attachment: sde.Ref[Attachment]
        body: str

    return sde.build_model(Attachment, Message)


def test_stored_types_and_can_store_predict_whether_the_layout_renders() -> None:
    """The equivalence the control plane's exclusion rests on.

    If these two could disagree, an engine would be reported able to hold a group and then fail
    while the map was being built - which is the situation this pair exists to end, only moved one
    step later and harder to read.
    """
    for model in (_model(), _model_with_a_clickhouse_hostile_field()):
        for group in sde.colocation_groups(model):
            types = sde.stored_types(model, group)
            for dialect in sde.DIALECTS:
                predicted = all(sde.can_store(t, dialect=dialect) for t in types)
                try:
                    default_layout(model, group, dialect=dialect)
                except DeclarationError:
                    rendered = False
                else:
                    rendered = True
                assert predicted is rendered, (
                    f"{group.name} in {dialect}: predicted {predicted}, layout rendered {rendered}"
                )


def test_a_relation_puts_the_targets_key_type_in_the_set() -> None:
    """A foreign-key column is a column, so its type is a type this engine has to have.

    A relation unions its ends into one group, so today the target's key type is also a declared
    field of a member and the set would be the same either way. Pinned anyway: if that ever changes,
    the failure is a placement approved for an engine that cannot type the foreign key.
    """
    model = _model_with_a_clickhouse_hostile_field()
    group = sde.group_of(sde.colocation_groups(model), "Message")
    assert "Attachment" in group.members, "a Ref should colocate; this test assumes it"
    assert "uuid" in sde.stored_types(model, group)


def test_the_clickhouse_gap_is_visible_before_a_layout_is_attempted() -> None:
    model = _model_with_a_clickhouse_hostile_field()
    group = sde.group_of(sde.colocation_groups(model), "Message")
    types = sde.stored_types(model, group)
    assert "bytes" in types
    assert all(sde.can_store(t, dialect="postgres") for t in types)
    assert not all(sde.can_store(t, dialect="clickhouse") for t in types)

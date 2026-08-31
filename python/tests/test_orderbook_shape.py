"""An engine whose schema is not ours: the fixed-shape concept, without an engine anywhere.

PostgreSQL and ClickHouse take a schema derived from the client's model. The orderbook engine has
already decided: it stores L2 depth in one shape, fixed in C++, and there is no CREATE TABLE to send
it. So a group either *is* that shape or it cannot be placed there, and every piece of the library
that used to answer "what columns should this get" has to answer "does this fit" instead.

These tests are about that inversion. The adapter is tested in `test_orderbook_adapter.py` and the
engine's measured behaviour in `test_orderbook_slice.py`.
"""

from __future__ import annotations

import pytest

import sde
from sde.errors import DeclarationError, EngineError
from sde.testing.loader import model_from_neutral


def _model(**fields: str) -> sde.LogicalModel:
    """A one-entity model with exactly the given field names and neutral types.

    Built from a declaration rather than through the decorator, because the point of most of these
    tests is a shape the decorator's Python types cannot express - `int64` and `int32` in the same
    entity, `string` where a reader expects an enum.

    The key is whichever of the orderbook key fields the model actually has, falling back to the
    first field. A key naming a field that does not exist is refused by the loader, and rightly:
    these tests are about the *shape* being wrong, and a model that cannot be declared at all would
    be testing the loader instead.
    """
    sde.clear_registry()
    key = [name for name in sde.ORDERBOOK_KEY if name in fields] or [next(iter(fields))]
    return model_from_neutral(
        {
            "name": "m",
            "entities": [
                {
                    "name": "Depth",
                    "fields": [
                        {"name": name, "type": kind, "nullable": False}
                        for name, kind in fields.items()
                    ],
                    "key": key,
                }
            ],
            "relations": [],
            "atomic": [],
        }
    )


def _orderbook_model() -> sde.LogicalModel:
    return _model(**dict(sde.ORDERBOOK_SHAPE))


def _group(model: sde.LogicalModel) -> sde.Group:
    return sde.colocation_groups(model)[0]


# ── The shape itself ────────────────────────────────────────────────────────────────────────────


def test_the_shape_and_the_key_agree_with_each_other() -> None:
    """The key names columns of the shape, in the engine's addressing order."""
    assert set(sde.ORDERBOOK_KEY) <= set(sde.ORDERBOOK_SHAPE)
    assert sde.ORDERBOOK_KEY[:2] == ("symbol", "exchange"), (
        "the engine's query language takes these two in the FROM clause; a key not starting with "
        "them would describe rows the engine cannot address"
    )


def test_orderbook_is_a_dialect_and_a_fixed_schema_one() -> None:
    assert "orderbook" in sde.DIALECTS
    assert "orderbook" in sde.FIXED_SCHEMA
    assert set(sde.DIALECTS) > sde.FIXED_SCHEMA, (
        "a fixed-schema dialect this library does not otherwise know would be one it can refuse a "
        "group for and never render anything for"
    )


# ── fixed_schema_mismatch ───────────────────────────────────────────────────────────────────────


def test_the_exact_shape_fits() -> None:
    model = _orderbook_model()
    columns = sde.group_columns(model, _group(model))
    assert sde.fixed_schema_mismatch(columns, dialect="orderbook") is None


def test_a_dialect_that_is_not_fixed_schema_has_no_objection() -> None:
    """`None` is the honest answer to a question that does not apply.

    The caller asks this once per engine. Branching on the dialect at the call site would put the
    set of fixed-schema engines in two places, which is how they stop agreeing.
    """
    model = _orderbook_model()
    columns = sde.group_columns(model, _group(model))
    for dialect in ("postgres", "clickhouse"):
        assert sde.fixed_schema_mismatch(columns, dialect=dialect) is None


def test_a_missing_field_is_named_and_the_whole_expected_shape_is_printed() -> None:
    """One read has to be enough to fix it, because the shape is not negotiable."""
    fields = dict(sde.ORDERBOOK_SHAPE)
    del fields["order_count"]
    model = _model(**fields)
    reason = sde.fixed_schema_mismatch(sde.group_columns(model, _group(model)), dialect="orderbook")
    assert reason is not None
    assert "order_count" in reason
    for name in sde.ORDERBOOK_SHAPE:
        assert name in reason, f"the refusal omits {name}, so one read is not enough to fix it"


def test_an_extra_field_is_refused_rather_than_dropped() -> None:
    """Dropping it would lose data in an engine chosen for not losing any."""
    model = _model(**dict(sde.ORDERBOOK_SHAPE), venue_note="string")
    reason = sde.fixed_schema_mismatch(sde.group_columns(model, _group(model)), dialect="orderbook")
    assert reason is not None
    assert "venue_note" in reason
    assert "nowhere to put" in reason


def test_a_field_of_the_right_name_and_wrong_type_is_refused() -> None:
    """The case a name-only check would pass, and the one that would corrupt a price."""
    fields = dict(sde.ORDERBOOK_SHAPE)
    fields["price"] = "decimal(12,2)"
    model = _model(**fields)
    reason = sde.fixed_schema_mismatch(sde.group_columns(model, _group(model)), dialect="orderbook")
    assert reason is not None
    assert "price is declared 'decimal(12,2)' and this engine stores 'int64'" in reason


def test_a_group_of_two_entities_cannot_go_somewhere_with_room_for_one() -> None:
    sde.clear_registry()
    model = model_from_neutral(
        {
            "name": "m",
            "entities": [
                {
                    "name": "Depth",
                    "fields": [
                        {"name": name, "type": kind, "nullable": False}
                        for name, kind in sde.ORDERBOOK_SHAPE.items()
                    ],
                    "key": list(sde.ORDERBOOK_KEY),
                },
                {
                    "name": "Trade",
                    "fields": [{"name": "id", "type": "int64", "nullable": False}],
                    "key": ["id"],
                },
            ],
            "relations": [],
            "atomic": [["Depth", "Trade"]],
        }
    )
    group = sde.colocation_groups(model)[0]
    reason = sde.fixed_schema_mismatch(sde.group_columns(model, group), dialect="orderbook")
    assert reason is not None
    assert "this engine stores one thing" in reason


def test_a_type_check_alone_would_have_let_the_wrong_model_through() -> None:
    """Why this function exists at all, stated as a test rather than as a comment.

    The orderbook shape is made of `string`, `int32` and `int64`. Every engine can store all three,
    so representability - which is what stops a `bytes` group reaching ClickHouse - says nothing
    here. A model of two integers and a string would pass it and then fail while the layout was
    built, which is the failure the representability check was added to stop happening once already.
    """
    model = _model(a="int64", b="int64", c="string")
    columns = sde.group_columns(model, _group(model))
    types = sde.stored_types(model, _group(model))
    assert all(sde.can_store(t, dialect="orderbook") for t in types), (
        "every type here is storable; that is the point"
    )
    assert sde.fixed_schema_mismatch(columns, dialect="orderbook") is not None


# ── default_layout ──────────────────────────────────────────────────────────────────────────────


def test_the_layout_carries_the_engines_own_table_name() -> None:
    model = _orderbook_model()
    layout = sde.default_layout(model, _group(model), dialect="orderbook")
    assert layout.tables == {"Depth": sde.ORDERBOOK_TABLE}
    assert set(layout.columns["Depth"]) == set(sde.ORDERBOOK_SHAPE)
    assert layout.indexes == (), "there is no index to create in an engine that takes no DDL"


def test_a_model_that_does_not_fit_raises_from_the_layout_too() -> None:
    """The same refusal where the map is built, so a wrong map for this engine cannot exist."""
    model = _model(a="int64", b="int64", c="string")
    with pytest.raises(DeclarationError, match="This engine's schema is fixed"):
        sde.default_layout(model, _group(model), dialect="orderbook")


def test_a_decimal_is_refused_rather_than_producing_a_key_error() -> None:
    """`can_store` tells "cannot store" from "does not parse" by the exception, so this matters."""
    assert sde.can_store("decimal(12,2)", dialect="orderbook") is False
    assert sde.can_store("uuid", dialect="orderbook") is False
    assert sde.can_store("int64", dialect="orderbook") is True


# ── DDL, and the honest way to say there is none ────────────────────────────────────────────────


def test_no_ddl_is_rendered_and_that_is_the_correct_action() -> None:
    model = _orderbook_model()
    layout = sde.default_layout(model, _group(model), dialect="orderbook")
    statements = sde.schema_statements(
        layout, keys={"Depth": sde.ORDERBOOK_KEY}, dialect="orderbook"
    )
    assert statements == ()


def test_schema_is_fixed_is_how_an_empty_list_is_told_from_nothing_to_do() -> None:
    assert sde.schema_is_fixed("orderbook") is True
    assert sde.schema_is_fixed("postgres") is False
    assert sde.schema_is_fixed("clickhouse") is False


def test_an_unknown_dialect_raises_rather_than_answering_false() -> None:
    with pytest.raises(EngineError, match="unknown dialect"):
        sde.schema_is_fixed("duckdb")

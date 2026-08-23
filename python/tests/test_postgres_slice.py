"""The thin slice, end to end, against a real PostgreSQL.

This is the test the whole first milestone exists for: a model declared in Python, a schema this
library created, rows written and read back, and not one table name anywhere in the calling code.
Everything after this - telemetry, the planner, a second engine, migration - is worthless if this
does not hold, which is why it is built first and has no intelligence in it at all.

It runs against a real database rather than a fake one. A fake would agree with whatever this
library believes about types, quoting and transactions, which is exactly the set of beliefs worth
checking.

    docker run -d --name sde_test_pg -e POSTGRES_PASSWORD=sde -e POSTGRES_DB=sde \\
        -p 127.0.0.1:55432:5432 postgres:15-alpine
    SDE_POSTGRES_DSN=postgresql://postgres:sde@127.0.0.1:55432/sde pytest
"""

from __future__ import annotations

import datetime as dt
import decimal
import os
import uuid
from collections.abc import Iterator
from typing import Annotated, Any

import pytest

import sde
from sde.engines.postgres import PostgresEngine
from sde.errors import EngineError

DSN = os.environ.get("SDE_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(
    not DSN,
    reason="set SDE_POSTGRES_DSN to run the PostgreSQL slice; skipped rather than faked, because a "
    "fake would agree with whatever this library believes about types and quoting",
)


@pytest.fixture()
def model() -> sde.LogicalModel:
    sde.clear_registry()

    @sde.entity
    class User:
        id: uuid.UUID
        email: str

        class Meta:
            pii = ["email"]

    @sde.entity
    class Order:
        id: uuid.UUID
        user: sde.Ref[User]
        total: Annotated[decimal.Decimal, sde.precision(12, 2)]
        placed_at: dt.datetime

    @sde.entity
    class Event:
        id: uuid.UUID
        name: str
        at: dt.datetime

    return sde.build_model(User, Order, Event)


@pytest.fixture()
def placement(model: sde.LogicalModel) -> sde.PlacementMap:
    """A placement map written by hand, with no signature and no account.

    This is the mode requirement 12.5 promises, exercised as the default in the test suite rather
    than as a special case at the end of it. Note how short it is: that is what ``auto`` buys, and
    without it this mode would be technically available and practically unusable.
    """
    raw: dict[str, Any] = {
        "contract": sde.CONTRACT,
        "model_version": model.version,
        "map_version": 1,
        "groups": {
            group.name: {
                "source": {
                    "id": f"{group.name}@pg",
                    "engine": "pg-test",
                    "layout": {"auto": True},
                }
            }
            for group in sde.colocation_groups(model)
        },
    }
    return sde.load_map(raw, model=model)


@pytest.fixture()
def engine(
    model: sde.LogicalModel, placement: sde.PlacementMap
) -> Iterator[PostgresEngine]:
    assert DSN
    with PostgresEngine(DSN) as eng:
        # Fresh tables per test. Dropping is the test's business, never the library's: the library
        # creates what is missing and changes nothing that exists, because anything else is a
        # migration and migrations carry a safety classification.
        tables = [
            table
            for group in placement.groups.values()
            for table in group.source.layout.tables.values()
        ]
        with eng._cx.cursor() as cur:
            for table in tables:
                cur.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')  # type: ignore[arg-type]

        for group in sde.colocation_groups(model):
            layout = placement.placement_of(group.name).source.layout
            eng.ensure_schema(
                layout,
                keys={name: model.entity(name).key for name in group.members},
            )
        yield eng


def _table(placement: sde.PlacementMap, model: sde.LogicalModel, entity: str) -> str:
    group = next(g for g in sde.colocation_groups(model) if entity in g)
    return placement.placement_of(group.name).source.layout.table_for(entity)


def test_schema_creation_is_idempotent(
    engine: PostgresEngine, model: sde.LogicalModel, placement: sde.PlacementMap
) -> None:
    # Called twice by construction: once in the fixture, once here. An application restarting must
    # not reapply DDL, and two instances starting together must not race.
    for group in sde.colocation_groups(model):
        layout = placement.placement_of(group.name).source.layout
        engine.ensure_schema(layout, keys={n: model.entity(n).key for n in group.members})


def test_write_and_read_back_without_naming_a_table(
    engine: PostgresEngine, model: sde.LogicalModel, placement: sde.PlacementMap
) -> None:
    user_id = uuid.uuid4()
    order_id = uuid.uuid4()

    # The table names come from the placement map, never from the caller. That indirection is the
    # entire product: it is what lets the schema change underneath an application.
    users = _table(placement, model, "User")
    orders = _table(placement, model, "Order")

    engine.insert(users, {"id": user_id, "email": "someone@example.com"})
    engine.insert(
        orders,
        {
            "id": order_id,
            "user_id": user_id,
            "total": decimal.Decimal("19.99"),
            "placed_at": dt.datetime.now(dt.UTC),
        },
    )

    got = engine.get(orders, {"id": order_id})
    assert got is not None
    assert got["user_id"] == user_id
    # A decimal survives as a decimal. This is why the type vocabulary makes precision mandatory:
    # had this column been created as a float, the assertion below would fail by a rounding error
    # that only shows up on some values.
    assert got["total"] == decimal.Decimal("19.99")


def test_derived_table_names_are_snake_case(
    model: sde.LogicalModel, placement: sde.PlacementMap
) -> None:
    assert _table(placement, model, "User") == "user"
    assert _table(placement, model, "Order") == "order"


def test_foreign_key_column_is_named_after_the_relation(
    model: sde.LogicalModel, placement: sde.PlacementMap
) -> None:
    layout = placement.placement_of("Order").source.layout
    assert "user_id" in layout.columns["Order"]
    # And the model never said so. The convention is the library's, chosen because it is the one
    # every ORM uses and a client reading their own database should recognise what they see.
    assert not any(r.name == "user_id" for r in model.relations)


def test_range_read_over_a_timestamp(
    engine: PostgresEngine, model: sde.LogicalModel, placement: sde.PlacementMap
) -> None:
    events = _table(placement, model, "Event")
    base = dt.datetime(2026, 8, 23, 12, 0, tzinfo=dt.UTC)
    for i in range(5):
        engine.insert(
            events, {"id": uuid.uuid4(), "name": f"e{i}", "at": base + dt.timedelta(hours=i)}
        )

    window = engine.range(
        events, "at", low=base + dt.timedelta(hours=1), high=base + dt.timedelta(hours=3)
    )
    assert [row["name"] for row in window] == ["e1", "e2"]


def test_a_transaction_rolls_back_as_one_unit(
    engine: PostgresEngine, model: sde.LogicalModel, placement: sde.PlacementMap
) -> None:
    users = _table(placement, model, "User")
    before = engine.count(users)

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom), engine.transaction():
        engine.insert(users, {"id": uuid.uuid4(), "email": "a@example.com"})
        engine.insert(users, {"id": uuid.uuid4(), "email": "b@example.com"})
        raise Boom

    assert engine.count(users) == before


def test_a_transaction_commits_as_one_unit(
    engine: PostgresEngine, model: sde.LogicalModel, placement: sde.PlacementMap
) -> None:
    users = _table(placement, model, "User")
    before = engine.count(users)
    with engine.transaction():
        engine.insert(users, {"id": uuid.uuid4(), "email": "c@example.com"})
        engine.insert(users, {"id": uuid.uuid4(), "email": "d@example.com"})
    assert engine.count(users) == before + 2


def test_a_failed_write_is_reported_not_swallowed(
    engine: PostgresEngine, model: sde.LogicalModel, placement: sde.PlacementMap
) -> None:
    # The most important assertion in this file. Internal problems in this library are swallowed and
    # logged so that a bug of ours cannot take down someone's application - but a write that did not
    # happen is not an internal problem, and reporting success for it would be the worst thing this
    # library could do.
    users = _table(placement, model, "User")
    duplicate = uuid.uuid4()
    engine.insert(users, {"id": duplicate, "email": "e@example.com"})
    with pytest.raises(EngineError, match="insert into"):
        engine.insert(users, {"id": duplicate, "email": "f@example.com"})


def test_a_write_is_never_rerouted_to_another_engine(
    engine: PostgresEngine, model: sde.LogicalModel, placement: sde.PlacementMap
) -> None:
    # There is no code path that could do this, and that absence is worth a test: the resolver
    # returns the source for every write kind, unconditionally, before it consults anything.
    shapes = [s for s in sde.enumerate_shapes(model) if s.kind in {"write", "bulk_write"}]
    assert shapes
    for shape in shapes:
        chosen = sde.resolve(placement, shape)
        assert chosen is placement.placement_of(shape.group).source


def test_reads_and_writes_go_to_the_same_place_when_there_is_one_engine(
    model: sde.LogicalModel, placement: sde.PlacementMap
) -> None:
    for shape in sde.enumerate_shapes(model):
        assert sde.resolve(placement, shape).engine == "pg-test"

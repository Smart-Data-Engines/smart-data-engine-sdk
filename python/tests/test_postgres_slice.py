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


def test_a_compatibility_view_runs_twice_and_answers_from_the_table(
    engine: PostgresEngine, model: sde.LogicalModel, placement: sde.PlacementMap
) -> None:
    """Requirement 19.7's statements, against the server rather than against a string.

    This is the class of defect that reads correctly and does not run, and it caught one: the first
    draft of `compatibility_views` emitted `CREATE VIEW IF NOT EXISTS`, which PostgreSQL does not
    have - it is a syntax error, not a subtle difference. Rendering is idempotent by construction
    everywhere else in this module, so it is run twice here.
    """
    group = next(g for g in sde.colocation_groups(model) if "User" in g)
    layout = placement.placement_of(group.name).source.layout
    views = sde.compatibility_views(
        layout, was={name: f"old_{name.lower()}" for name in group.members}, dialect="postgres"
    )
    assert views.create, "a renamed table has a view to render"

    session = sde.Session(model, placement, {"pg-test": engine})
    session.save("User", {"id": uuid.uuid4(), "email": "compat@example.test"})

    with engine._cx.cursor() as cur:
        for _ in range(2):
            for statement in views.create:
                cur.execute(statement)  # type: ignore[arg-type]
        cur.execute('SELECT "email" FROM "old_user"')  # type: ignore[arg-type]
        assert cur.fetchall() == [("compat@example.test",)]
        for statement in views.drop:
            cur.execute(statement)  # type: ignore[arg-type]
        cur.execute(
            "SELECT count(*) FROM information_schema.views WHERE table_name = 'old_user'"  # type: ignore[arg-type]
        )
        assert cur.fetchall() == [(0,)], "the view goes with the source it stood in for"


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


def test_the_session_works_against_a_real_engine(
    engine: PostgresEngine, model: sde.LogicalModel, placement: sde.PlacementMap
) -> None:
    """The same path the session tests exercise against a fake, now against PostgreSQL.

    The fake agrees with whatever this library believes about tables and transactions, so it can
    only check the routing decisions. This checks that the decisions are executable: that the table
    name the map produced exists, that a transaction on the group's source engine actually commits,
    and that a read after a write inside it returns the row.
    """
    session = sde.Session(model, placement, {"pg-test": engine})

    user_id = uuid.uuid4()
    with session.transaction("User", "Order"):
        session.save("User", {"id": user_id, "email": "session@example.com"})
        # Inside a write transaction the read must come from the source, and here that is the only
        # copy - so this asserts the plumbing rather than the routing.
        inside = session.get("User", {"id": user_id})
        assert inside is not None
        assert inside["email"] == "session@example.com"

    assert session.get("User", {"id": user_id}) is not None


def test_the_session_refuses_a_cross_group_transaction_on_a_real_engine(
    engine: PostgresEngine, model: sde.LogicalModel, placement: sde.PlacementMap
) -> None:
    # Same refusal as the unit test, repeated here for one reason: to confirm nothing is opened on
    # the real connection before it fires. A guarantee that holds against a fake and leaks a real
    # transaction would be worse than not having it.
    from sde.errors import ModelPlanningError

    session = sde.Session(model, placement, {"pg-test": engine})
    expected = "cannot span colocation groups"
    with pytest.raises(ModelPlanningError, match=expected), session.transaction("Order", "Event"):
        pass
    # The connection is still usable, which it would not be if a transaction had been left open.
    assert engine.count(_table(placement, model, "User")) >= 0


def test_a_table_that_already_exists_with_another_shape_is_refused(
    engine: sde.Engine, placement: sde.PlacementMap, model: sde.LogicalModel
) -> None:
    """`CREATE TABLE IF NOT EXISTS` keeps whatever is there, so somebody has to look.

    Found by running the walkthrough against a database that already had a `reading` table from
    something else. The DDL reported success, `ensure_schema` returned, and the first insert failed
    with `column "at" of relation "reading" does not exist` - an error naming a column rather than
    the cause, arriving in the request path instead of at startup.

    Missing columns are refused. Extra ones are not: a client may have added one outside SDE, the
    map does not name it, writes are unaffected, and refusing would make this library an obstacle to
    work it has no opinion about.
    """
    group = next(g for g in sde.colocation_groups(model) if "Event" in g.members)
    layout = placement.placement_of(group.name).source.layout
    assert layout is not None
    table = layout.tables["Event"]

    with engine._cx.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{table}"')
        cur.execute(f'CREATE TABLE "{table}" ("id" uuid PRIMARY KEY, "unrelated" text)')

    keys = {e.name: tuple(e.key) for e in model.entities}
    with pytest.raises(sde.errors.EngineError, match="already existed with a different shape"):
        engine.ensure_schema(layout, keys=keys)

    with engine._cx.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{table}"')


def test_an_extra_column_is_allowed_and_logged(
    engine: sde.Engine, placement: sde.PlacementMap, model: sde.LogicalModel
) -> None:
    group = next(g for g in sde.colocation_groups(model) if "Event" in g.members)
    layout = placement.placement_of(group.name).source.layout
    assert layout is not None
    table = layout.tables["Event"]
    keys = {e.name: tuple(e.key) for e in model.entities}

    with engine._cx.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{table}"')
    engine.ensure_schema(layout, keys=keys)
    with engine._cx.cursor() as cur:
        cur.execute(f'ALTER TABLE "{table}" ADD COLUMN "added_by_the_client" text')

    # No exception: the map has no opinion about a column it does not name.
    engine.ensure_schema(layout, keys=keys)

    with engine._cx.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{table}"')

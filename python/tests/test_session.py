"""Session routing and the transaction boundary, against a fake engine.

No database here on purpose. The guarantee this file exists for - that a transaction cannot span two
colocation groups - is a property of the model and the map, so it must hold before any connection is
opened. Requiring PostgreSQL to test it would mean the test could be skipped in an environment where
the guarantee still needs to hold.

The PostgreSQL slice next door checks the other half: that what the session routes actually works
against a real engine.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import pytest

import sde
from sde.errors import EngineError, ModelPlanningError


class FakeEngine:
    """Records what it was asked to do. Satisfies the Engine protocol without importing it."""

    dialect = "fake"

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.schema_calls = 0
        self.transactions = 0
        self.rolled_back = 0

    def ensure_schema(self, layout: Any, *, keys: Mapping[str, Any]) -> None:
        self.schema_calls += 1
        for table in layout.tables.values():
            self.rows.setdefault(table, [])

    def insert(self, table: str, values: Mapping[str, Any]) -> None:
        self.rows.setdefault(table, []).append(dict(values))

    def get(self, table: str, key: Mapping[str, Any]) -> dict[str, Any] | None:
        for row in self.rows.get(table, []):
            if all(row.get(k) == v for k, v in key.items()):
                return row
        return None

    @contextmanager
    def transaction(self) -> Iterator[FakeEngine]:
        self.transactions += 1
        try:
            yield self
        except Exception:
            self.rolled_back += 1
            raise


@pytest.fixture(autouse=True)
def _isolate() -> None:
    sde.clear_registry()


def _two_group_model() -> sde.LogicalModel:
    @sde.entity
    class User:
        id: uuid.UUID
        email: str

    @sde.entity
    class Order:
        id: uuid.UUID
        user: sde.Ref[User]
        note: str

    @sde.entity
    class Event:
        id: uuid.UUID
        name: str

    return sde.build_model(User, Order, Event)


def _placement(model: sde.LogicalModel, engine: str = "fake") -> sde.PlacementMap:
    raw: dict[str, Any] = {
        "contract": sde.CONTRACT,
        "model_version": model.version,
        "map_version": 1,
        "groups": {
            group.name: {
                "source": {
                    "id": f"{group.name}@{engine}",
                    "engine": engine,
                    "layout": {"auto": True},
                }
            }
            for group in sde.colocation_groups(model)
        },
    }
    return sde.load_map(raw, model=model)


def _session(model: sde.LogicalModel) -> tuple[sde.Session, FakeEngine]:
    engine = FakeEngine()
    return sde.Session(model, _placement(model), {"fake": engine}), engine


def test_a_transaction_across_two_groups_is_refused_before_anything_opens() -> None:
    # The point of this test is the *before*. A commit that fails after half the work is a
    # production incident; a refusal at the point the transaction is requested is a test failure,
    # and this is a mistake that should always be the second kind.
    model = _two_group_model()
    session, engine = _session(model)

    with pytest.raises(ModelPlanningError) as caught, session.transaction("Order", "Event"):
        pass

    assert engine.transactions == 0
    message = str(caught.value)
    # It has to name the groups and the fix. "Not supported" would be true and useless: the reader
    # needs to know that declaring atomicity turns this into a placement constraint.
    assert "Event" in message and "Order" in message
    assert "atomic_with" in message


def test_a_transaction_within_one_group_is_allowed() -> None:
    model = _two_group_model()
    session, engine = _session(model)
    with session.transaction("Order", "User"):
        session.save("User", {"id": uuid.uuid4(), "email": "a@example.com"})
    assert engine.transactions == 1
    assert engine.rolled_back == 0


def test_declaring_atomicity_is_what_makes_a_wide_transaction_possible() -> None:
    # The same two entities that were refused above, now declared atomic - so they share a group, so
    # the transaction is legal. This is the sentence the error message promises, executed.
    @sde.entity
    class Ledger:
        id: uuid.UUID
        balance: int

    @sde.entity
    class Payment:
        id: uuid.UUID
        amount: int

        class Meta:
            atomic_with = ["Ledger"]

    model = sde.build_model(Ledger, Payment)
    session, engine = _session(model)
    with session.transaction("Ledger", "Payment"):
        session.save("Ledger", {"id": uuid.uuid4(), "balance": 0})
    assert engine.transactions == 1


def test_a_transaction_with_no_entities_is_refused_on_a_multi_group_model() -> None:
    # Not a convenience for small models: silently covering one group of two would give the caller a
    # transaction narrower than the one they asked for, which is the failure mode worth preventing.
    model = _two_group_model()
    session, _ = _session(model)
    expected = "cannot span colocation groups"
    with pytest.raises(ModelPlanningError, match=expected), session.transaction():
        pass


def test_a_transaction_with_no_entities_is_fine_on_a_single_group_model() -> None:
    @sde.entity
    class Alone:
        id: uuid.UUID
        v: str

    model = sde.build_model(Alone)
    session, engine = _session(model)
    with session.transaction():
        session.save("Alone", {"id": uuid.uuid4(), "v": "x"})
    assert engine.transactions == 1


def test_reads_inside_a_write_transaction_go_to_the_source() -> None:
    # A derived copy is behind by design, so it cannot show the write the caller just made. The
    # session has to carry that state into the router, which is easy to forget and invisible until a
    # client reads their own write and does not find it.
    model = _two_group_model()
    engine = FakeEngine()
    raw: dict[str, Any] = {
        "contract": sde.CONTRACT,
        "model_version": model.version,
        "map_version": 1,
        "groups": {
            g.name: {
                "source": {"id": f"{g.name}@src", "engine": "fake", "layout": {"auto": True}},
                "derived": [
                    {
                        "id": f"{g.name}@derived",
                        "engine": "fake",
                        # An explicit layout with different table names. Both copies live in the
                        # same fake engine here, and the map loader now refuses two materialisations
                        # that would name the same tables - which it should, because that is a copy
                        # that is silently the original.
                        "layout": {
                            "tables": {m: m.lower() + "_derived" for m in g.members},
                            "columns": {},
                        },
                        "lag_budget_ms": 1000,
                    }
                ],
            }
            for g in sde.colocation_groups(model)
        },
    }
    shapes = {(s.entity, s.kind): s for s in sde.enumerate_shapes(model)}
    raw["routing"] = {shapes[("User", "point_read")].id: "Order@derived"}
    placement = sde.load_map(raw, model=model)
    session = sde.Session(model, placement, {"fake": engine})
    session.ensure_schema()

    key = uuid.uuid4()
    # Outside a transaction the routing table wins and the read goes to the derived copy, which has
    # nothing in it.
    session.save("User", {"id": key, "email": "a@example.com"})
    assert session.get("User", {"id": key}) is None

    # Inside a write transaction it must come from the source.
    with session.transaction("User", "Order"):
        assert session.get("User", {"id": key}) is not None

    # And asking for freshness has the same effect without a transaction.
    assert session.get("User", {"id": key}, fresh=True) is not None


def test_a_partial_key_is_refused_rather_than_silently_widened() -> None:
    @sde.entity
    class Tenanted:
        tenant: uuid.UUID
        id: uuid.UUID
        v: str

        class Meta:
            key = ["tenant", "id"]

    model = sde.build_model(Tenanted)
    session, _ = _session(model)
    with pytest.raises(ModelPlanningError, match="needs exactly its key"):
        session.get("Tenanted", {"id": uuid.uuid4()})


def test_an_operation_the_model_does_not_admit_is_a_modelling_error() -> None:
    model = _two_group_model()
    session, _ = _session(model)
    with pytest.raises(ModelPlanningError, match="not in this model"):
        session.group_of("Ghost")


def test_a_map_naming_an_engine_we_have_no_adapter_for_is_refused_at_construction() -> None:
    # Refused when the session is built, not when the first operation reaches that engine. The
    # difference is between a startup failure and an outage on one code path.
    model = _two_group_model()
    placement = _placement(model, engine="clickhouse-1")
    with pytest.raises(EngineError, match="not supplied"):
        sde.Session(model, placement, {"fake": FakeEngine()})


def test_schema_is_created_for_every_materialisation() -> None:
    model = _two_group_model()
    session, engine = _session(model)
    session.ensure_schema()
    # Two groups, one materialisation each.
    assert engine.schema_calls == 2
    assert set(engine.rows) == {"user", "order", "event"}

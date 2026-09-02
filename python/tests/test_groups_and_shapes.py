"""Colocation groups and operation shapes."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

import sde


@pytest.fixture(autouse=True)
def _isolate() -> None:
    sde.clear_registry()


def _shop() -> sde.LogicalModel:
    """The model that demonstrates why relation-as-edge is right rather than ruinous."""

    @sde.entity
    class User:
        id: uuid.UUID
        email: str

    @sde.entity
    class Order:
        id: uuid.UUID
        user: sde.Ref[User]
        placed_at: dt.datetime

    @sde.entity
    class OrderLine:
        id: uuid.UUID
        order: sde.Ref[Order]
        quantity: int

    @sde.entity
    class Event:
        id: uuid.UUID
        name: str
        at: dt.datetime

    return sde.build_model(User, Order, OrderLine, Event)


def test_the_transactional_core_and_the_event_stream_separate() -> None:
    # This is the product's thesis in one assertion. Everything joined ends up in one group and will
    # be placed in a row store; the event stream nobody joins ends up on its own and is free to go
    # to a column store. Those unjoined entities are exactly the ones that were in the wrong engine.
    groups = sde.colocation_groups(_shop())
    assert [(g.name, g.members) for g in groups] == [
        ("Event", ("Event",)),
        ("Order", ("Order", "OrderLine", "User")),
    ]


def test_atomicity_merges_groups_that_relations_would_not() -> None:
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

    groups = sde.colocation_groups(sde.build_model(Ledger, Payment))
    # No relation between them at all, yet they must share an engine: the declaration says they
    # commit together, and one engine's transaction is the only thing that can deliver that.
    assert [g.members for g in groups] == [("Ledger", "Payment")]


def test_group_naming_and_ordering_are_deterministic() -> None:
    first = sde.colocation_groups(_shop())
    sde.clear_registry()
    second = sde.colocation_groups(_shop())
    assert [(g.name, g.members) for g in first] == [(g.name, g.members) for g in second]


def test_every_entity_is_in_exactly_one_group() -> None:
    model = _shop()
    groups = sde.colocation_groups(model)
    seen = [member for g in groups for member in g.members]
    assert sorted(seen) == sorted(e.name for e in model.entities)
    assert len(seen) == len(set(seen))


def test_shapes_carry_no_values_and_only_the_declared_keys() -> None:
    # A shape is assembled from the structure of an operation, so there is nowhere for a value to
    # come from. This asserts the encoding surface rather than trying to prove a negative: any new
    # key here would be a new opportunity to leak something.
    for shape in sde.enumerate_shapes(_shop()):
        assert set(shape.as_ir()) == {"group", "kind", "entity", "fields", "target"}


def test_shape_kinds_are_from_the_closed_set() -> None:
    for shape in sde.enumerate_shapes(_shop()):
        assert shape.kind in sde.SHAPE_KINDS


def test_range_reads_are_enumerated_only_for_ordered_types() -> None:
    @sde.entity
    class Reading:
        id: uuid.UUID
        sensor: str
        at: dt.datetime
        value: float

    shapes = sde.enumerate_shapes(sde.build_model(Reading))
    ranged = {s.fields[0] for s in shapes if s.kind == "range_read"}
    # A range over a timestamp or a float is a real query. A range over a string or a uuid is legal
    # SQL and almost never what anybody means, so it is not enumerated - if telemetry ever shows
    # one, that is a reason to revisit the list rather than to widen it now.
    assert ranged == {"at", "value"}


def test_relation_walk_is_enumerated_per_relation() -> None:
    shapes = sde.enumerate_shapes(_shop())
    walks = {(s.entity, s.fields[0], s.target) for s in shapes if s.kind == "relation_walk"}
    assert walks == {("Order", "user", "User"), ("OrderLine", "order", "Order")}


def test_shape_ids_are_stable_and_distinct() -> None:
    shapes = sde.enumerate_shapes(_shop())
    ids = [s.id for s in shapes]
    assert len(set(ids)) == len(ids)
    sde.clear_registry()
    assert [s.id for s in sde.enumerate_shapes(_shop())] == ids


def test_shape_id_depends_on_the_group_it_lands_in() -> None:
    # Two models with the same entity but different colocation produce different shape ids, which is
    # correct: the routing table is keyed by shape, and the same read against a differently placed
    # group is a different routing decision.
    @sde.entity
    class Alone:
        id: uuid.UUID
        v: str

    solo = sde.enumerate_shapes(sde.build_model(Alone))
    solo_point = next(s for s in solo if s.kind == "point_read")

    sde.clear_registry()

    @sde.entity
    class Anchor:
        id: uuid.UUID
        v: str

    @sde.entity
    class Alone2:
        id: uuid.UUID
        v: str
        a: sde.Ref[Anchor]

    joined = sde.enumerate_shapes(sde.build_model(Anchor, Alone2))
    joined_point = next(s for s in joined if s.kind == "point_read" and s.entity == "Alone2")
    assert solo_point.id != joined_point.id


def test_the_write_kinds_are_defined_once_in_the_package() -> None:
    """Four copies of this set existed and two were inside this package.

    One in `routing`, deciding whether an operation goes to the source; one in `telemetry`,
    deciding whether an operation counts as a write in the features a placement is scored on. Two
    copies of a set in one process is how the same operation becomes a write for routing and a read
    for scoring, and nothing would have raised - the two would simply disagree about what the
    client is doing.

    A static test rather than a behavioural one, because the behaviour of two identical copies is
    identical: the defect is only visible in the source until the day somebody edits one.
    """
    import re
    from pathlib import Path

    package = Path(sde.__file__).resolve().parent
    literal = re.compile(r"""frozenset\(\{["']write["']""")
    offenders = [
        path.name
        for path in sorted(package.rglob("*.py"))
        if literal.search(path.read_text(encoding="utf-8")) and path.name != "shapes.py"
    ]
    assert not offenders, f"the write kinds are spelled out again in {offenders}"
    assert frozenset({"write", "bulk_write"}) == sde.WRITE_KINDS
    assert set(sde.SHAPE_KINDS) >= sde.WRITE_KINDS, "a write kind that is not a shape kind"

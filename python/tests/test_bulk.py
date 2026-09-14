"""Application batches preserve their call boundary, values and source-commit semantics."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

import sde
from sde.bulk import batch_columns, snapshot_rows
from sde.testing.memory import MemoryEngine, Recorded


def fixture(
    *,
    fanout: bool = True,
    hashed: bool = False,
    json_field: bool = False,
    source_bulk: bool = True,
    copy_bulk: bool = True,
) -> tuple[Any, ...]:
    sde.clear_registry()
    if json_field:

        @sde.entity
        class Event:
            id: int
            value: dict[str, Any]
    else:

        @sde.entity
        class Event:
            id: int
            value: str

    model = sde.build_model(Event)
    names = None
    if hashed:
        model, names = sde.hash_identifiers(model, b"b" * 32)
    group = sde.colocation_groups(model)[0].name
    entity = model.entities[0].name
    layout = sde.default_layout(model, sde.colocation_groups(model)[0], dialect="postgres")
    raw = {
        "contract": 3,
        "model_version": model.version,
        "map_version": 1,
        "groups": {
            group: {
                "source": {
                    "id": "source",
                    "engine": "source",
                    "layout": {
                        "tables": dict(layout.tables),
                        "columns": {k: dict(v) for k, v in layout.columns.items()},
                    },
                },
                "derived": [
                    {
                        "id": "copy",
                        "engine": "copy",
                        "layout": {
                            "tables": dict(layout.tables),
                            "columns": {k: dict(v) for k, v in layout.columns.items()},
                        },
                        "lag_budget_ms": 30000,
                    }
                ],
                "also_write": ["copy"] if fanout else [],
            }
        },
    }
    journal = Recorded()
    source = MemoryEngine(name="source", journal=journal, can_bulk_write=source_bulk)
    copy = MemoryEngine(name="copy", journal=journal, can_bulk_write=copy_bulk)
    recorder = sde.Recorder(model.version)
    session = sde.Session(
        model,
        sde.load_map(raw, model=model),
        {"source": source, "copy": copy},
        recorder=recorder,
        names=names,
    )
    return session, source, copy, journal, recorder, layout.table_for(entity)


def rows() -> list[dict[str, Any]]:
    return [{"id": 1, "value": "one"}, {"value": "two", "id": 2}]


def test_bulk_shape_routes_to_source_then_one_batch_per_copy() -> None:
    session, source, copy, journal, recorder, table = fixture()
    session.save_many("Event", rows())
    assert journal.calls == [
        {"engine": name, "call": "insert_many", "table": table, "rows": 2}
        for name in ("source", "copy")
    ]
    assert source.tables[table] == copy.tables[table] == rows()
    window = recorder.roll()
    assert window is not None
    shape = next(s for s in sde.enumerate_shapes(session.model) if s.kind == "bulk_write")
    stats = next(stat for stat in window.shapes if stat.shape_id == shape.id)
    assert stats.calls == 1 and stats.rows == 2 and stats.errors == 0


@pytest.mark.parametrize("hashed", [False, True])
def test_nested_json_snapshot_survives_caller_changes_before_outer_commit(hashed: bool) -> None:
    session, source, copy, journal, _, table = fixture(hashed=hashed, json_field=True)
    payload = [{"id": 1, "value": {"nested": ["original"]}}]
    with session.transaction("Event"):
        with session.transaction("Event"):
            session.save_many("Event", payload)
        payload[0]["value"]["nested"][0] = "changed"
        payload[0]["id"] = 99
        payload.clear()
        assert copy.tables == {}
    assert source.tables[table] == copy.tables[table]
    assert session.get("Event", {"id": 1}) == {"id": 1, "value": {"nested": ["original"]}}
    assert [c["call"] for c in journal.calls].count("insert_many") == 2


def test_inner_rollback_discards_only_its_batch_and_outer_rollback_discards_all() -> None:
    session, source, copy, _, _, table = fixture()
    with session.transaction("Event"):
        session.save_many("Event", [rows()[0]])
        with pytest.raises(ValueError), session.transaction("Event"):
            session.save_many("Event", [rows()[1]])
            raise ValueError("rollback inner")
        assert copy.tables == {}
    assert source.tables[table] == copy.tables[table] == [rows()[0]]
    with pytest.raises(ValueError), session.transaction("Event"):
        with session.transaction("Event"):
            session.save_many("Event", [rows()[1]])
        raise ValueError("rollback outer")
    assert source.tables[table] == copy.tables[table] == [rows()[0]]


@pytest.mark.parametrize("failed_side", ["source", "copy"])
def test_source_is_never_retried_and_copy_failure_does_not_fail_source(failed_side: str) -> None:
    session, source, copy, journal, recorder, table = fixture()
    (source if failed_side == "source" else copy)._fail_inserts[table] = 1
    if failed_side == "source":
        with pytest.raises(sde.EngineError):
            session.save_many("Event", rows())
        assert source.tables == copy.tables == {}
    else:
        session.save_many("Event", rows())
        assert source.tables[table] == rows() and copy.tables == {}
    assert [c["engine"] for c in journal.calls] == (
        ["source"] if failed_side == "source" else ["source", "copy"]
    )
    window = recorder.roll()
    assert window is not None
    stats = window.shapes[0]
    assert stats.rows == 2 and stats.errors == int(failed_side == "source")
    copies = list(window.copies("Event"))
    assert len(copies) == int(failed_side == "copy")
    if copies:
        assert copies[0].as_record()["failures"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        "rows",
        [None],
        [{}],
        [{"id": 1}],
        [{"id": 1, "value": "v", "unknown": 9}],
        [{"id": 1, "value": "v"}, {"id": 2}],
        rows() * 501,
        [{"id": 1, "value": object()}],
    ],
)
def test_bad_batch_refuses_without_any_io(payload: Any) -> None:
    session, _, _, journal, recorder, _ = fixture()
    with pytest.raises(sde.BulkWriteRefused):
        session.save_many("Event", payload)
    assert journal.calls == [] and recorder.roll() is None


@pytest.mark.parametrize("side", ["source", "copy"])
def test_capability_preflight_covers_the_source_and_every_copy(side: str) -> None:
    session, _, _, journal, _, _ = fixture(source_bulk=side != "source", copy_bulk=side != "copy")
    with pytest.raises(sde.BulkWriteRefused, match="support bulk"):
        session.save_many("Event", rows())
    assert journal.calls == []


def test_empty_batch_still_checks_entity_and_session_lifetime() -> None:
    session, _, _, journal, recorder, _ = fixture(source_bulk=False, copy_bulk=False)
    session.save_many("Event", [])
    with pytest.raises(sde.ModelPlanningError):
        session.save_many("Missing", [])
    session.close()
    with pytest.raises(sde.ResourceClosed):
        session.save_many("Event", [])
    assert journal.calls == [] and recorder.roll() is None


def test_bounds_include_the_generation_and_do_not_consume_a_generator() -> None:
    row = {f"f{i}": i for i in range(60)}
    batch = [row] * 1000
    assert len(batch_columns(batch)) == 60
    with pytest.raises(sde.BulkWriteRefused, match="60000"):
        batch_columns(batch, extra_columns=1)
    assert len(batch_columns([{**row, "extra": 1}] * 983)) == 61
    with pytest.raises(sde.BulkWriteRefused, match="60000"):
        batch_columns([{**row, "extra": 1}] * 984)

    def stream() -> Any:
        raise AssertionError("stream consumed")
        yield row

    with pytest.raises(sde.BulkWriteRefused):
        batch_columns(stream())


def test_snapshot_refuses_cycles_without_recursing_into_a_write() -> None:
    session, _, _, journal, _, _ = fixture(json_field=True)
    value: dict[str, Any] = {}
    value["cycle"] = value
    with pytest.raises(sde.BulkWriteRefused, match="cycles"):
        session.save_many("Event", [{"id": 1, "value": value}])
    assert journal.calls == []
    buffer = bytearray(b"abc")
    original: Mapping[str, Any] = {"id": 1, "value": buffer}
    snapshot = snapshot_rows([original])
    buffer[0] = 0
    assert snapshot[0]["value"] == b"abc"


@pytest.mark.parametrize("hashed", [False, True])
def test_relation_columns_are_valid_logical_input_and_round_trip_when_hashed(hashed: bool) -> None:
    sde.clear_registry()

    @sde.entity
    class Parent:
        id: int

    @sde.entity
    class Child:
        id: int
        parent: sde.Ref[Parent]

    model = sde.build_model(Parent, Child)
    names = None
    if hashed:
        model, names = sde.hash_identifiers(model, b"r" * 32)
    group = sde.colocation_groups(model)[0].name
    placement = sde.load_map(
        {
            "contract": 3,
            "model_version": model.version,
            "map_version": 1,
            "groups": {
                group: {"source": {"id": "source", "engine": "db", "layout": {"auto": True}}}
            },
        },
        model=model,
    )
    engine = MemoryEngine()
    session = sde.Session(model, placement, {"db": engine}, names=names)
    session.save_many("Parent", [{"id": 1}])
    session.save_many("Child", [{"id": 2, "parent_id": 1}])
    assert session.get("Child", {"id": 2}) == {"id": 2, "parent_id": 1}
    columns = sde.group_columns(model, sde.colocation_groups(model)[0])
    for entity, table in placement.groups[group].source.layout.tables.items():
        assert set(engine.tables[table][0]) == set(columns[entity])


def test_empty_batch_obeys_transaction_group_and_other_thread_cannot_enter() -> None:
    from concurrent.futures import ThreadPoolExecutor

    sde.clear_registry()

    @sde.entity
    class A:
        id: int

    @sde.entity
    class B:
        id: int

    model = sde.build_model(A, B)
    placement = sde.load_map(
        {
            "contract": 3,
            "model_version": model.version,
            "map_version": 1,
            "groups": {
                g.name: {"source": {"id": g.name, "engine": "db", "layout": {"auto": True}}}
                for g in sde.colocation_groups(model)
            },
        },
        model=model,
    )
    engine = MemoryEngine()
    session = sde.Session(model, placement, {"db": engine})
    with ThreadPoolExecutor(max_workers=1) as pool, session.transaction("A"):
        with pytest.raises(sde.ModelPlanningError):
            session.save_many("B", [])
        with pytest.raises(sde.ResourceBusy):
            pool.submit(session.save_many, "A", [{"id": 1}]).result(timeout=5)
    assert engine.tables == {}


def test_unknown_hashed_batch_field_has_the_shared_local_refusal() -> None:
    session, _, _, journal, _, _ = fixture(hashed=True)
    with pytest.raises(sde.BulkWriteRefused):
        session.save_many("Event", [{"id": 1, "value": "v", "unknown": 9}])
    assert journal.calls == []

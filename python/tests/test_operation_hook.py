"""The session's operation hook: a record a completed operation, nothing the operation carried.

Engine-free: the adapter is the in-memory double with the read protocols added, so every method of
the session is reached without a server. The sentinel test is the one that matters most: a value
put in every row, key and filter must not appear in anything the hook was given - and the control
is the same value coming back in the rows the operations return, so the test cannot pass by
looking at nothing.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import sde
from sde.errors import EngineError, ModelPlanningError
from sde.hook import FIELDS, OPERATIONS, OperationRecord, outcome_of
from sde.query import ReadColumn, ReadPlan
from sde.testing.memory import MemoryEngine

SENTINEL = "sentinel-7f3a9c41-in-every-value"


class Reader(MemoryEngine):
    """The in-memory double, answering reads from its own tables."""

    def select_rows(self, table: str, plan: ReadPlan) -> list[dict[str, Any]]:
        return [dict(row) for row in self.tables.get(table, [])][: plan.limit + 1]

    def count_rows(self, table: str, plan: ReadPlan) -> int:
        return len(self.tables.get(table, []))

    def summarize_rows(self, table: str, plan: ReadPlan, column: ReadColumn) -> dict[str, Any]:
        values = [row[column.name] for row in self.tables.get(table, [])]
        return {
            "sde_count": len(values),
            "sde_present": len(values),
            "sde_min": str(min(values)),
            "sde_max": str(max(values)),
            "sde_total": str(sum(values)),
        }


@pytest.fixture
def records() -> Iterator[list[OperationRecord]]:
    sde.reset_internal_failures()
    yield []
    sde.reset_internal_failures()


def session(records: list[OperationRecord], engine: MemoryEngine | None = None) -> sde.Session:
    model = sde.testing.loader.model_from_neutral(
        {
            "entities": [
                {
                    "name": "Note",
                    "fields": [
                        {"name": "body", "type": "string"},
                        {"name": "id", "type": "string"},
                        {"name": "size", "type": "int64"},
                    ],
                    "key": ["id"],
                },
                {
                    "name": "Tag",
                    "fields": [
                        {"name": "id", "type": "string"},
                        {"name": "note", "type": "string"},
                    ],
                    "key": ["id"],
                },
            ],
            "relations": [],
            "atomic": [["Note", "Tag"]],
        }
    )
    placement = sde.load_map(
        {
            "contract": 3,
            "model_version": model.version,
            "map_version": 1,
            "groups": {"Note": {"source": {"id": "notes@pg", "engine": "pg-main",
                                           "layout": {"auto": True}}}},
        },
        model=model,
    )  # fmt: skip
    return sde.Session(
        model, placement, {"pg-main": engine or Reader(name="pg-main")}, hook=records.append
    )


def test_each_method_is_one_record_with_where_it_went(records: list[OperationRecord]) -> None:
    notes = session(records)
    notes.save("Note", {"id": "a", "body": "one", "size": 1})
    notes.save_many("Note", [{"id": "b", "body": "two", "size": 2},
                             {"id": "c", "body": "three", "size": 3}])  # fmt: skip
    assert notes.get("Note", {"id": "a"}) is not None
    notes.scan("Note", limit=10)
    notes.count("Note")
    notes.summarize("Note", "size")
    assert [(r.operation, r.rows) for r in records] == [
        ("save", 1), ("save_many", 2), ("get", 1), ("scan", 3), ("count", 1), ("summarize", 1),
    ]  # fmt: skip
    for record in records:
        assert (record.entity, record.group, record.engine) == ("Note", "Note", "pg-main")
        assert (record.dialect, record.materialization) == ("postgres", "notes@pg")
        assert record.outcome == "ok"
        assert record.duration_ns >= 0
        assert record.operation in OPERATIONS
    shapes = {s.id for s in sde.enumerate_shapes(notes._model)}
    assert {r.shape for r in records} <= shapes


def test_a_transaction_is_a_record_of_its_own_beside_its_operations(
    records: list[OperationRecord],
) -> None:
    notes = session(records)
    with notes.transaction("Note", "Tag"):
        notes.save("Note", {"id": "a", "body": "one", "size": 1})
        notes.save("Tag", {"id": "t", "note": "a"})
    assert [(r.operation, r.entity, r.outcome) for r in records] == [
        ("save", "Note", "ok"), ("save", "Tag", "ok"), ("transaction", "Note", "ok"),
    ]  # fmt: skip
    assert records[-1].shape == ""
    assert records[-1].materialization == "notes@pg"


def test_a_transaction_that_raises_says_what_ended_it(records: list[OperationRecord]) -> None:
    notes = session(records)
    with pytest.raises(ModelPlanningError), notes.transaction("Note"):
        raise ModelPlanningError("the application gave up")
    with pytest.raises(ValueError), notes.transaction("Note"):
        raise ValueError("not ours")
    assert [r.outcome for r in records] == ["ModelPlanningError", "unexpected"]


def test_an_engine_error_is_its_class_and_never_its_message(
    records: list[OperationRecord],
) -> None:
    class Refusing(Reader):
        def insert(self, table: str, values: Any) -> None:
            raise EngineError(f"insert into {table} failed: Key (id)=({SENTINEL}) already exists")

    notes = session(records, Refusing(name="pg-main"))
    with pytest.raises(EngineError):
        notes.save("Note", {"id": SENTINEL, "body": "x", "size": 1})
    [record] = records
    assert (record.operation, record.outcome, record.rows) == ("save", "EngineError", 1)
    assert SENTINEL not in repr(record)


def test_a_hook_that_raises_changes_nothing_and_is_counted(
    records: list[OperationRecord],
) -> None:
    def broken(record: OperationRecord) -> None:
        raise RuntimeError("the application's exporter is down")

    model_session = session(records)
    model_session._hook = broken
    model_session.save("Note", {"id": "a", "body": "one", "size": 1})
    assert model_session.get("Note", {"id": "a"}) == {"id": "a", "body": "one", "size": 1}
    assert sde.internal_failures() == {"session.hook": 2}


def test_nothing_an_operation_carried_reaches_the_hook(records: list[OperationRecord]) -> None:
    notes = session(records)
    row = {"id": SENTINEL, "body": SENTINEL, "size": 7}
    notes.save("Note", row)
    notes.save_many("Note", [{"id": SENTINEL + "-2", "body": SENTINEL, "size": 8}])
    with notes.transaction("Note", "Tag"):
        notes.save("Tag", {"id": SENTINEL + "-t", "note": SENTINEL})
    returned = [notes.get("Note", {"id": SENTINEL})]
    returned += list(notes.scan("Note", where={"body": SENTINEL}, limit=10).rows)
    notes.count("Note", where={"body": SENTINEL})
    notes.summarize("Note", "size", where={"body": SENTINEL})
    # The control: the value is there to be found, in what the operations return.
    assert any(SENTINEL in repr(item) for item in returned)
    assert len(records) == 8
    for record in records:
        assert SENTINEL not in repr(dataclasses.astuple(record)), record


def test_the_record_has_the_contracts_fields_in_order() -> None:
    assert FIELDS == (
        "operation", "entity", "group", "shape", "engine", "dialect", "materialization",
        "duration_ns", "rows", "outcome",
    )  # fmt: skip
    assert outcome_of(None) == "ok"
    assert outcome_of(EngineError("x")) == "EngineError"
    assert outcome_of(KeyError("x")) == "unexpected"


def test_the_record_is_one_record_in_three_languages() -> None:
    """TypeScript's and C++'s operation names and fields are Python's, read from their sources.

    Not a conformance family: the record is not bytes anything signs or compares, and a hook is an
    API of each library. What has to hold across them is the spelling an exporter keys on, so this
    reads it where each library keeps it.
    """
    root = Path(__file__).resolve().parents[2]
    typescript = (root / "typescript" / "src" / "hook.ts").read_text(encoding="utf-8")
    cpp = (root / "cpp" / "include" / "sde" / "hook.hpp").read_text(encoding="utf-8")

    def listed(text: str, name: str) -> tuple[str, ...]:
        found = re.search(name + r"[^=]*=\s*[\[{](.*?)[\]}]", text, re.DOTALL)
        assert found is not None, name
        return tuple(re.findall(r"['\"]([a-zA-Z_]+)['\"]", found.group(1)))

    def snake(name: str) -> str:
        return re.sub(r"(?<!^)([A-Z])", lambda m: "_" + m.group(1).lower(), name)

    assert listed(typescript, "export const OPERATIONS") == OPERATIONS
    assert tuple(snake(f) for f in listed(typescript, "export const FIELDS")) == FIELDS
    assert listed(cpp, r"inline constexpr std::string_view OPERATIONS\[\]") == OPERATIONS
    assert listed(cpp, r"inline constexpr std::string_view OPERATION_FIELDS\[\]") == FIELDS

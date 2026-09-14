"""Session policy for logical reads: routing, copy projection, lifetime and private telemetry."""

from __future__ import annotations

from typing import Any

import pytest

import sde
from sde.query import ReadPlan
from sde.testing.memory import MemoryEngine


class Reader(MemoryEngine):
    def __init__(self, name: str, calls: list[str]) -> None:
        super().__init__(name=name)
        self.calls = calls

    def select_rows(self, table: str, plan: ReadPlan) -> list[dict[str, Any]]:
        self.calls.append(self.name + ".select")
        return [{"id": 2, "label": self.name}, {"id": 3, "label": "sentinel"}]

    def count_rows(self, table: str, plan: ReadPlan) -> int:
        self.calls.append(self.name + ".count")
        return 9_007_199_254_740_993


def fixture(*, partial: bool = False, query_capability: bool = True) -> tuple[Any, ...]:
    sde.clear_registry()

    @sde.entity
    class Event:
        id: int
        label: str

    model = sde.build_model(Event)
    shapes = {shape.kind: shape.id for shape in sde.enumerate_shapes(model)}
    full = {"tables": {"Event": "event"}, "columns": {"Event": {"id": "bigint", "label": "text"}}}
    copied = {
        "tables": {"Event": "copy"},
        "columns": {"Event": {"id": "bigint"} if partial else full["columns"]["Event"]},
    }
    placement = sde.load_map(
        {
            "contract": 3,
            "model_version": model.version,
            "map_version": 1,
            "groups": {
                "Event": {
                    "source": {"id": "source", "engine": "source", "layout": full},
                    "derived": [
                        {"id": "copy", "engine": "copy", "layout": copied, "lag_budget_ms": 30000}
                    ],
                }
            },
            "routing": {shapes["full_scan"]: "copy", shapes["aggregate"]: "copy"},
        },
        model=model,
    )
    calls: list[str] = []
    source = Reader("source", calls)
    copy = Reader("copy", calls) if query_capability else MemoryEngine()
    recorder = sde.Recorder(model.version)
    session = sde.Session(model, placement, {"source": source, "copy": copy}, recorder=recorder)
    return session, calls, recorder


def test_page_uses_routed_copy_but_fresh_and_transaction_use_source() -> None:
    session, calls, _ = fixture()
    page = session.scan("Event", limit=1)
    assert page.rows == ({"id": 2, "label": "copy"},)
    assert page.next_after == {"id": 2}
    page.rows[0]["id"] = 99
    assert page.next_after == {"id": 2}
    assert session.scan("Event", fresh=True, limit=1).rows[0]["label"] == "source"
    with session.transaction("Event"):
        assert session.scan("Event", limit=1).rows[0]["label"] == "source"
    assert calls == ["copy.select", "source.select", "source.select"]


def test_missing_projection_refuses_without_fallback_but_count_can_use_that_copy() -> None:
    session, calls, _ = fixture(partial=True)
    with pytest.raises(sde.QueryRefused, match="materialization"):
        session.scan("Event")
    assert calls == []
    assert session.count("Event") == 9_007_199_254_740_993
    with pytest.raises(sde.QueryRefused, match="materialization"):
        session.count("Event", where={"label": "sensitive filter"})
    assert calls == ["copy.count"]
    assert session.scan("Event", fresh=True).rows[0]["label"] == "source"


def test_count_result_is_exact_and_not_reported_as_telemetry_row_count() -> None:
    session, _, recorder = fixture()
    assert session.count("Event", where={"label": "private value"}) == 9_007_199_254_740_993
    window = recorder.roll()
    assert window is not None
    assert len(window.shapes) == 1
    assert window.shapes[0].kind == "aggregate" and window.shapes[0].rows == 1
    assert "private value" not in str(window)
    assert "9007199254740993" not in str(window)


@pytest.mark.parametrize("operation", ["scan", "count"])
def test_capability_and_closed_session_refuse_before_calls(operation: str) -> None:
    session, calls, _ = fixture(query_capability=False)
    with pytest.raises(sde.QueryRefused, match="adapter"):
        getattr(session, operation)("Event")
    assert calls == []
    session.close()
    with pytest.raises(sde.ResourceClosed):
        getattr(session, operation)("Event")


@pytest.mark.parametrize(
    "options",
    [
        {"fresh": 1},
        {"where": []},
        {"limit": 0},
        {"after": {"bad": 1}},
        {"bounds": sde.Range("missing", 1)},
        {"order_by": "missing"},
    ],
)
def test_bad_scan_arguments_never_reach_an_adapter(options: dict[str, Any]) -> None:
    session, calls, recorder = fixture()
    with pytest.raises(sde.QueryRefused):
        session.scan("Event", **options)
    assert calls == [] and recorder.roll() is None

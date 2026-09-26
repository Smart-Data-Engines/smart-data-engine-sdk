"""`Session.measure_storage`: a group's size from its engine's catalogue, or why it stayed unknown.

Engine-free: the adapters here are the in-memory test double, with or without the catalogue
capability, so every branch - a size summed over a group's tables, the source alone during a
staging, an adapter without a catalogue, a refused read, a failed one, a table the map names that
does not exist - is reached without a server. The live half is `test_storage_sizes_live.py` and
`test_measure_storage_live.py`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

import sde
from sde.errors import EngineError, ResourceBusy
from sde.testing.loader import model_from_neutral
from sde.testing.memory import MemoryEngine

MODEL = model_from_neutral(
    {
        "entities": [
            {"name": "Detail", "fields": [{"name": "body", "type": "string"},
                                          {"name": "id", "type": "int64"}], "key": ["id"]},
            {"name": "Event", "fields": [{"name": "at", "type": "timestamptz"},
                                         {"name": "id", "type": "int64"}], "key": ["id"]},
            {"name": "Label", "fields": [{"name": "id", "type": "int64"},
                                         {"name": "name", "type": "string"}], "key": ["id"]},
        ],
        "relations": [{"name": "event", "from": "Detail", "to": "Event"}],
    }
)


class Catalogued(MemoryEngine):
    """The in-memory double with a catalogue: sizes by table, or an error to raise."""

    def __init__(self, sizes: Mapping[str, tuple[int, int]], *, error: BaseException | None = None,
                 **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.sizes = dict(sizes)
        self.error = error
        self.asked: list[list[str]] = []

    def storage_sizes(self, tables: Sequence[str]) -> dict[str, tuple[int, int]]:
        self.asked.append(list(tables))
        if self.error is not None:
            raise self.error
        return {table: self.sizes[table] for table in tables if table in self.sizes}


def placement(groups: Mapping[str, dict[str, Any]]) -> sde.PlacementMap:
    raw = {"contract": 3, "model_version": MODEL.version, "map_version": 1, "groups": dict(groups)}
    return sde.load_map(raw, model=MODEL)


def material(engine: str, tables: Mapping[str, str], identity: str = "source") -> dict[str, Any]:
    group = next(g for g in sde.colocation_groups(MODEL) if set(tables) <= set(g.members))
    layout = sde.default_layout(MODEL, group, dialect="postgres")
    return {
        "id": identity,
        "engine": engine,
        "layout": {
            "tables": dict(tables),
            "columns": {name: dict(columns) for name, columns in layout.columns.items()},
        },
    }


def two_groups(pg: Any, other: Any) -> sde.Session:
    events = {"Detail": "details", "Event": "events"}
    placed = placement({
        "Detail": {"source": material("pg", events)},
        "Label": {"source": material("other", {"Label": "labels"})},
    })
    recorder = sde.Recorder(MODEL.version)
    return sde.Session(MODEL, placed, {"pg": pg, "other": other}, recorder=recorder)


def test_a_groups_size_is_the_sum_over_its_tables_and_reaches_the_window() -> None:
    pg = Catalogued({"details": (1_000, 100), "events": (4_000, 900), "labels": (70, 0)})
    other = Catalogued({"labels": (500, 50)}, dialect="postgres", name="other")
    session = two_groups(pg, other)
    measured = session.measure_storage()
    assert measured.unavailable == {}
    found = [(s.group, s.engine, s.total_bytes, s.secondary_index_bytes) for s in measured.sizes]
    assert found == [("Detail", "pg", 5_000, 1_000), ("Label", "other", 500, 50)]
    assert pg.asked == [["details", "events"]], "one statement per engine, its tables only"
    session.save("Label", {"id": 1, "name": "x"})
    window = session._recorder.roll()  # type: ignore[union-attr]
    assert window is not None
    body = window.as_record(MODEL)["groups"]["Label"]
    assert body["total_bytes"] == 500 and body["index_to_table_ratio"] == 50 / 450


def test_an_adapter_without_a_catalogue_leaves_its_groups_unknown() -> None:
    pg = Catalogued({"details": (1, 0), "events": (1, 0)})
    session = two_groups(pg, MemoryEngine(name="other"))
    measured = session.measure_storage()
    assert measured.unavailable == {"Label": "unsupported"}
    assert [s.group for s in measured.sizes] == ["Detail"]


@pytest.mark.parametrize(
    ("cause", "reason"),
    [
        (type("InsufficientPrivilege", (Exception,), {"sqlstate": "42501"})("denied"), "refused"),
        (Exception("Code: 497. DB::Exception: Not enough privileges"), "refused"),
        (Exception("connection reset by peer"), "failed"),
    ],
)
def test_a_refused_or_failed_read_is_an_unknown_size_not_an_exception(
    cause: Exception, reason: str
) -> None:
    error = EngineError("storage sizes could not be read")
    error.__cause__ = cause
    session = two_groups(Catalogued({}, error=error), Catalogued({"labels": (9, 0)}, name="other"))
    measured = session.measure_storage()
    assert measured.unavailable == {"Detail": reason}
    assert [s.group for s in measured.sizes] == ["Label"]


def test_a_table_the_map_names_that_does_not_exist_is_missing_not_empty() -> None:
    other = Catalogued({"labels": (9, 0)}, name="other")
    session = two_groups(Catalogued({"events": (10, 0)}), other)
    assert session.measure_storage().unavailable == {"Detail": "missing_table"}


def test_misuse_of_an_adapter_is_the_callers_to_see() -> None:
    busy = Catalogued({}, error=ResourceBusy("another session owns the current transaction scope"))
    session = two_groups(busy, Catalogued({"labels": (9, 0)}, name="other"))
    with pytest.raises(ResourceBusy):
        session.measure_storage()


def test_only_the_source_is_measured_while_a_staging_maintains_a_copy() -> None:
    pg = Catalogued({"details": (1_000, 0), "events": (2_000, 0)})
    copy = Catalogued({"copy_details": (7, 0), "copy_events": (7, 0), "labels": (5, 0)},
                      name="copy")
    source = material("pg", {"Detail": "details", "Event": "events"})
    derived = material("copy", {"Detail": "copy_details", "Event": "copy_events"}, "stage")
    placed = placement({
        "Detail": {"source": source, "derived": [{**derived, "lag_budget_ms": 30_000}],
                  "also_write": ["stage"]},
        "Label": {"source": material("copy", {"Label": "labels"})},
    })
    session = sde.Session(MODEL, placed, {"pg": pg, "copy": copy})
    measured = session.measure_storage()
    assert [(s.group, s.total_bytes) for s in measured.sizes] == [("Detail", 3_000), ("Label", 5)]
    assert copy.asked == [["labels"]], "the copy's tables are not the group's size"


def test_without_a_recorder_the_size_is_still_returned() -> None:
    pg = Catalogued({"details": (1, 0), "events": (2, 0)})
    other = Catalogued({"labels": (3, 1)}, name="other")
    events = {"Detail": "details", "Event": "events"}
    placed = placement({
        "Detail": {"source": material("pg", events)},
        "Label": {"source": material("other", {"Label": "labels"})},
    })
    measured = sde.Session(MODEL, placed, {"pg": pg, "other": other}).measure_storage()
    assert len(measured.sizes) == 2

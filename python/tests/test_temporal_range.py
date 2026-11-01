"""A day or an instant an engine holds (format contract section 8b, decision 43 of the C++ port).

ClickHouse 24.8 holds ``Date32`` and ``DateTime64`` from 1900-01-01 to 2299-12-31 and moved a value
outside to the nearer end without an error; 26.9 holds the contract's whole range. The session's
refusals are pinned by ``errors/140``-``142`` with an engine set that declares 24.8's range; this
file holds the range itself and a migration's copy, which no vector reaches.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

import sde
from sde.errors import MigrationRefused
from sde.temporal_range import TemporalRange, from_engine_text
from sde.testing.memory import MemoryEngine

NARROW = from_engine_text(
    "1900-01-01", "2299-12-31", "1900-01-01 00:00:00.000000", "2299-12-31 23:59:59.999999"
)


@pytest.fixture(autouse=True)
def _isolate() -> None:
    sde.clear_registry()


def test_a_server_holding_the_whole_range_reports_no_range() -> None:
    whole = from_engine_text(
        "0001-01-01", "9999-12-31", "0001-01-01 00:00:00.000000", "9999-12-31 23:59:59.999999"
    )
    assert whole is None
    assert isinstance(NARROW, TemporalRange)


@pytest.mark.parametrize(
    ("kind", "value", "held"),
    [
        ("date", date(1900, 1, 1), True),
        ("date", date(2299, 12, 31), True),
        ("date", date(1899, 12, 31), False),
        ("date", date(2300, 1, 1), False),
        ("timestamp", datetime(2299, 12, 31, 23, 59, 59, 999999), True),
        ("timestamp", datetime(2300, 1, 1), False),
        ("timestamptz", datetime(1900, 1, 1, tzinfo=UTC), True),
        # An instant is held by its UTC reading: one microsecond before 1900 in UTC is outside.
        ("timestamptz", datetime(1899, 12, 31, 23, 59, 59, 999999, tzinfo=UTC), False),
    ],
)
def test_the_range_holds_its_ends_and_nothing_past_them(
    kind: str, value: object, held: bool
) -> None:
    assert NARROW is not None
    assert NARROW.holds(kind, value) is held


def _dated() -> tuple[sde.LogicalModel, str]:
    @sde.entity
    class Visit:
        id: sde.Int32
        on: date

        class Meta:
            key = ["id"]

    model = sde.build_model(Visit)
    return model, sde.colocation_groups(model)[0].name


def _migrating(target_range: TemporalRange | None) -> tuple[sde.Session, str, MemoryEngine]:
    model, group = _dated()
    columns = {"Visit": {"id": "integer", "on": "date"}}
    source = MemoryEngine("postgres", name="pg-main")
    target = MemoryEngine("clickhouse", name="ch-copy", temporal_range=target_range)
    session = sde.Session(
        model,
        sde.load_map({
            "contract": 3, "model_version": model.version, "map_version": 1,
            "groups": {group: {
                "source": {"id": "src", "engine": "pg-main",
                           "layout": {"tables": {"Visit": "visit"}, "columns": columns}},
                "derived": [{"id": "dst", "engine": "ch-copy", "lag_budget_ms": 30_000,
                             "layout": {"tables": {"Visit": "visit_copy"},
                                        "columns": {"Visit": {"id": "Int32", "on": "Date32"}}}}],
                "also_write": ["dst"],
            }},
        }, model=model),
        {"pg-main": source, "ch-copy": target},
    )
    source.tables["visit"] = [
        {"id": 1, "on": date(2026, 10, 7)},
        {"id": 2, "on": date(1800, 1, 1)},
    ]
    return session, group, target


def test_a_migration_refuses_a_chunk_its_target_cannot_hold_before_copying_any() -> None:
    session, group, target = _migrating(NARROW)
    with pytest.raises(MigrationRefused) as refused:
        sde.backfill(session, group, chunk_rows=10)
    assert str(refused.value).startswith(
        "ch-copy holds a date from 1900-01-01 to 2299-12-31, and a row of Visit gives on one "
        "outside it, so this chunk was not copied."
    )
    assert "1800" not in str(refused.value), "a refusal never repeats the client's value"
    assert target.tables.get("visit_copy", []) == [], "nor the row inside the range"


def test_a_target_holding_the_whole_range_takes_the_same_chunk() -> None:
    session, group, target = _migrating(None)
    assert sde.backfill(session, group, chunk_rows=10).complete
    assert len(target.tables["visit_copy"]) == 2

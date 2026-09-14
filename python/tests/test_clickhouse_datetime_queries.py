"""Temporal query bounds must retain the microseconds that the native table stored."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from test_generation_migration_live import engines as engines
from test_runtime_privileges_live import runtime_roles

import sde
from sde.testing.loader import model_from_neutral

PROJECT = "1" * 32


def declaration(kind: str) -> dict[str, Any]:
    return {
        "entities": [
            {
                "name": "Reading",
                "fields": [
                    {"name": "station", "type": "string"},
                    {"name": "at", "type": kind},
                    {"name": "value", "type": "int32"},
                ],
                "key": ["station", "at"],
            }
        ]
    }


def values(*, aware: bool, before_epoch: bool = False) -> list[dict[str, Any]]:
    base = datetime(
        1965 if before_epoch else 2026, 9, 14, 12, 0, 0, 123456, tzinfo=UTC if aware else None
    )
    return [
        {"station": "station'\\\\é", "at": base + timedelta(microseconds=n), "value": n}
        for n in range(4)
    ]


@pytest.fixture(params=["timestamp", "timestamptz"])
def temporal(request: Any) -> Iterator[tuple[Any, list[dict[str, Any]]]]:
    model = model_from_neutral(declaration(request.param))
    with runtime_roles("clickhouse") as role:
        layout = sde.default_layout(model, sde.colocation_groups(model)[0], dialect="clickhouse")
        role.operator.ensure_schema(layout, keys={"Reading": ["station", "at"]})
        role.grant("reading")
        rows = values(aware=request.param == "timestamptz")
        for row in rows:
            role.operator.insert("reading", row)
        yield role, rows


def test_point_reads_preserve_fractional_temporal_keys(temporal: Any) -> None:
    role, rows = temporal
    assert role.operator.count("reading") == len(rows)
    for expected in rows:
        assert (
            role.runtime.get("reading", {name: expected[name] for name in ("station", "at")})
            == expected
        )


def test_half_open_range_keeps_both_microsecond_boundaries(temporal: Any) -> None:
    role, rows = temporal
    assert role.runtime.range("reading", "at", low=rows[1]["at"], high=rows[3]["at"]) == rows[1:3]


def test_keyset_cursor_advances_and_its_upper_bound_is_inclusive(temporal: Any) -> None:
    role, rows = temporal
    first = role.runtime.key_range("reading", ["station", "at"], limit=1)
    assert first == rows[:1]
    cursor = [first[0][name] for name in ("station", "at")]
    assert role.runtime.key_range("reading", ["station", "at"], after=cursor, limit=1) == rows[1:2]
    assert role.runtime.key_range("reading", ["station", "at"], upto=cursor) == rows[:1]
    high = [rows[2][name] for name in ("station", "at")]
    assert (
        role.runtime.key_range("reading", ["station", "at"], after=cursor, upto=high) == rows[1:3]
    )


def test_aware_key_uses_its_instant_not_the_connection_formatting_zone(temporal: Any) -> None:
    role, rows = temporal
    # Column timezone and connection metadata need not be the same. Binding an already normalized
    # UTC string with an explicit SQL timezone does not consult this driver formatting setting.
    role.runtime._cx.server_tz = ZoneInfo("America/New_York")
    original = rows[0]["at"].replace(tzinfo=UTC)
    key = original.astimezone(ZoneInfo("Asia/Kathmandu"))
    assert role.runtime.get("reading", {"station": rows[0]["station"], "at": key}) == rows[0]


@pytest.mark.parametrize("kind", ["timestamp", "timestamptz"])
def test_pre_epoch_temporal_key_keeps_its_exact_fraction(kind: str) -> None:
    model = model_from_neutral(declaration(kind))
    with runtime_roles("clickhouse") as role:
        layout = sde.default_layout(model, sde.colocation_groups(model)[0], dialect="clickhouse")
        role.operator.ensure_schema(layout, keys={"Reading": ["station", "at"]})
        expected = values(aware=kind == "timestamptz", before_epoch=True)[0]
        role.operator.insert("reading", expected)
        assert (
            role.operator.get("reading", {name: expected[name] for name in ("station", "at")})
            == expected
        )


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_temporal_key_backfill_resumes_and_verifies_exact_values(
    engines: dict[str, Any], source: str
) -> None:
    model = model_from_neutral(declaration("timestamptz"))
    target = "clickhouse" if source == "postgres" else "postgres"

    def material(engine: str, name: str) -> dict[str, Any]:
        layout = sde.default_layout(model, sde.colocation_groups(model)[0], dialect=engine)
        return {
            "id": name,
            "engine": engine,
            "layout": {
                "tables": {"Reading": name + "_readings"},
                "columns": {entity: dict(fields) for entity, fields in layout.columns.items()},
            },
        }

    placement = sde.load_map(
        {
            "contract": 4,
            "project_id": PROJECT,
            "model_version": model.version,
            "map_version": 1,
            "groups": {
                "Reading": {
                    "source": material(source, "source"),
                    "write_epoch": 1,
                    "derived": [{**material(target, "copy"), "lag_budget_ms": 1000}],
                    "also_write": ["copy"],
                }
            },
        },
        model=model,
    )
    sde.prepare_schema(model, placement, engines, project_id=PROJECT)
    rows = values(aware=True)
    for row in rows:
        engines[source].insert("source_readings", {**row, sde.WRITE_EPOCH_COLUMN: 1})
    session = sde.Session(model, placement, engines, project_id=PROJECT)
    # Bound both calls so a broken cursor cannot make the regression hang indefinitely.
    sde.backfill(session, "Reading", chunk_rows=1, stop_after=2)
    assert engines[target].count("copy_readings") == 2
    sde.backfill(session, "Reading", chunk_rows=1, stop_after=5)
    assert engines[target].count("copy_readings") == len(rows)
    assert sde.verify(session, "Reading", chunk_rows=1).matched
    for expected in rows:
        key = {name: expected[name] for name in ("station", "at")}
        observed = engines[target].get("copy_readings", key)
        assert observed is not None
        observed.pop(sde.WRITE_EPOCH_COLUMN)
        assert observed == expected


def test_naive_query_key_is_utc_even_when_the_process_timezone_is_not(
    temporal: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    role, rows = temporal
    key = {"station": rows[0]["station"], "at": rows[0]["at"].replace(tzinfo=None)}
    try:
        with monkeypatch.context() as patch:
            patch.setenv("TZ", "Pacific/Honolulu")
            time.tzset()
            assert role.runtime.get("reading", key) == rows[0]
    finally:
        time.tzset()

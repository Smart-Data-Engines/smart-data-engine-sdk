"""A day or an instant the server holds, against a live ClickHouse (format contract section 8b).

What the test expects is the server's: ClickHouse 24.8 holds ``Date32`` and ``DateTime64`` from
1900-01-01 to 2299-12-31 and moved 1800-01-01 to 1900-01-01 without an error, and 26.9 holds the
contract's whole range. CI runs the live slices on both, so each half runs somewhere.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import pytest

import sde

CH_DSN = os.environ.get("SDE_CLICKHOUSE_DSN")

pytestmark = pytest.mark.skipif(not CH_DSN, reason="set SDE_CLICKHOUSE_DSN for a live ClickHouse")


@pytest.fixture(autouse=True)
def _isolate() -> None:
    sde.clear_registry()


@pytest.fixture
def engine() -> Iterator[Any]:
    from sde.engines.clickhouse import ClickHouseEngine

    adapter = ClickHouseEngine(CH_DSN or "")
    adapter.connect()
    try:
        yield adapter
    finally:
        adapter.close()


def _session(engine: Any, table: str) -> sde.Session:
    @sde.entity
    class Visit:
        id: sde.Int32
        on: date
        at: datetime

        class Meta:
            key = ["id"]

    model = sde.build_model(Visit)
    group = sde.colocation_groups(model)[0].name
    columns = {"id": "Int32", "on": "Date32", "at": "DateTime64(6, 'UTC')"}
    session = sde.Session(
        model,
        sde.load_map({
            "contract": 3, "model_version": model.version, "map_version": 1,
            "groups": {group: {"source": {"id": "src", "engine": "ch", "layout": {
                "tables": {"Visit": table}, "columns": {"Visit": columns}}}}},
        }, model=model),
        {"ch": engine},
    )
    session.ensure_schema()
    return session


def test_the_server_says_its_range_and_a_value_outside_it_never_reaches_the_server(
    engine: Any,
) -> None:
    table = "range_" + uuid.uuid4().hex[:8]
    try:
        session = _session(engine, table)
        span = engine.temporal_range
        # The adapter's report against the server's own answer. A binary write and read
        # round-trip 1800-01-01 even on 24.8, so the rest of this test alone could not tell a range
        # reported whole from one that is.
        first, last = engine._cx.query(
            "SELECT toString(toDate32('0001-01-01')), toString(toDate32('9999-12-31'))"
        ).result_rows[0]
        assert (span is None) == (first == "0001-01-01")
        if span is not None:
            assert (span.first_day.isoformat(), span.last_day.isoformat()) == (first, last)
        # Both ends of the narrower range are held wherever the server stands.
        ends = {"id": 1, "on": date(1900, 1, 1), "at": datetime(2299, 12, 31, 23, 59, 59, 999999,
                                                                 tzinfo=UTC)}
        session.save("Visit", ends)
        assert session.get("Visit", {"id": 1}) == ends
        before = {"id": 2, "on": date(1800, 1, 1), "at": datetime(2026, 10, 7, tzinfo=UTC)}
        if span is None:
            # 26.9: the contract's whole range, and the value comes back as written.
            session.save("Visit", before)
            assert session.get("Visit", {"id": 2}) == before
        else:
            # 24.8: refused before the server could move it to 1900-01-01.
            assert (span.first_day, span.last_day) == (date(1900, 1, 1), date(2299, 12, 31))
            with pytest.raises(sde.EngineError) as refused:
                session.save("Visit", before)
            assert str(refused.value) == (
                "ch holds a date from 1900-01-01 to 2299-12-31, and this row gives Visit.on one "
                "outside it; nothing was sent"
            )
            assert session.get("Visit", {"id": 2}) is None
        assert engine.temporal_range == span, "read once per connection"
    finally:
        engine._cx.command(f"DROP TABLE IF EXISTS `{table}`")

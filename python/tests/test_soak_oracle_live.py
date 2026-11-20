"""The oracle's count of a reading key's physical rows holds one key at a time, on a live engine.

On 20 November 2026 the first day-long soak's judge died on that count: every key of a language's
3.4 million readings counted at once was more than the soak's own ClickHouse would hold, and the
verdict was lost. The table is ordered by (station, at), as the soak's is, and the count now goes
in that order. The control: the first day's statement, under the same memory budget, is refused.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import pytest
from qualification.soak_oracle import DUPLICATES_SQL, DUPLICATES_SETTINGS

DSN = os.environ.get("SDE_CLICKHOUSE_DSN")
ROWS = 4_000_000
BUDGET = {"max_memory_usage": 64 * 1024 * 1024}
FIRST_DAY = (
    "SELECT at, count() FROM weather_reading WHERE station = {station:String} "
    "GROUP BY at HAVING count() > 1"
)


def _client(database: str | None = None) -> Any:
    import clickhouse_connect

    assert DSN is not None
    parts = urlsplit(DSN)
    return clickhouse_connect.get_client(
        host=parts.hostname or "127.0.0.1",
        port=parts.port or 8123,
        username=unquote(parts.username or "default"),
        password=unquote(parts.password or ""),
        database=database or "default",
        send_receive_timeout=600,
    )


@pytest.mark.skipif(DSN is None, reason="set SDE_CLICKHOUSE_DSN to a ClickHouse server")
def test_the_copies_of_four_million_keys_are_counted_in_a_small_memory_budget() -> None:
    database = "soak_oracle_" + uuid4().hex[:12]
    admin = _client()
    admin.command(f"CREATE DATABASE {database}")
    try:
        client = _client(database)
        client.command(
            "CREATE TABLE weather_reading (station String, at DateTime64(6, 'UTC'), "
            "celsius Decimal(8, 2), humidity Int64, id UUID) ENGINE = MergeTree "
            "ORDER BY (station, at)"
        )
        start = "toDateTime64('2026-11-19 11:00:00', 6, 'UTC')"
        client.command(
            f"INSERT INTO weather_reading SELECT 'soak-python', "
            f"{start} + toIntervalMillisecond(number), 1.5, 50, generateUUIDv4() "
            f"FROM numbers({ROWS})"
        )
        # One key written twice, in a part of its own: what the count is for.
        client.command(
            f"INSERT INTO weather_reading SELECT 'soak-python', "
            f"{start} + toIntervalMillisecond(7), 1.5, 50, generateUUIDv4()"
        )
        station = {"station": "soak-python"}
        found = client.query(
            DUPLICATES_SQL, parameters=station, settings={**DUPLICATES_SETTINGS, **BUDGET}
        ).result_rows
        assert [count for _at, count in found] == [2]
        with pytest.raises(Exception, match="MEMORY_LIMIT_EXCEEDED|memory limit"):
            client.query(
                FIRST_DAY,
                parameters=station,
                settings={**BUDGET, "max_bytes_before_external_group_by": 0},
            )
    finally:
        admin.command(f"DROP DATABASE IF EXISTS {database} SYNC")

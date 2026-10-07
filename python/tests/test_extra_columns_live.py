"""``sde.schema.extra_columns`` names a column added outside SDE, never the library's own.

The event is for a client's addition (``sde.logging``): allowed, and worth one line. From contract
4 a table carries ``__sde_write_epoch``, which provisioning adds for a map with write generations,
and every schema check named it. So an alert on the event fired on every start of every deployment
with generations, about a column that is ours. Found by the C++ port (finding 21), captured on
ClickHouse on 7 October 2026.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from typing import Any

import pytest

import sde
from sde.engines.clickhouse import ClickHouseEngine
from sde.engines.postgres import PostgresEngine
from sde.generation import EPOCH_COLUMN

DSN = {
    "postgres": os.environ.get("SDE_POSTGRES_DSN"),
    "clickhouse": os.environ.get("SDE_CLICKHOUSE_DSN"),
}
TABLE = "extra_columns_probe"


def _statement(engine: Any, dialect: str, sql: str) -> None:
    if dialect == "postgres":
        with engine._cx.cursor() as cursor:
            cursor.execute(sql)
    else:
        engine._cx.command(sql)


def _named(records: list[logging.LogRecord]) -> list[list[str]]:
    return [
        list(record.sde_fields["columns"])  # type: ignore[attr-defined]
        for record in records
        if getattr(record, "sde_event", None) == "sde.schema.extra_columns"
    ]


@pytest.fixture(params=["postgres", "clickhouse"])
def provisioned(request: pytest.FixtureRequest) -> Iterator[tuple[Any, str, Any]]:
    dialect: str = request.param
    dsn = DSN[dialect]
    if dsn is None:
        pytest.skip(f"set SDE_{dialect.upper()}_DSN")
    sde.clear_registry()

    @sde.entity
    class ExtraColumnsProbe:
        id: int
        label: str

    model = sde.build_model(ExtraColumnsProbe)
    layout = sde.default_layout(model, sde.colocation_groups(model)[0], dialect=dialect)
    assert layout.table_for("ExtraColumnsProbe") == TABLE
    engine: Any = PostgresEngine(dsn) if dialect == "postgres" else ClickHouseEngine(dsn)
    engine.connect()
    quote = '"' if dialect == "postgres" else "`"
    drop = f"DROP TABLE IF EXISTS {quote}{TABLE}{quote}"
    if dialect == "clickhouse":
        drop += " SYNC"
    _statement(engine, dialect, drop)
    try:
        engine.ensure_schema(layout, keys={"ExtraColumnsProbe": ("id",)})
        yield engine, dialect, layout
    finally:
        _statement(engine, dialect, drop)
        engine.close()


def test_the_generation_column_is_not_named_and_a_clients_column_is(
    provisioned: tuple[Any, str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    engine, dialect, layout = provisioned
    keys = {"ExtraColumnsProbe": ("id",)}
    integer, text = ("bigint", "text") if dialect == "postgres" else ("Int64", "String")
    quote = '"' if dialect == "postgres" else "`"
    caplog.set_level(logging.INFO, logger="sde")

    # What provisioning adds for a map with write generations.
    alter = f"ALTER TABLE {quote}{TABLE}{quote} ADD COLUMN"
    _statement(engine, dialect, f"{alter} {quote}{EPOCH_COLUMN}{quote} {integer}")
    engine.ensure_schema(layout, keys=keys)
    assert _named(caplog.records) == []

    # The control: a column a client added outside SDE is still named, and alone.
    caplog.clear()
    _statement(engine, dialect, f"{alter} {quote}client_note{quote} {text}")
    engine.ensure_schema(layout, keys=keys)
    assert _named(caplog.records) == [["client_note"]]

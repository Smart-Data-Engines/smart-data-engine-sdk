"""The engine facts a running engine shows: a transaction, and a second save under one key.

``test_engine_facts.py`` holds what the adapters' code shows; this holds the rest against
PostgreSQL and ClickHouse. The orderbook engine's answer to the same question - both rows stored -
is measured in ``test_orderbook_slice.py``.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

import sde
from sde.engines.clickhouse import ClickHouseEngine
from sde.engines.postgres import PostgresEngine
from sde.errors import EngineError

POSTGRES = os.environ.get("SDE_POSTGRES_DSN")
CLICKHOUSE = os.environ.get("SDE_CLICKHOUSE_DSN")


def _layout(dialect: str, table: str) -> sde.PhysicalLayout:
    integer, text = ("bigint", "text") if dialect == "postgres" else ("Int64", "String")
    columns = {"Event": {"id": integer, "name": text}}
    return sde.PhysicalLayout(tables={"Event": table}, columns=columns)


@pytest.mark.skipif(not POSTGRES, reason="set SDE_POSTGRES_DSN")
def test_postgres_opens_a_transaction_and_enforces_its_key() -> None:
    facts = sde.engine_facts("postgres")
    assert facts["transactions"] is True and facts["key"] == "enforced"
    assert POSTGRES is not None
    table = "facts_" + uuid4().hex[:12]
    with PostgresEngine(POSTGRES) as engine:
        engine.ensure_schema(_layout("postgres", table), keys={"Event": ["id"]})
        try:
            with engine.transaction():
                engine.insert(table, {"id": 1, "name": "first"})
            with pytest.raises(EngineError):
                engine.insert(table, {"id": 1, "name": "second"})
            assert engine.get(table, {"id": 1}) == {"id": 1, "name": "first"}
        finally:
            engine._cx.execute(f'DROP TABLE IF EXISTS "{table}"')


@pytest.mark.skipif(not CLICKHOUSE, reason="set SDE_CLICKHOUSE_DSN")
def test_clickhouse_keeps_the_newest_row_for_a_key() -> None:
    facts = sde.engine_facts("clickhouse")
    assert facts["transactions"] is False and facts["key"] == "newest_wins"
    assert CLICKHOUSE is not None
    table = "facts_" + uuid4().hex[:12]
    with ClickHouseEngine(CLICKHOUSE) as engine:
        engine.ensure_schema(_layout("clickhouse", table), keys={"Event": ["id"]})
        try:
            # Merges stopped, or ClickHouse collapses the duplicate before a read shows who won.
            engine._cx.command(f"SYSTEM STOP MERGES `{table}`")
            engine.insert(table, {"id": 1, "name": "first"})
            engine.insert(table, {"id": 1, "name": "second"})
            assert engine._cx.query(f"SELECT count() FROM `{table}`").result_rows[0][0] == 2
            assert engine.get(table, {"id": 1}) == {"id": 1, "name": "second"}
        finally:
            engine._cx.command(f"DROP TABLE IF EXISTS `{table}` SYNC")

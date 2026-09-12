"""Write barriers against real engines, with resources owned by each test."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest

import sde
from sde.schema import QUOTE

PROJECT = "1" * 32
HOLD = "2" * 32


@pytest.fixture
def guarded(request: pytest.FixtureRequest) -> Iterator[tuple[Any, str, sde.WriteFence]]:
    dialect = request.param
    variable = "SDE_POSTGRES_DSN" if dialect == "postgres" else "SDE_CLICKHOUSE_DSN"
    dsn = os.environ.get(variable)
    if not dsn:
        pytest.skip(f"{variable} is required for the native write-fence slice")
    namespace = "sde_fence_" + uuid4().hex[:16]
    table = 'event "quoted' if dialect == "postgres" else "event `quoted"
    quote = QUOTE[dialect]
    if dialect == "postgres":
        from sde.engines.postgres import PostgresEngine

        engine = PostgresEngine(dsn)
        engine.connect()
        engine._cx.execute(f"CREATE SCHEMA {quote(namespace)}")
        engine._cx.execute(f"SET search_path TO {quote(namespace)}")
        engine._cx.execute(f"CREATE TABLE {quote(table)} (id bigint PRIMARY KEY)")
    else:
        from sde.engines.clickhouse import ClickHouseEngine

        with ClickHouseEngine(dsn) as setup:
            setup._cx.command(f"CREATE DATABASE {quote(namespace)} ENGINE=Atomic")
        parts = urlsplit(dsn)
        local = urlunsplit(
            (parts.scheme, parts.netloc, "/" + namespace, parts.query, parts.fragment)
        )
        engine = ClickHouseEngine(local)  # type: ignore[assignment]
        engine.connect()
        engine._cx.command(
            f"CREATE TABLE {quote(table)} (id Int64) ENGINE=ReplacingMergeTree ORDER BY id"
        )
    try:
        yield engine, table, engine.write_fence(table, project_id=PROJECT)
    finally:
        if dialect == "postgres":
            engine._cx.execute(f"DROP SCHEMA {quote(namespace)} CASCADE")
        else:
            # Cleanup only this fixture's table, including a test interrupted after DETACH.
            found = engine._cx.query(
                "SELECT count() FROM system.detached_tables WHERE database=currentDatabase()"
            ).result_rows[0][0]
            if found:
                engine._cx.command(f"ATTACH TABLE {quote(table)}")
            engine._cx.command(f"DROP DATABASE {quote(namespace)} SYNC")
        engine.close()


@pytest.mark.parametrize("guarded", ["postgres", "clickhouse"], indirect=True)
def test_native_constraints_reject_unstamped_old_and_future_writers(guarded: Any) -> None:
    engine, table, fence = guarded
    assert fence.prepare(1).epoch == 1
    with pytest.raises(sde.EngineError):
        engine.insert(table, {"id": 1})
    engine.insert(table, {"id": 2, sde.WRITE_EPOCH_COLUMN: 1})
    with pytest.raises(sde.EngineError):
        engine.insert(table, {"id": 3, sde.WRITE_EPOCH_COLUMN: 2})
    assert fence.freeze(HOLD).closed
    with pytest.raises(sde.EngineError):
        engine.insert(table, {"id": 4, sde.WRITE_EPOCH_COLUMN: 1})
    assert fence.advance(2).closed
    fence.release(HOLD)
    with pytest.raises(sde.EngineError):
        engine.insert(table, {"id": 5, sde.WRITE_EPOCH_COLUMN: 1})
    engine.insert(table, {"id": 6, sde.WRITE_EPOCH_COLUMN: 2})
    assert engine.get(table, {"id": 2}) is not None
    assert engine.get(table, {"id": 6}) is not None
    assert engine.count(table) == 2


@pytest.mark.parametrize("guarded", ["clickhouse"], indirect=True)
def test_an_interruption_after_detach_resumes_only_the_logged_table(
    guarded: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, table, fence = guarded
    fence.prepare(1)
    engine.insert(table, {"id": 1, sde.WRITE_EPOCH_COLUMN: 1})
    backend = fence._backend
    original = backend._command

    def interrupted(statement: str) -> None:
        if statement.startswith("ATTACH TABLE"):
            raise sde.EngineError("executor died after confirmed DETACH")
        original(statement)

    monkeypatch.setattr(backend, "_command", interrupted)
    with pytest.raises(sde.EngineError, match="executor died"):
        fence.freeze(HOLD)
    with pytest.raises(sde.EngineError):
        engine.insert(table, {"id": 2, sde.WRITE_EPOCH_COLUMN: 1})
    monkeypatch.undo()
    assert fence.resume(HOLD).closed
    assert engine.count(table) == 1
    fence.release(HOLD)
    engine.insert(table, {"id": 3, sde.WRITE_EPOCH_COLUMN: 1})
    assert engine.count(table) == 2


@pytest.mark.parametrize("guarded", ["postgres"], indirect=True)
def test_postgres_barrier_waits_for_the_open_source_transaction(guarded: Any) -> None:
    import time
    from concurrent.futures import ThreadPoolExecutor

    engine, table, fence = guarded
    fence.prepare(1)
    namespace = engine._cx.execute("SELECT current_schema()").fetchone()[0]
    writer = engine._psycopg.connect(engine._dsn, autocommit=False)
    observer = engine._psycopg.connect(engine._dsn, autocommit=True)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        writer.execute(f"SET search_path TO {QUOTE['postgres'](namespace)}")
        writer.execute(
            f"INSERT INTO {QUOTE['postgres'](table)} "
            f"(id, {QUOTE['postgres'](sde.WRITE_EPOCH_COLUMN)}) VALUES (1,1)"
        )
        future = pool.submit(fence.freeze, HOLD)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            row = observer.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                [engine._cx.info.backend_pid],
            ).fetchone()
            if row and row[0] == "Lock":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("the barrier was not observed waiting for the writer")
        assert not future.done()
        writer.commit()
        assert future.result(timeout=5).closed
        assert engine.count(table) == 1
        with pytest.raises(sde.EngineError):
            engine.insert(table, {"id": 2, sde.WRITE_EPOCH_COLUMN: 1})
    finally:
        writer.rollback()
        pool.shutdown(wait=True)
        writer.close()
        observer.close()


@pytest.mark.parametrize("guarded", ["clickhouse"], indirect=True)
def test_clickhouse_barrier_waits_for_an_insert_using_old_metadata(guarded: Any) -> None:
    import time
    from concurrent.futures import ThreadPoolExecutor

    from sde.engines.clickhouse import ClickHouseEngine

    engine, table, fence = guarded
    fence.prepare(1)
    marker = "fence_insert_" + uuid4().hex
    pool = ThreadPoolExecutor(max_workers=1)
    writer = ClickHouseEngine(engine._dsn)
    writer.connect()
    try:
        future = pool.submit(
            writer._cx.command,
            f"INSERT INTO {QUOTE['clickhouse'](table)} SELECT 1, 1+sleep(2) /* {marker} */",
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows = engine._cx.query(
                "SELECT count() FROM system.processes WHERE startsWith(query,'INSERT') "
                "AND position(query,{marker:String})>0 AND elapsed>0.1",
                parameters={"marker": marker},
            ).result_rows
            if rows[0][0] == 1:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("the old INSERT was not observed running")
        assert fence.freeze(HOLD).closed
        # A metadata-only ALTER returns before this row commits. The fixture also requires that
        # old INSERT to succeed, proving it captured metadata before the new constraint existed.
        assert engine.count(table) == 1
        future.result(timeout=5)
        with pytest.raises(sde.EngineError):
            engine.insert(table, {"id": 2, sde.WRITE_EPOCH_COLUMN: 1})
    finally:
        pool.shutdown(wait=True)
        writer.close()


@pytest.mark.parametrize("guarded", ["clickhouse"], indirect=True)
def test_drain_intent_is_confirmed_even_with_asynchronous_client_defaults(
    guarded: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _table, fence = guarded
    fence.prepare(1)
    engine._cx.set_client_setting("async_insert", 1)
    engine._cx.set_client_setting("wait_for_async_insert", 0)
    engine._cx.set_client_setting("async_insert_busy_timeout_ms", 2000)
    engine._cx.set_client_setting("async_insert_use_adaptive_busy_timeout", 0)
    settings = engine._cx.query(
        "SELECT getSetting('async_insert'), getSetting('wait_for_async_insert')"
    ).result_rows
    assert settings == [(True, False)]
    backend = fence._backend
    original = backend._command
    observed = []

    def require_intent_before_detach(statement: str) -> None:
        if statement.startswith("DETACH TABLE"):
            rows = engine._cx.query(
                "SELECT count() FROM __sde_fence_drains WHERE hold={hold:String}",
                parameters={"hold": HOLD},
            ).result_rows
            observed.append(rows[0][0])
            assert rows[0][0] == 1, "DETACH would begin before the intent is present in the engine"
        original(statement)

    monkeypatch.setattr(backend, "_command", require_intent_before_detach)
    assert fence.freeze(HOLD).closed
    assert observed == [1]

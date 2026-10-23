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


@pytest.mark.parametrize("guarded", ["postgres"], indirect=True)
def test_postgres_a_retried_barrier_still_waits_for_a_writer_holding_the_table(
    guarded: Any,
) -> None:
    """A retry finds its constraint in place, so its ALTER TABLE has nothing to wait for: only the
    drain's LOCK TABLE holds it until a writer that took the table after the first barrier is done.

    The test above passed with that lock removed, because its first barrier waits in the ALTER.
    Finding 17 of the C++ port, whose `ARetriedBarrierStillWaitsForAWriterHoldingTheTable` this is.
    """
    import time
    from concurrent.futures import ThreadPoolExecutor

    engine, table, fence = guarded
    fence.prepare(1)
    engine.insert(table, {"id": 1, sde.WRITE_EPOCH_COLUMN: 1})
    assert fence.freeze(HOLD).closed
    namespace = engine._cx.execute("SELECT current_schema()").fetchone()[0]
    writer = engine._psycopg.connect(engine._dsn, autocommit=False)
    observer = engine._psycopg.connect(engine._dsn, autocommit=True)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        writer.execute(f"SET search_path TO {QUOTE['postgres'](namespace)}")
        # A DELETE writes no row the closed barrier could refuse, and holds the table to commit.
        writer.execute(f"DELETE FROM {QUOTE['postgres'](table)} WHERE id = 1")
        future = pool.submit(fence.freeze, HOLD)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not future.done():
            row = observer.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                [engine._cx.info.backend_pid],
            ).fetchone()
            if row and row[0] == "Lock":
                break
            time.sleep(0.01)
        assert not future.done(), "the retried barrier returned while a writer held the table"
        writer.commit()
        assert future.result(timeout=5).closed
        assert engine.count(table) == 0
    finally:
        writer.rollback()
        pool.shutdown(wait=True)
        writer.close()
        observer.close()


@pytest.fixture
def schema() -> Iterator[Any]:
    """An isolated PostgreSQL schema, for tables the fixture above cannot shape."""
    dsn = os.environ.get("SDE_POSTGRES_DSN")
    if not dsn:
        pytest.skip("SDE_POSTGRES_DSN is required for the native write-fence slice")
    from sde.engines.postgres import PostgresEngine

    namespace = "sde_fence_" + uuid4().hex[:16]
    engine = PostgresEngine(dsn)
    engine.connect()
    engine._cx.execute(f"CREATE SCHEMA {QUOTE['postgres'](namespace)}")
    engine._cx.execute(f"SET search_path TO {QUOTE['postgres'](namespace)}")
    try:
        yield engine
    finally:
        engine._cx.execute(f"DROP SCHEMA {QUOTE['postgres'](namespace)} CASCADE")
        engine.close()


# What the fence's metadata refuses on PostgreSQL. The reference had no test of any of it; the C++
# port tested each case and the reference answered every one the same way (finding 18).


def test_postgres_a_fence_guards_only_an_ordinary_table_without_inheritance(schema: Any) -> None:
    schema._cx.execute("CREATE TABLE parent (id bigint PRIMARY KEY)")
    schema._cx.execute("CREATE TABLE child () INHERITS (parent)")
    schema._cx.execute("CREATE VIEW shown AS SELECT id FROM parent")
    for table in ("parent", "child", "shown"):
        with pytest.raises(sde.MigrationRefused, match="ordinary PostgreSQL tables without"):
            schema.write_fence(table, project_id=PROJECT).state()
    with pytest.raises(sde.EngineError, match="write-fence table does not exist"):
        schema.write_fence("absent", project_id=PROJECT).state()


@pytest.mark.parametrize(
    "definition",
    [
        "integer NOT NULL DEFAULT 0",
        "bigint DEFAULT 0",
        "bigint NOT NULL DEFAULT 1",
        "bigint NOT NULL",
        "bigint NOT NULL GENERATED ALWAYS AS (0) STORED",
    ],
)
def test_postgres_the_reserved_column_defined_otherwise_is_refused(
    schema: Any, definition: str
) -> None:
    column = QUOTE["postgres"](sde.WRITE_EPOCH_COLUMN)
    schema._cx.execute(f"CREATE TABLE guarded (id bigint PRIMARY KEY, {column} {definition})")
    with pytest.raises(sde.MigrationRefused, match="has an incompatible definition"):
        schema.write_fence("guarded", project_id=PROJECT).state()


def test_postgres_the_reserved_column_as_the_fence_defines_it_is_its_own(schema: Any) -> None:
    column = QUOTE["postgres"](sde.WRITE_EPOCH_COLUMN)
    schema._cx.execute(
        f"CREATE TABLE guarded (id bigint PRIMARY KEY, {column} bigint NOT NULL DEFAULT 0)"
    )
    assert schema.write_fence("guarded", project_id=PROJECT).state().column == "valid"


def test_postgres_fence_ddl_is_refused_inside_an_application_transaction(schema: Any) -> None:
    schema._cx.execute("CREATE TABLE plain (id bigint PRIMARY KEY)")
    refused = pytest.raises(sde.MigrationRefused, match="inside an application transaction")
    with refused, schema.transaction():
        schema.write_fence("plain", project_id=PROJECT).prepare(1)
    # Nothing was changed by the refused call: the table has no reserved column.
    assert schema.write_fence("plain", project_id=PROJECT).state().column == "absent"


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

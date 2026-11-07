"""Leaving, against real engines: what the library left, and its removal run as plain text.

The claim a departing client needs is not that a function returns the right strings. It is that a
write **without this library** - an ordinary INSERT, from code that has never heard of a write
generation - is refused while our fence is in place and accepted once the statements
:func:`sde.departure` lists have run. So every test here writes the way a client's own code would,
through the raw connection and with no generation, and runs the listed statements **as text** on
that same raw connection - never through the library's own fence code - because the listing is
also what an administrator runs by hand.

The control is the refusal before the release: a test that only showed the write succeeding after
it would pass just as well against an engine where nothing had been fenced at all.

Each engine gets an isolated namespace: a PostgreSQL schema, a ClickHouse database, and on
Elasticsearch fresh index names. Elasticsearch has no namespace for bookkeeping, so the one test
that removes ``sde_map_state`` there removes the shared test cluster's - which the library creates
again on first use.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest

import sde
from sde.departure import bookkeeping_statement
from sde.write_fence import EPOCH_COLUMN, FENCE_PREFIX

PROJECT = "d" * 32
OTHER = "e" * 32
HOLD = "5" * 32
DSN = {
    "postgres": "SDE_POSTGRES_DSN",
    "clickhouse": "SDE_CLICKHOUSE_DSN",
    "elasticsearch": "SDE_ELASTICSEARCH_DSN",
}
ALL = ["postgres", "clickhouse", "elasticsearch"]


@contextmanager
def _namespace(dialect: str) -> Iterator[tuple[Any, str]]:
    """A connected adapter in a namespace of its own, and a fresh prefix for table names."""
    dsn = os.environ.get(DSN[dialect])
    if not dsn:
        pytest.skip(f"{DSN[dialect]} is required for the live departure tests")
    space = "sde_depart_" + uuid4().hex[:12]
    if dialect == "postgres":
        from sde.engines.postgres import PostgresEngine

        engine: Any = PostgresEngine(dsn)
        engine.connect()
        engine._cx.execute(f'CREATE SCHEMA "{space}"')
        engine._cx.execute(f'SET search_path TO "{space}"')
        try:
            yield engine, "reading"
        finally:
            engine._cx.execute(f'DROP SCHEMA "{space}" CASCADE')
            engine.close()
    elif dialect == "clickhouse":
        from sde.engines.clickhouse import ClickHouseEngine

        with ClickHouseEngine(dsn) as setup:
            setup._cx.command(f"CREATE DATABASE `{space}` ENGINE=Atomic")
        parts = urlsplit(dsn)
        engine = ClickHouseEngine(
            urlunsplit((parts.scheme, parts.netloc, "/" + space, parts.query, parts.fragment))
        )
        engine.connect()
        try:
            yield engine, "reading"
        finally:
            # Permanently detached only: 24.8 also lists a table dropped with SYNC, with
            # is_permanently = 0, which cannot be attached because it is gone.
            detached = engine._cx.query(
                "SELECT table FROM system.detached_tables WHERE database=currentDatabase() "
                "AND is_permanently=1"
            ).result_rows
            for (table,) in detached:
                engine._cx.command(f"ATTACH TABLE `{table}`")
            engine._cx.command(f"DROP DATABASE `{space}` SYNC")
            engine.close()
    else:
        from sde.engines.elasticsearch import ElasticsearchEngine

        engine = ElasticsearchEngine(dsn)
        engine.connect()
        try:
            yield engine, space
        finally:
            for index in (space, space + "_retired", space + "_theirs"):
                engine._request("PUT", f"/{index}/_settings", {"index.blocks.write": False})
                engine._request("PUT", f"/{index}/_settings", {"index": {"final_pipeline": None}})
                engine._request("DELETE", f"/{index}")
                engine._request("DELETE", f"/_ingest/pipeline/{FENCE_PREFIX}{index}")
            engine.close()


def _signed(dialect: str, table: str, *, project: str = PROJECT) -> tuple[Any, Any]:
    """A signed generation-bearing map of one entity in ``table``, at generation 1."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sde.clear_registry()

    @sde.entity
    class Reading:
        id: int
        station: str

    model = sde.build_model(Reading)
    columns = {
        "postgres": {"id": "bigint", "station": "text"},
        "clickhouse": {"id": "Int64", "station": "String"},
        "elasticsearch": {"id": "long", "station": "keyword"},
    }[dialect]
    raw: dict[str, Any] = {
        "contract": 4,
        "project_id": project,
        "model_version": model.version,
        # From the clock, as the operator's live tests do: Elasticsearch keeps one watermark for
        # the whole cluster, and a version below another test's would be refused as a rollback.
        "map_version": time.time_ns() // 1_000_000,
        "groups": {
            "Reading": {
                "write_epoch": 1,
                "source": {
                    "id": "source",
                    "engine": "db",
                    "layout": {"tables": {"Reading": table}, "columns": {"Reading": columns}},
                },
            }
        },
    }
    key = Ed25519PrivateKey.generate()
    raw["signature"] = {
        "alg": "ed25519",
        "value": base64.b64encode(key.sign(sde.canonical_bytes(raw))).decode(),
    }
    public = key.public_key().public_bytes_raw()
    return model, sde.load_map(raw, model=model, public_key=public, require_signature=True)


def _plain_insert(dialect: str, engine: Any, table: str, identity: int) -> bool:
    """One row written as a client's own code would: no library call, no generation. Accepted?"""
    if dialect == "elasticsearch":
        status, _ = engine._request(
            "PUT", f"/{table}/_doc/{identity}?refresh=wait_for", {"id": identity, "station": "KRK"}
        )
        return 200 <= int(status) < 300
    try:
        if dialect == "postgres":
            with engine._cx.cursor() as cursor:
                cursor.execute(
                    f'INSERT INTO "{table}" (id, station) VALUES (%s, %s)', [identity, "KRK"]
                )
        else:
            engine._cx.command(f"INSERT INTO `{table}` (id, station) VALUES ({identity}, 'KRK')")
    except Exception:
        return False
    return True


def _as_text(dialect: str, engine: Any, statements: tuple[str, ...]) -> None:
    """Run listed statements the way an administrator would: the text, on a raw connection.

    The Elasticsearch lines are console syntax; this parses them independently of the library's
    own runner, so that a line only the library could execute would fail here.
    """
    for statement in statements:
        if dialect == "elasticsearch":
            method, path, *body = statement.split(" ", 2)
            status, answer = engine._request(method, path, json.loads(body[0]) if body else None)
            assert 200 <= int(status) < 300, (statement, status, answer)
        elif dialect == "postgres":
            with engine._cx.cursor() as cursor:
                cursor.execute(statement)
        else:
            engine._cx.command(statement)


def _count(dialect: str, engine: Any, table: str) -> int:
    if dialect == "elasticsearch":
        engine._request("POST", f"/{table}/_refresh")
        status, body = engine._request("GET", f"/{table}/_count")
        assert int(status) == 200
        return int(body["count"])
    if dialect == "postgres":
        return int(engine._cx.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
    return int(engine._cx.query(f"SELECT count() FROM `{table}` FINAL").result_rows[0][0])


def _ours_in_catalogue(dialect: str, engine: Any) -> list[str]:
    """Every object of this library the engine's own catalogue still shows, independently read."""
    if dialect == "postgres":
        rows = engine._cx.execute(
            "SELECT c.relname || '.' || k.conname FROM pg_constraint k JOIN pg_class c "
            "ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=current_schema() AND starts_with(k.conname, %s) "
            "UNION ALL SELECT c.relname || '.' || a.attname FROM pg_attribute a JOIN pg_class c "
            "ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=current_schema() AND a.attname=%s AND NOT a.attisdropped "
            "UNION ALL SELECT c.relname FROM pg_class c JOIN pg_namespace n "
            "ON n.oid=c.relnamespace WHERE n.nspname=current_schema() "
            "AND c.relname IN ('sde_map_state', 'sde_backfill_state') ORDER BY 1",
            [FENCE_PREFIX, EPOCH_COLUMN],
        ).fetchall()
        return [str(row[0]) for row in rows]
    if dialect == "clickhouse":
        found = []
        for name, create in engine._cx.query(
            "SELECT name, create_table_query FROM system.tables WHERE database=currentDatabase()"
        ).result_rows:
            if name in ("sde_map_state", "sde_backfill_state", "__sde_fence_drains"):
                found.append(name)
            found.extend(f"{name}.{m}" for m in re.findall(r"CONSTRAINT `?(__sde_f_\w+)", create))
            if EPOCH_COLUMN in create:
                found.append(f"{name}.{EPOCH_COLUMN}")
        return sorted(found)
    raise AssertionError(dialect)


@pytest.mark.parametrize("dialect", ALL)
def test_a_plain_write_is_refused_under_the_fence_and_accepted_after_the_listed_release(
    dialect: str,
) -> None:
    with _namespace(dialect) as (engine, table):
        model, placement = _signed(dialect, table)
        sde.prepare_schema(model, placement, {"db": engine}, project_id=PROJECT)
        session = sde.Session(model, placement, {"db": engine}, project_id=PROJECT)
        session.save("Reading", {"id": 1, "station": "WAW"})

        # The control: our fence is real, and it refuses the client's own code.
        assert not _plain_insert(dialect, engine, table, 2)

        listed = sde.departure(placement, {"db": engine}, project_id=PROJECT)
        assert listed.ready
        (only,) = listed.engines
        kinds = {(finding.kind, finding.name) for finding in only.findings}
        assert ("fence", table) in kinds
        assert ("bookkeeping", "sde_map_state") in kinds
        assert only.release

        _as_text(dialect, engine, only.release)
        assert _plain_insert(dialect, engine, table, 2)
        assert _count(dialect, engine, table) == 2
        # And the library itself stops here: a session of this map would write unfenced now, so
        # it refuses to open rather than write beside the client's own code.
        with pytest.raises(sde.MigrationRefused, match="generation for Reading is not active"):
            sde.Session(model, placement, {"db": engine}, project_id=PROJECT)

        # Released, the fence is gone and the generation column is the only thing left in the
        # table; the cleanup removes it where the engine can, and the bookkeeping with it.
        released = sde.departure(placement, {"db": engine}, project_id=PROJECT).engines[0]
        assert released.release == ()
        _as_text(dialect, engine, released.cleanup)
        after = sde.departure(placement, {"db": engine}, project_id=PROJECT).engines[0]
        assert (after.release, after.cleanup, after.refusals) == ((), (), ())
        if dialect == "elasticsearch":
            # A mapped field stays until a reindex, and is said to stay. The cluster is the
            # namespace here, shared with every other live test: what they left is theirs.
            mine = [(f.kind, f.name) for f in after.findings if f.kind != "foreign"]
            shared = any(f.kind == "foreign" for f in after.findings)
            status, _ = engine._request("GET", f"/_ingest/pipeline/{FENCE_PREFIX}{table}")
            assert int(status) == 404
            status, _ = engine._request("GET", "/sde_map_state/_settings")
            if shared:
                assert ("field", table) in mine and int(status) == 200
            else:
                assert mine == [("field", table)] and int(status) == 404
        else:
            assert after.findings == ()
            assert _ours_in_catalogue(dialect, engine) == []
        # Nothing of the client's went with it.
        assert _count(dialect, engine, table) == 2
        assert _plain_insert(dialect, engine, table, 3)


@pytest.mark.parametrize("dialect", ALL)
def test_a_table_held_by_an_operation_refuses_the_whole_release(dialect: str) -> None:
    with _namespace(dialect) as (engine, table):
        model, placement = _signed(dialect, table)
        sde.prepare_schema(model, placement, {"db": engine}, project_id=PROJECT)
        engine.write_fence(table, project_id=PROJECT).freeze(HOLD)

        listed = sde.departure(placement, {"db": engine}, project_id=PROJECT)
        assert not listed.ready
        (only,) = listed.engines
        assert only.release == ()
        assert ("held", table) in {(finding.kind, finding.name) for finding in only.findings}
        assert any(HOLD in reason and "sde-operator" in reason for reason in only.refusals)
        with pytest.raises(sde.MigrationRefused, match="holds a table"):
            listed.execute({"db": engine}, cleanup=True)
        # Refused means untouched: the barrier still closes the table.
        assert engine.write_fence(table, project_id=PROJECT).state().holds == (HOLD,)


@pytest.mark.parametrize("dialect", ALL)
def test_fenced_tables_outside_the_map_are_listed_and_never_released_or_dropped(
    dialect: str,
) -> None:
    """A source a move retired holds rows; so does another project's table. Neither is ours to free.

    Both stay closed to a plain write after the release and the cleanup have run, with their rows.
    """
    with _namespace(dialect) as (engine, table):
        model, placement = _signed(dialect, table)
        sde.prepare_schema(model, placement, {"db": engine}, project_id=PROJECT)
        retired, theirs = table + "_retired", table + "_theirs"
        for name, project in ((retired, PROJECT), (theirs, OTHER)):
            _, other = _signed(dialect, name, project=project)
            layout = other.groups["Reading"].source.layout
            engine.ensure_schema(layout, keys={"Reading": ["id"]})
            engine.write_fence(name, project_id=project).prepare(1)
            engine.insert(name, {"id": 7, "station": "GDN", EPOCH_COLUMN: 1})

        listed = sde.departure(placement, {"db": engine}, project_id=PROJECT)
        (only,) = listed.engines
        kinds = {(finding.kind, finding.name) for finding in only.findings}
        assert {("outside", retired), ("foreign", theirs), ("fence", table)} <= kinds
        everything = " ".join(only.release + only.cleanup)
        assert retired not in everything and theirs not in everything
        # Another project writes in this namespace, so the bookkeeping is shared, and kept.
        assert "sde_map_state" not in everything
        kept = [f for f in only.findings if (f.kind, f.name) == ("bookkeeping", "sde_map_state")]
        assert kept and "kept" in kept[0].detail

        listed.execute({"db": engine}, cleanup=True)
        for name in (retired, theirs):
            assert not _plain_insert(dialect, engine, name, 8)
            assert _count(dialect, engine, name) == 1
        assert _plain_insert(dialect, engine, table, 9)


@pytest.mark.parametrize("dialect", ALL)
def test_execute_runs_what_was_listed_and_refuses_an_engine_that_changed_since(
    dialect: str,
) -> None:
    with _namespace(dialect) as (engine, table):
        model, placement = _signed(dialect, table)
        sde.prepare_schema(model, placement, {"db": engine}, project_id=PROJECT)
        listed = sde.departure(placement, {"db": engine}, project_id=PROJECT)
        assert any("sde_map_state" in statement for statement in listed.engines[0].cleanup)

        # A barrier taken after the listing refuses it, and leaves the table as it was.
        fence = engine.write_fence(table, project_id=PROJECT)
        fence.freeze(HOLD)
        with pytest.raises(sde.MigrationRefused, match="holds a table"):
            listed.execute({"db": engine})
        assert not _plain_insert(dialect, engine, table, 4)
        fence.release(HOLD)

        # The bookkeeping gone after the listing: what was read is no longer what is there, so
        # the listing is refused as a whole rather than run in part.
        _as_text(dialect, engine, (bookkeeping_statement(dialect, "sde_map_state"),))
        with pytest.raises(sde.MigrationRefused, match="changed since"):
            listed.execute({"db": engine}, cleanup=True)
        assert not _plain_insert(dialect, engine, table, 4)

        after = sde.departure(placement, {"db": engine}, project_id=PROJECT).execute(
            {"db": engine}
        )
        assert after.ready and after.engines[0].release == ()
        assert _plain_insert(dialect, engine, table, 4)


@pytest.mark.parametrize("dialect", ["clickhouse"])
def test_a_table_a_drain_left_detached_refuses_the_release(
    dialect: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ClickHouse drains by DETACH and ATTACH. A drain cut between the two leaves the table
    detached, recorded in ``__sde_fence_drains``: only the operator's resumption attaches it, so
    leaving waits for that - and the log that resumption reads is not dropped under it."""
    with _namespace(dialect) as (engine, table):
        model, placement = _signed(dialect, table)
        sde.prepare_schema(model, placement, {"db": engine}, project_id=PROJECT)
        fence = engine.write_fence(table, project_id=PROJECT)
        backend = fence._backend
        original = backend._command

        def interrupted(statement: str) -> None:
            if statement.startswith("ATTACH TABLE"):
                raise sde.EngineError("operator stopped after a confirmed DETACH")
            original(statement)

        monkeypatch.setattr(backend, "_command", interrupted)
        with pytest.raises(sde.EngineError):
            fence.freeze(HOLD)
        monkeypatch.undo()

        listed = sde.departure(placement, {"db": engine}, project_id=PROJECT)
        (only,) = listed.engines
        assert ("detached", table) in {(finding.kind, finding.name) for finding in only.findings}
        assert not listed.ready
        assert any("resume" in reason for reason in only.refusals)


def test_an_elasticsearch_fence_pipeline_whose_index_is_gone_is_cleaned_up() -> None:
    with _namespace("elasticsearch") as (engine, table):
        model, placement = _signed("elasticsearch", table)
        sde.prepare_schema(model, placement, {"db": engine}, project_id=PROJECT)
        engine._request("PUT", f"/{table}/_settings", {"index": {"final_pipeline": None}})
        engine._request("DELETE", f"/{table}")

        listed = sde.departure(placement, {"db": engine}, project_id=PROJECT)
        (only,) = listed.engines
        pipeline = FENCE_PREFIX + table
        assert ("pipeline", pipeline) in {(f.kind, f.name) for f in only.findings}
        assert f"DELETE /_ingest/pipeline/{pipeline}" in only.cleanup
        _as_text("elasticsearch", engine, only.cleanup)
        status, _ = engine._request("GET", f"/_ingest/pipeline/{pipeline}")
        assert int(status) == 404

"""The library's own scans use the engines' indexes - asked of each engine's planner.

Found in the general test on 2 October 2026: an agent decided a B-tree on ``Order(account)``, the
operator built it in place under traffic, and the library's scans by ``account`` took 131 ms before
it and 131 ms after it. On PostgreSQL every scan, count and summary renders a text column as
``(col COLLATE "C")`` - code-point comparison, which the format contract asks for - while the
library created text columns, and so every index on them, primary keys included, with the default
collation. The planner matches collations by identity, so no such index could serve the library.
On ClickHouse a ``uuid`` equality was ``toString(col) = …``, which the primary key cannot prune.

Each test runs the scan through ``Session``, records the exact statement and parameters the adapter
sent, and asks the engine for that statement's plan. On PostgreSQL ``enable_seqscan`` is off, and
the claim is the predicate as an index's condition, not merely an index in the plan. On ClickHouse
``EXPLAIN indexes = 1`` reports the granules the primary key kept.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest

import sde
import sde.engines.clickhouse as clickhouse_module
import sde.engines.postgres as postgres_module
from sde.engines._index_build import NativeIndexBuild
from sde.engines._operator import NativeOperator
from sde.engines.clickhouse import ClickHouseEngine
from sde.engines.postgres import PostgresEngine
from sde.testing.loader import model_from_neutral

POSTGRES = os.environ.get("SDE_POSTGRES_DSN")
CLICKHOUSE = os.environ.get("SDE_CLICKHOUSE_DSN")
pytestmark = pytest.mark.skipif(
    not (POSTGRES and CLICKHOUSE), reason="set SDE_POSTGRES_DSN and SDE_CLICKHOUSE_DSN"
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def _model() -> sde.LogicalModel:
    sde.clear_registry()
    return model_from_neutral(
        {
            "entities": [
                {
                    "name": "Reading",
                    "fields": [
                        {"name": "at", "type": "timestamptz"},
                        {"name": "celsius", "type": "int32"},
                        {"name": "sensor", "type": "string"},
                        {"name": "station", "type": "string"},
                    ],
                    "key": ["station", "at"],
                },
                {
                    "name": "Order",
                    "fields": [
                        {"name": "account", "type": "string"},
                        {"name": "id", "type": "uuid"},
                    ],
                    "key": ["id"],
                },
            ]
        }
    )


def _placement(model: sde.LogicalModel, dialect: str, indexes: list[dict[str, Any]]) -> Any:
    groups: dict[str, Any] = {}
    for group in sde.colocation_groups(model):
        layout = sde.default_layout(model, group, dialect=dialect)
        raw: dict[str, Any] = {
            "tables": dict(layout.tables),
            "columns": {entity: dict(cols) for entity, cols in layout.columns.items()},
        }
        mine = [index for index in indexes if index["entity"] in group.members]
        if mine:
            raw["indexes"] = mine
        groups[group.name] = {"source": {"id": f"{group.name}@e", "engine": "e", "layout": raw}}
    # Contract 3: indexes without a method are B-trees, and no write generation is needed for a
    # question about the planner.
    document = {"contract": 3, "model_version": model.version, "map_version": 1, "groups": groups}
    return sde.load_map(document, model=model)


def _spy(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[tuple[str, list[Any]]]:
    """Every read statement an adapter renders, with the values it binds, in order."""
    calls: list[tuple[str, list[Any]]] = []
    real = module.read_sql

    def spy(table: str, plan: Any, *, dialect: str, parameter: Any, **kwargs: Any) -> str:
        values: list[Any] = []

        def bind(value: Any) -> str:
            values.append(value)
            return str(parameter(value))

        statement = str(real(table, plan, dialect=dialect, parameter=bind, **kwargs))
        calls.append((statement, values))
        return statement

    monkeypatch.setattr(module, "read_sql", spy)
    return calls


# --- PostgreSQL --------------------------------------------------------------------------------


@pytest.fixture
def pg_schema() -> Iterator[str]:
    assert POSTGRES
    name = "sde_index_use_" + uuid.uuid4().hex[:10]
    with PostgresEngine(POSTGRES) as admin:
        admin._cx.execute(f'CREATE SCHEMA "{name}"')
        try:
            yield name
        finally:
            admin._cx.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


def _pg_dsn(schema: str) -> str:
    assert POSTGRES
    separator = "&" if "?" in POSTGRES else "?"
    return f"{POSTGRES}{separator}options=-csearch_path%3D{schema}"


def _pg_scans(engine: PostgresEngine, statement: str, values: list[Any]) -> list[str]:
    """The plan's scans, ``Node Type[:index][ on condition]``, with ``enable_seqscan`` off.

    With sequential scans off a planner that cannot use an index for the predicate still reads
    one - whole, filtering every entry - so an index's name in the plan proves nothing. What
    proves the index serves the read is the predicate as its ``Index Cond``.
    """
    with engine._cx.cursor() as cursor:
        cursor.execute("SET enable_seqscan = off")
        cursor.execute("EXPLAIN (FORMAT JSON) " + statement, values)
        row = cursor.fetchone()
        cursor.execute("RESET enable_seqscan")
    assert row is not None
    nodes: list[str] = []

    def walk(node: dict[str, Any]) -> None:
        if "Scan" in node["Node Type"]:
            label = node["Node Type"] + (":" + node["Index Name"] if "Index Name" in node else "")
            nodes.append(label + (" on condition" if node.get("Index Cond") else ""))
        for child in node.get("Plans", ()):
            walk(child)

    walk(row[0][0]["Plan"])
    return nodes


def _fill(session: sde.Session) -> None:
    session.save_many(
        "Reading",
        [
            {"station": f"st-{n % 50:02d}", "at": START + timedelta(seconds=n),
             "sensor": f"sn-{n % 400:03d}", "celsius": n % 40}
            for n in range(1_000)
        ],
    )


def test_a_scan_by_a_text_key_prefix_uses_the_primary_key(
    pg_schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _model()
    placement = _placement(model, "postgres", [])
    with PostgresEngine(_pg_dsn(pg_schema)) as engine:
        session = sde.Session(model, placement, {"e": engine})
        session.ensure_schema()
        _fill(session)
        calls = _spy(monkeypatch, postgres_module)
        page = session.scan("Reading", where={"station": "st-07"}, limit=5)
        assert [row["station"] for row in page.rows] == ["st-07"] * 5
        scans = _pg_scans(engine, *calls[-1])
    # An index or a bitmap index scan is the planner's choice; the primary key with the predicate
    # as its condition, and no sequential scan, is the claim.
    assert any(scan.endswith(":reading_pkey on condition") for scan in scans), scans
    assert "Seq Scan" not in scans, scans


def test_a_scan_by_a_designed_index_on_a_text_column_uses_it(
    pg_schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _model()
    index = {"entity": "Reading", "name": "reading_sensor_btree", "columns": ["sensor"]}
    placement = _placement(model, "postgres", [index])
    with PostgresEngine(_pg_dsn(pg_schema)) as engine:
        session = sde.Session(model, placement, {"e": engine})
        session.ensure_schema()
        _fill(session)
        calls = _spy(monkeypatch, postgres_module)
        assert session.count("Reading", where={"sensor": "sn-123"}) == 3
        scans = _pg_scans(engine, *calls[-1])
    assert any(scan.endswith(":reading_sensor_btree on condition") for scan in scans), scans


# The DDL of SDK 0.1.x, before text columns had a collation of their own.
LEGACY_READING = (
    'CREATE TABLE "reading" ("at" timestamptz, "celsius" integer, "sensor" text, '
    '"station" text, PRIMARY KEY ("station", "at"))'
)

# A name only an in-place build creates, as a signed packet binds it.
BOUND = "sde_i_" + "a" * 32 + "_000001"


@contextmanager
def _builder(engine: PostgresEngine, schema: str) -> Iterator[NativeIndexBuild]:
    """The operator's own index builder on ``engine``, with the runtime probe it requires."""
    with PostgresEngine(_pg_dsn(schema)) as probe:
        yield NativeIndexBuild(NativeOperator(engine, [probe]))


def _pg_index(engine: PostgresEngine, name: str) -> tuple[str, list[str]]:
    """An index's object id and its columns' collations, read from the catalogue.

    A column of a type without collation has none, the empty string here.
    """
    row = engine._cx.execute(
        "SELECT i.indexrelid::text, ARRAY(SELECT coalesce(co.collname, '') "
        "FROM unnest(i.indcollation) WITH ORDINALITY k(oid, pos) "
        "LEFT JOIN pg_collation co ON co.oid = k.oid ORDER BY k.pos) "
        "FROM pg_index i WHERE i.indexrelid = to_regclass(%s)",
        ('"' + name + '"',),
    ).fetchone()
    assert row is not None, name
    return str(row[0]), list(row[1])


def test_an_index_built_in_place_on_a_table_from_before_serves_the_scan(
    pg_schema: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A table created by an earlier library: its text columns have the default collation.

    The index the operator's in-place build adds still serves the library's scans, because the
    build renders the column with the collation the scans use. The table's primary key cannot, and
    the session logs it with the remedy - not a finding, which would refuse provisioning and the
    build itself. An index the client added outside SDE is not named: the map does not declare it.
    """
    model = _model()
    index = {"entity": "Reading", "name": BOUND, "columns": ["sensor"]}
    placement = _placement(model, "postgres", [index])
    layout = placement.placement_of("Reading").source.layout
    with PostgresEngine(_pg_dsn(pg_schema)) as engine:
        engine._cx.execute(LEGACY_READING)
        engine._cx.execute('CREATE INDEX "client_station" ON "reading" ("station")')
        with _builder(engine, pg_schema) as builder:
            builder.build(builder.native.identity("reading"), index, layout.columns["Reading"])
        assert _pg_index(engine, BOUND)[1] == ["C"]
        with caplog.at_level(logging.INFO, logger="sde"):
            session = sde.Session(model, placement, {"e": engine})
            session.ensure_schema()
        assert session.physical == (), session.physical
        logged = [
            record.__dict__["sde_fields"]
            for record in caplog.records
            if record.__dict__.get("sde_event") == "sde.schema.text_collation"
        ]
        # Once per check: the session's opening and ensure_schema each read the catalogue.
        assert logged, "the table's primary key was not named"
        assert {(entry["index"], tuple(entry["columns"])) for entry in logged} == {
            ("reading_pkey", ("station",))
        }, logged
        _fill(session)
        calls = _spy(monkeypatch, postgres_module)
        assert session.count("Reading", where={"sensor": "sn-123"}) == 3
        scans = _pg_scans(engine, *calls[-1])
    assert any(scan.endswith(":" + BOUND + " on condition") for scan in scans), scans


def test_our_index_an_earlier_library_built_is_built_again(pg_schema: str) -> None:
    """A build resumed after an upgrade finds its bound index ready, without the reads' collation.

    A library from before 2 October 2026 built it for this very map, so it is ours, and it is no
    use to a read: the build drops it concurrently and builds it again. One this library built is
    kept - the control, by object id.
    """
    model = _model()
    index = {"entity": "Reading", "name": BOUND, "columns": ["sensor", "celsius"]}
    placement = _placement(model, "postgres", [index])
    layout = placement.placement_of("Reading").source.layout
    with PostgresEngine(_pg_dsn(pg_schema)) as engine:
        engine._cx.execute(LEGACY_READING)
        # What the build of SDK 0.1.x ran.
        engine._cx.execute(
            f'CREATE INDEX CONCURRENTLY "{BOUND}" ON "reading" ("sensor", "celsius")'
        )
        before = _pg_index(engine, BOUND)
        assert before[1] == ["default", ""]
        with _builder(engine, pg_schema) as builder:
            table = builder.native.identity("reading")
            assert builder.status(table, index) == "ready"
            builder.build(table, index, layout.columns["Reading"])
            rebuilt = _pg_index(engine, BOUND)
            builder.build(table, index, layout.columns["Reading"])
            kept = _pg_index(engine, BOUND)
    assert rebuilt[0] != before[0], "the index of the earlier library was adopted"
    assert rebuilt[1] == ["C", ""], rebuilt
    assert kept == rebuilt, "an index this library built was built again"


# --- ClickHouse --------------------------------------------------------------------------------


@pytest.fixture
def ch_database() -> Iterator[str]:
    assert CLICKHOUSE
    name = "sde_index_use_" + uuid.uuid4().hex[:10]
    with ClickHouseEngine(CLICKHOUSE) as admin:
        admin._cx.command(f"CREATE DATABASE {name}")
        try:
            yield name
        finally:
            admin._cx.command(f"DROP DATABASE IF EXISTS {name} SYNC")


def test_a_clickhouse_scan_by_a_uuid_key_prunes_granules(
    ch_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert CLICKHOUSE
    parts = urlsplit(CLICKHOUSE)
    dsn = urlunsplit((parts.scheme, parts.netloc, "/" + ch_database, parts.query, parts.fragment))
    model = _model()
    placement = _placement(model, "clickhouse", [])
    ids = [uuid.UUID(int=(n * 2_654_435_761) % (1 << 128)) for n in range(1, 40_001)]
    with ClickHouseEngine(dsn) as engine:
        session = sde.Session(model, placement, {"e": engine})
        session.ensure_schema()
        for start in range(0, len(ids), 1_000):
            session.save_many(
                "Order", [{"id": i, "account": f"a-{n % 97}"} for n, i in
                          enumerate(ids[start:start + 1_000], start)]
            )
        engine._cx.command("OPTIMIZE TABLE `order` FINAL")
        calls = _spy(monkeypatch, clickhouse_module)
        page = session.scan("Order", where={"id": ids[12_345]}, limit=5)
        assert [row["id"] for row in page.rows] == [ids[12_345]]
        statement, values = calls[-1]
        # The adapter names its parameters read_0, read_1, ... in the order it binds them.
        parameters = {f"read_{position}": value for position, value in enumerate(values)}
        assert set(re.findall(r"%\((\w+)\)s", statement)) == set(parameters), statement
        explained = engine._cx.query(
            "EXPLAIN indexes = 1 " + statement, parameters=parameters
        ).result_rows
    text = "\n".join(str(row[0]) for row in explained)
    granules = re.findall(r"Granules: (\d+)/(\d+)", text)
    assert granules, text
    kept, total = (int(value) for value in granules[0])
    assert total > 1, text
    assert kept == 1, text

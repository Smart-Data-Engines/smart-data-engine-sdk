"""The demo executes only a current, approved COUNT and keeps its business value local."""

from __future__ import annotations

import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from _weather_fixture import supplied

import sde
from sde.schema import QUOTE
from sde_demo import project, query_count, resources, runtime
from sde_demo.model import model, reading


def packet(
    dialect: str = "postgres", bundle: dict[str, Any] | None = None
) -> tuple[dict[str, Any], Any, dict[str, Any]]:
    if bundle is None:
        bundle, _ = supplied(dialect)
    _, placement = project.bootstrap(bundle)
    source = placement.groups["WeatherReading"].source
    sql = "SELECT COUNT(*) AS total FROM " + QUOTE[dialect](
        source.layout.table_for("WeatherReading")
    )
    if dialect == "clickhouse":
        sql += " FINAL"
    record = {
        "sql": sql,
        "group": "WeatherReading",
        "engine": source.engine,
        "materialization": source.id,
        "dialect": dialect,
        "stamp": {
            "model_version": placement.model_version,
            "map_version": placement.map_version,
            "materialization": source.id,
        },
        "reasoning": "counts logical observations",
    }
    result = query_count.make_count_request(
        project_id=bundle["project_id"],
        name="weather-count",
        revision=1,
        query=record,
        placement=placement,
        dialect=dialect,
    )
    return result, placement, bundle


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_request_binds_exact_query_to_source_and_map(dialect: str) -> None:
    document, placement, bundle = packet(dialect)
    loaded = query_count.load_count_request(
        document, project_id=bundle["project_id"], placement=placement, dialects=bundle["engines"]
    )
    assert loaded.sql == document["query"]["sql"]
    document["query"]["sql"] = "untrusted later mutation"
    assert loaded.sql != document["query"]["sql"]
    assert loaded.map_fingerprint == placement.fingerprint


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT COUNT(*) FROM "weather_reading"',
        " select count ( * ) as total FROM weather_reading ; ",
        'SELECT COUNT(*) AS "Total"\nFROM "weather_reading";',
    ],
)
def test_postgres_count_grammar_preserves_the_reviewed_text(sql: str) -> None:
    assert query_count.count_sql(sql, table="weather_reading", dialect="postgres") == sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) FROM `weather_reading` FINAL",
        "SELECT count(*) AS `total` FROM weather_reading FINAL;",
        'select Count(*) from "weather_reading" final',
    ],
)
def test_clickhouse_count_requires_logical_final(sql: str) -> None:
    assert query_count.count_sql(sql, table="weather_reading", dialect="clickhouse") == sql


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) FROM other_table",
        'SELECT COUNT(*) FROM "WEATHER_READING"',
        "SELECT COUNT(1) FROM weather_reading",
        "SELECT COUNT(*) FROM weather_reading WHERE 1=1",
        "SELECT COUNT(*) FROM weather_reading; SELECT 1",
        "SELECT COUNT(*) FROM weather_reading; DROP TABLE weather_reading",
        "SELECT COUNT(*) FROM weather_reading SETTINGS max_execution_time=0",
        "SELECT COUNT(*) FROM url('https://example.invalid')",
        "SELECT COUNT(*) FROM weather_reading UNION ALL SELECT 1",
        "WITH x AS (DELETE FROM weather_reading RETURNING *) SELECT COUNT(*) FROM x",
        "SELECT COUNT(*) FROM weather_reading -- comment",
        "SELECT COUNT(*), current_user FROM weather_reading",
    ],
)
def test_other_sql_is_refused_by_the_closed_count_grammar(dialect: str, sql: str) -> None:
    with pytest.raises(project.DemoRefused):
        query_count.count_sql(sql, table="weather_reading", dialect=dialect)


def test_missing_final_is_not_accepted_on_clickhouse() -> None:
    with pytest.raises(project.DemoRefused, match="FINAL"):
        query_count.count_sql(
            "SELECT COUNT(*) FROM weather_reading", table="weather_reading", dialect="clickhouse"
        )


@pytest.mark.parametrize(
    "change",
    [
        "project",
        "fingerprint",
        "digest",
        "revision",
        "engine",
        "materialization",
        "stamp_version",
        "stamp_model",
        "sql",
    ],
)
def test_changed_handoff_never_becomes_a_count_request(change: str) -> None:
    document, placement, bundle = packet()
    value = deepcopy(document)
    if change == "project":
        value["project_id"] = "f" * 32
    elif change == "fingerprint":
        value["map_fingerprint"] = "e" * 64
    elif change == "digest":
        value["digest"] = "0" * 64
    elif change == "revision":
        value["revision"] = True
    elif change == "engine":
        value["query"]["engine"] = "elsewhere"
    elif change == "materialization":
        value["query"]["materialization"] = "elsewhere"
    elif change == "stamp_version":
        value["query"]["stamp"]["map_version"] = True
    elif change == "stamp_model":
        value["query"]["stamp"]["model_version"] = "0" * 16
    else:
        value["query"]["sql"] = "SELECT COUNT(*) FROM unrelated"
    if change != "digest":
        value["digest"] = query_count._digest(value)
    with pytest.raises(project.DemoRefused):
        query_count.load_count_request(
            value, project_id=bundle["project_id"], placement=placement, dialects=bundle["engines"]
        )


def test_metadata_builder_imports_no_driver() -> None:
    script = """
import sys
from sde_demo.query_count import make_count_request
assert not any(name in sys.modules for name in ('psycopg', 'clickhouse_connect', 'sde.engines'))
"""
    subprocess.run([sys.executable, "-c", script], check=True, env=dict(os.environ))


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_actual_count_and_business_result_stay_in_the_customer_directory(
    dialect: str, tmp_path: Path
) -> None:
    admin = {
        name: os.environ.get(variable, "")
        for name, variable in (
            ("postgres", "SDE_POSTGRES_DSN"),
            ("clickhouse", "SDE_CLICKHOUSE_DSN"),
        )
    }
    if not all(admin.values()):
        pytest.skip("both native engines are required for the Weather count demo")
    root = tmp_path / "weather"
    document, _, bundle = packet(dialect)
    try:
        project.setup(root, bundle, admin)
        run = runtime.run(root, iterations=2, batch_size=3, interval_ms=0)
        private_credentials = root / "operator-credentials.json"
        held = root / "operator.held"
        private_credentials.rename(held)
        try:
            receipt = query_count.run_count_query(root, document)
        finally:
            held.rename(private_credentials)
        assert receipt["verified"] is True
        assert receipt["run_ids"] == [run["run_id"]]
        assert set(receipt) == {
            "protocol",
            "kind",
            "execution_id",
            "project_id",
            "map_fingerprint",
            "query_name",
            "query_revision",
            "query_digest",
            "verified",
            "run_ids",
            "observed_at",
        }
        result_file = root / "query-results" / receipt["execution_id"] / "result.json"
        assert project.read(result_file)["count"] == 6
        assert result_file.stat().st_mode & 0o777 == 0o600
        settings = project.config(root)
        with project.connections(root, settings) as (_, runtime_engines):
            placement = sde.load_local_map(
                root / "state",
                model=model(),
                project_id=settings["project_id"],
                public_key=project.public_keys(settings["public_keys"]),
            )
            with sde.Session(
                model(),
                placement,
                {dialect: runtime_engines[dialect]},
                project_id=settings["project_id"],
            ) as app:
                app.save("WeatherReading", reading("f" * 32, 0, 1))
        with pytest.raises(project.DemoRefused, match="differs"):
            query_count.run_count_query(root, document)
    finally:
        if (root / "resources.json").exists():
            resources.reset(root, admin)


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
@pytest.mark.parametrize("change", ["map", "report"])
def test_no_query_receipt_survives_a_context_change_during_execution(
    dialect: str, change: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = {
        name: os.environ.get(variable, "")
        for name, variable in (
            ("postgres", "SDE_POSTGRES_DSN"),
            ("clickhouse", "SDE_CLICKHOUSE_DSN"),
        )
    }
    if not all(admin.values()):
        pytest.skip("both native engines are required for the Weather count demo")
    root = tmp_path / "weather"
    bundle, sign = supplied(dialect)
    document, _, _ = packet(dialect, bundle)
    try:
        project.setup(root, bundle, admin)
        run = runtime.run(root, iterations=1, batch_size=2, interval_ms=0)
        path = (
            root / "state" / "active-map.json"
            if change == "map"
            else root / "runs" / run["run_id"] / "report.json"
        )
        original_bytes = path.read_bytes()
        counted = query_count._count

        def changed(adapter: Any, sql: str) -> int:
            result = counted(adapter, sql)
            value = project.read(path)
            if change == "map":
                value["map_version"] += 1
                value = sign(value)
            else:
                value["unexpected_note"] = "controlled report change"
            project.write(path, value)
            return result

        try:
            with monkeypatch.context() as alteration:
                alteration.setattr(query_count, "_count", changed)
                with pytest.raises(project.DemoRefused, match="changed"):
                    query_count.run_count_query(root, document)
            assert not (root / "query-results").exists()
        finally:
            path.write_bytes(original_bytes)
        assert query_count.run_count_query(root, document)["verified"] is True
    finally:
        if (root / "resources.json").exists():
            resources.reset(root, admin)


@pytest.mark.parametrize(
    ("table", "spelling"),
    [
        ("weather_reading", "weather_read\u0131ng"),
        ("weather_reading", "weather_read\u0130ng"),
        ("samples", "\u017famples"),
        ("keys", "\u212aeys"),
    ],
)
def test_unicode_case_folding_cannot_choose_another_postgres_identifier(
    table: str, spelling: str
) -> None:
    with pytest.raises(project.DemoRefused):
        query_count.count_sql(f"SELECT COUNT(*) FROM {spelling}", table=table, dialect="postgres")


def test_sql_keywords_use_ascii_case_folding() -> None:
    with pytest.raises(project.DemoRefused):
        query_count.count_sql(
            "\u017fELECT COUNT(*) FROM weather_reading", table="weather_reading", dialect="postgres"
        )


def test_postgres_homograph_cannot_count_another_accessible_table(tmp_path: Path) -> None:
    admin = {
        name: os.environ.get(variable, "")
        for name, variable in (
            ("postgres", "SDE_POSTGRES_DSN"),
            ("clickhouse", "SDE_CLICKHOUSE_DSN"),
        )
    }
    if not all(admin.values()):
        pytest.skip("both native engines are required for the Weather count demo")
    from psycopg import sql

    root = tmp_path / "weather"
    document, placement, bundle = packet("postgres")
    source_name = placement.groups["WeatherReading"].source.layout.table_for("WeatherReading")
    homograph = source_name.replace("i", "\u0131")
    assert homograph != source_name
    try:
        project.setup(root, bundle, admin)
        runtime.run(root, iterations=1, batch_size=2, interval_ms=0)
        metadata = project.read(root / "resources.json")
        principal = metadata["engines"]["postgres"]["runtime_user"]
        with project.connections(root, project.config(root)) as (operators, _):
            native = operators["postgres"]._cx
            native.execute(sql.SQL("CREATE TABLE {} (id bigint)").format(sql.Identifier(homograph)))
            native.execute(
                sql.SQL("INSERT INTO {} VALUES (1),(2)").format(sql.Identifier(homograph))
            )
            native.execute(
                sql.SQL("GRANT SELECT ON TABLE {} TO {}").format(
                    sql.Identifier(homograph), sql.Identifier(principal)
                )
            )
            names = native.execute(
                "SELECT to_regclass(%s)::oid,to_regclass(%s)::oid", [source_name, homograph]
            ).fetchone()
            assert names is not None and names[0] != names[1]
        changed = deepcopy(document)
        changed["query"]["sql"] = "SELECT COUNT(*) FROM " + homograph
        changed["digest"] = query_count._digest(changed)
        with pytest.raises(project.DemoRefused, match="exact Weather source"):
            query_count.run_count_query(root, changed)
        assert not (root / "query-results").exists()
    finally:
        if (root / "resources.json").exists():
            resources.reset(root, admin)

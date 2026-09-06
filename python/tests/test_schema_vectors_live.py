"""Every statement in the ``schema/`` vectors, executed against a real engine, per case.

The other families' expectations are strings compared to strings, and that is enough for them: an
IR is bytes and a refusal is a message. A DDL statement is neither. It is an instruction to a
server, and a vector holding one no server accepts would be a frozen mistake that every future
implementation would be *required* to reproduce - the worst kind of artefact this repository can
hold, because the conformance suite's authority is exactly what makes it dangerous when wrong.

So this closes the loop the vectors cannot close by themselves. It is deliberately not a slice of
the adapters: it takes the statements out of the vector files, not out of the library, and runs
them. If the two ever disagree the adapters' own tests say so, and there are several of those.

It does not run the ``orderbook`` dialect, which renders no statements at all: there is nothing to
execute and the fixed-schema case is pinned by the vector's ``fixed`` flag.

**Two things changed here after a third implementation read the family, and both were about this
file rather than about the vectors.**

*One case per schema.* Every case used to share one schema in vector order, and that made the
coverage depend on the order: ``schema/002``'s second case renders a ClickHouse layout with the
``postgres`` dialect - deliberately, since that is what pins "the renderer never translates a
type" - and PostgreSQL refuses ``DateTime64`` outright. It passed anyway, because an earlier vector
had already created a table of the same name and ``CREATE TABLE IF NOT EXISTS`` never parsed the
body. A file whose docstring says it runs every statement had one that had never run. Cases now get
a schema each, and the one that cannot execute says ``runs: false`` in the vector.

*Accepted is not the same claim as created under the name we asked for.* Inside a backtick-quoted
identifier ClickHouse reads a backslash as an escape introducer, so ``CREATE TABLE `a\nb` `` is
accepted and creates a column called ``a``, a newline and ``b``. A wrong escaper therefore passed
this file. Each case's names are now read back out of the catalogue - ``information_schema.columns``
and ``system.columns`` - and compared with the layout the statements came from. That is the only
instrument that can tell the two dialects' escaping rules apart, and ``schema/011`` is the case
that needs it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, NamedTuple

import pytest

VECTORS = Path(__file__).resolve().parents[2] / "conformance" / "vectors" / "schema"

PG_DSN = os.environ.get("SDE_POSTGRES_DSN")
CH_DSN = os.environ.get("SDE_CLICKHOUSE_DSN")


class Runnable(NamedTuple):
    """One case's statements, and the table and column names they should leave behind."""

    case: str
    statements: tuple[str, ...]
    declared: dict[str, tuple[str, ...]]


def _layout_of(document: dict[str, Any], materialization: str) -> dict[str, Any]:
    for group in document["groups"].values():
        for candidate in [group["source"], *group.get("derived", ())]:
            if candidate["id"] == materialization:
                return dict(candidate["layout"])
    raise AssertionError(f"no materialisation called {materialization!r} in this map")


def _runnable(dialect: str) -> list[Runnable]:
    """Every case of one dialect that has statements a server can be asked to execute.

    Views included: a ``CREATE OR REPLACE VIEW`` that is a syntax error is exactly the defect this
    file exists for - PostgreSQL has no ``CREATE VIEW IF NOT EXISTS`` and the first draft of the
    renderer emitted one, which read correctly and did not run.
    """
    out: list[Runnable] = []
    for case in sorted(p for p in VECTORS.iterdir() if p.is_dir()):
        cases: list[dict[str, Any]] = json.loads((case / "cases.json").read_text())
        document = json.loads((case / "map.json").read_text())
        for expectation in cases:
            if expectation.get("dialect") != dialect:
                continue
            if expectation.get("runs") is False:
                continue
            statements = list(expectation.get("statements") or ())
            views = expectation.get("views") or {}
            statements += list(views.get("create") or ())
            statements += list(views.get("drop") or ())
            if not statements:
                continue
            layout = _layout_of(document, expectation["materialization"])
            columns = expectation.get("layout_columns", layout.get("columns", {}))
            out.append(
                Runnable(
                    case=case.name,
                    statements=tuple(statements),
                    declared={
                        table: tuple(sorted(columns.get(entity, {})))
                        for entity, table in layout.get("tables", {}).items()
                    },
                )
            )
    return out


def test_the_family_has_statements_for_both_dialects_we_can_reach() -> None:
    """Guards the guard. A test that runs zero statements passes for the wrong reason, and this
    file is the only place the vectors meet a server. The count is asserted per dialect *and* the
    one deliberately unrunnable case is asserted to be excluded, so a `runs: false` spreading
    quietly through the family would fail here rather than shrink the coverage in silence."""
    assert _runnable("postgres"), "no postgres statements in the schema vectors"
    assert _runnable("clickhouse"), "no clickhouse statements in the schema vectors"
    excluded = [
        expectation
        for case in sorted(p for p in VECTORS.iterdir() if p.is_dir())
        for expectation in json.loads((case / "cases.json").read_text())
        if expectation.get("runs") is False
    ]
    assert len(excluded) == 1, (
        f"{len(excluded)} cases say they cannot be executed. There is one - a ClickHouse layout "
        "rendered with the postgres dialect - and a second would be coverage leaving this file."
    )


def _missing(declared: dict[str, tuple[str, ...]], found: dict[str, set[str]]) -> str | None:
    """The first disagreement between what the layout declared and what the server made.

    Extra objects are ignored, which is the rule ``ensure_schema`` applies: a missing column is a
    refusal and a surplus one is a log line.
    """
    for table, columns in declared.items():
        if table not in found:
            return (
                f"there is no table called {table!r}. The server made {sorted(found)} - so the "
                f"statement was accepted and the name is not the one the layout declared, which "
                f"is what a wrong identifier escaper looks like"
            )
        absent = sorted(set(columns) - found[table])
        if absent:
            return f"{table!r} is missing {absent}; it has {sorted(found[table])}"
    return None


@pytest.mark.skipif(
    not PG_DSN,
    reason="set SDE_POSTGRES_DSN to run the schema vectors against PostgreSQL; skipped rather "
    "than faked, because a fake accepts whatever this library emits",
)
def test_every_postgres_case_runs_and_creates_the_names_it_declared() -> None:
    import psycopg

    assert PG_DSN is not None
    schema = "sde_schema_vectors"
    with (
        psycopg.connect(PG_DSN, autocommit=True) as connection,
        connection.cursor() as cursor,
    ):
        try:
            for runnable in _runnable("postgres"):
                # A schema per case, dropped between them. The vectors name tables like `order`
                # and `payment` in several cases, and a shared one let `IF NOT EXISTS` swallow a
                # statement whole - see the module docstring.
                cursor.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
                cursor.execute(f"CREATE SCHEMA {schema}")
                cursor.execute(f"SET search_path TO {schema}")
                # Twice, because every statement in this family claims to be idempotent - that is
                # why they are all IF NOT EXISTS or OR REPLACE - and an application restarting
                # reapplies them. A statement that is correct once and fails twice is a deployment
                # that works until the first restart.
                for _ in range(2):
                    for statement in runnable.statements:
                        try:
                            cursor.execute(statement)  # type: ignore[arg-type]
                        except Exception as exc:
                            raise AssertionError(
                                f"schema/{runnable.case}: PostgreSQL refused a statement this "
                                f"family says every implementation must produce.\n  {statement}"
                                f"\n  {exc}"
                            ) from exc
                found: dict[str, set[str]] = {}
                for table, column in cursor.execute(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = %s",
                    (schema,),
                ):
                    found.setdefault(table, set()).add(column)
                complaint = _missing(runnable.declared, found)
                assert complaint is None, f"schema/{runnable.case}: {complaint}"
        finally:
            cursor.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


@pytest.mark.skipif(
    not CH_DSN,
    reason="set SDE_CLICKHOUSE_DSN to run the schema vectors against ClickHouse; skipped rather "
    "than faked, because a fake accepts whatever this library emits",
)
def test_every_clickhouse_case_runs_and_creates_the_names_it_declared() -> None:
    import clickhouse_connect

    assert CH_DSN is not None
    client = clickhouse_connect.get_client(dsn=CH_DSN)
    database = "sde_schema_vectors"
    try:
        for runnable in _runnable("clickhouse"):
            client.command(f"DROP DATABASE IF EXISTS {database}")
            client.command(f"CREATE DATABASE {database}")
            scoped = clickhouse_connect.get_client(dsn=CH_DSN, database=database)
            try:
                for _ in range(2):  # idempotence, same argument as above
                    for statement in runnable.statements:
                        try:
                            scoped.command(statement)
                        except Exception as exc:
                            raise AssertionError(
                                f"schema/{runnable.case}: ClickHouse refused a statement this "
                                f"family says every implementation must produce.\n  {statement}"
                                f"\n  {exc}"
                            ) from exc
                found: dict[str, set[str]] = {}
                for table, column in scoped.query(
                    "SELECT table, name FROM system.columns WHERE database = %(db)s",
                    parameters={"db": database},
                ).result_rows:
                    found.setdefault(table, set()).add(column)
                complaint = _missing(runnable.declared, found)
                assert complaint is None, f"schema/{runnable.case}: {complaint}"
            finally:
                scoped.close()
    finally:
        client.command(f"DROP DATABASE IF EXISTS {database}")
        client.close()

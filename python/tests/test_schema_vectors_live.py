"""Every statement in the ``schema/`` vectors, executed against a real engine.

The other families' expectations are strings compared to strings, and that is enough for them: an
IR is bytes and a refusal is a message. A DDL statement is neither. It is an instruction to a
server, and a vector holding one no server accepts would be a frozen mistake that every future
implementation would be *required* to reproduce - the worst kind of artefact this repository can
hold, because the conformance suite's authority is exactly what makes it dangerous when wrong.

So this closes the loop the vectors cannot close by themselves. It is deliberately not a slice of
the adapters: it takes the statements out of the vector files, not out of the library, and runs
them. If the two ever disagree the adapters' own tests say so, and there are several of those.

Two things it does **not** do, both on purpose. It does not compare the resulting table to the
layout - ``ensure_schema`` verifies that after applying, and it has its own tests. And it does not
run the ``orderbook`` dialect, which renders no statements at all: there is nothing to execute and
the fixed-schema case is pinned by the vector's ``fixed`` flag.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

VECTORS = Path(__file__).resolve().parents[2] / "conformance" / "vectors" / "schema"

PG_DSN = os.environ.get("SDE_POSTGRES_DSN")
CH_DSN = os.environ.get("SDE_CLICKHOUSE_DSN")


def _statements(dialect: str) -> list[tuple[str, str]]:
    """Every statement the family expects for one dialect, with the case it came from.

    Views included: a ``CREATE OR REPLACE VIEW`` that is a syntax error is exactly the defect this
    file exists for - PostgreSQL has no ``CREATE VIEW IF NOT EXISTS`` and the first draft of the
    renderer emitted one, which read correctly and did not run.
    """
    out: list[tuple[str, str]] = []
    for case in sorted(p for p in VECTORS.iterdir() if p.is_dir()):
        cases: list[dict[str, Any]] = json.loads((case / "cases.json").read_text())
        for expectation in cases:
            if expectation.get("dialect") != dialect:
                continue
            for statement in expectation.get("statements") or ():
                out.append((case.name, statement))
            views = expectation.get("views") or {}
            for statement in views.get("create") or ():
                out.append((case.name, statement))
            for statement in views.get("drop") or ():
                out.append((case.name, statement))
    return out


def test_the_family_has_statements_for_both_dialects_we_can_reach() -> None:
    """Guards the guard. A test that runs zero statements passes for the wrong reason, and this
    file is the only place the vectors meet a server."""
    assert _statements("postgres"), "no postgres statements in the schema vectors"
    assert _statements("clickhouse"), "no clickhouse statements in the schema vectors"


@pytest.mark.skipif(
    not PG_DSN,
    reason="set SDE_POSTGRES_DSN to run the schema vectors against PostgreSQL; skipped rather "
    "than faked, because a fake accepts whatever this library emits",
)
def test_every_postgres_statement_the_vectors_expect_runs() -> None:
    import psycopg

    statements = _statements("postgres")
    assert PG_DSN is not None
    # A schema of its own, dropped at the end. The vectors name tables like `order` and `payment`,
    # which are exactly the names another test might be using, and a shared search_path would make
    # this file's failures depend on test ordering.
    with (
        psycopg.connect(PG_DSN, autocommit=True) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute("DROP SCHEMA IF EXISTS sde_schema_vectors CASCADE")
        cursor.execute("CREATE SCHEMA sde_schema_vectors")
        cursor.execute("SET search_path TO sde_schema_vectors")
        for case, statement in statements:
            try:
                cursor.execute(statement)  # type: ignore[arg-type]
            except Exception as exc:
                raise AssertionError(
                    f"schema/{case}: PostgreSQL refused a statement this family says every "
                    f"implementation must produce.\n  {statement}\n  {exc}"
                ) from exc
        # Twice, because every statement in this family claims to be idempotent - that is why
        # they are all IF NOT EXISTS or OR REPLACE - and an application restarting reapplies
        # them. A statement that is correct once and fails twice is a deployment that works
        # until the first restart.
        for case, statement in statements:
            try:
                cursor.execute(statement)  # type: ignore[arg-type]
            except Exception as exc:
                raise AssertionError(
                    f"schema/{case}: PostgreSQL accepted this statement once and refused it "
                    f"the second time, so it is not idempotent.\n  {statement}\n  {exc}"
                ) from exc
        cursor.execute("DROP SCHEMA sde_schema_vectors CASCADE")


@pytest.mark.skipif(
    not CH_DSN,
    reason="set SDE_CLICKHOUSE_DSN to run the schema vectors against ClickHouse; skipped rather "
    "than faked, because a fake accepts whatever this library emits",
)
def test_every_clickhouse_statement_the_vectors_expect_runs() -> None:
    import clickhouse_connect

    statements = _statements("clickhouse")
    assert CH_DSN is not None
    client = clickhouse_connect.get_client(dsn=CH_DSN)
    database = "sde_schema_vectors"
    try:
        client.command(f"DROP DATABASE IF EXISTS {database}")
        client.command(f"CREATE DATABASE {database}")
        scoped = clickhouse_connect.get_client(dsn=CH_DSN, database=database)
        try:
            for _ in range(2):  # idempotence, same argument as above
                for case, statement in statements:
                    try:
                        scoped.command(statement)
                    except Exception as exc:
                        raise AssertionError(
                            f"schema/{case}: ClickHouse refused a statement this family says "
                            f"every implementation must produce.\n  {statement}\n  {exc}"
                        ) from exc
        finally:
            scoped.close()
    finally:
        client.command(f"DROP DATABASE IF EXISTS {database}")
        client.close()

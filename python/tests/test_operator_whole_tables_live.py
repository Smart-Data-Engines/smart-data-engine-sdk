"""An operator does not read a table through a row policy, on the live engines.

PostgreSQL's row-level security and ClickHouse's row policies answer a login the rows a policy lets
it see, and no error. An operator that copies and compares a table would move what it was shown and
call the copy complete. Two things stand between it and that: it refuses a table that has a policy
at all, where it takes the table's identity, and its PostgreSQL connections are told to refuse a
read a policy would narrow, which is what holds if a policy appears after the identity was taken.
Each case shows its premise first - a login counting fewer rows than the table holds.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest

import sde
from sde.engines._operator import native_operator
from sde.engines.clickhouse import ClickHouseEngine
from sde.engines.postgres import PostgresEngine

ROWS = 100
SHOWN = 10


@contextmanager
def postgres_owner() -> Iterator[tuple[Any, Any, str]]:
    """A table of ``ROWS`` rows owned by a login that is no superuser: the administrator's
    connection, the owner's own, and the table's name. A superuser is shown every row whatever
    the policy, which is why the suites' usual operator cannot stand in here."""
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    dsn = os.environ.get("SDE_POSTGRES_DSN")
    if not dsn:
        pytest.skip("SDE_POSTGRES_DSN is required: this is about a live server's own policies")
    name = "sde_whole_" + uuid4().hex[:16]
    owner, password = name + "_op", secrets.token_hex(24)
    with PostgresEngine(dsn) as root:
        made = False
        try:
            root._cx.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(owner), sql.Literal(password)
                )
            )
            made = True
            root._cx.execute(
                sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                    sql.Identifier(name), sql.Identifier(owner)
                )
            )
            local = make_conninfo(
                dsn, user=owner, password=password, options=f"-csearch_path={name}"
            )
            with PostgresEngine(local) as mine:
                mine._cx.execute("CREATE TABLE events (id bigint PRIMARY KEY, value integer)")
                mine._cx.execute(
                    "INSERT INTO events SELECT g, g FROM generate_series(1, %s) g", [ROWS]
                )
                yield root, mine, local
        finally:
            root._cx.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name))
            )
            if made:
                root._cx.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(owner)))


def test_postgres_refuses_a_table_with_row_level_security_and_reads_none_through_it() -> None:
    with postgres_owner() as (_root, mine, local), PostgresEngine(local) as probe:
        operator = native_operator(mine, [probe])
        operator.identity("events")  # the control: no policy, and the table is taken
        mine._cx.execute("ALTER TABLE events ENABLE ROW LEVEL SECURITY")
        mine._cx.execute(f"CREATE POLICY few ON events USING (id <= {SHOWN})")
        # Not forced, the policy does not apply to the table's owner - who reads every row with
        # row_security off - and the table is refused all the same: a copy would not carry it.
        assert mine.count("events") == ROWS
        with pytest.raises(sde.MigrationRefused, match="without RLS"):
            operator.identity("events")

        mine._cx.execute("ALTER TABLE events FORCE ROW LEVEL SECURITY")
        # The premise, as a connection that has not been told otherwise sees it.
        assert probe.count("events") == SHOWN
        with pytest.raises(sde.MigrationRefused, match="without RLS"):
            operator.identity("events")
        # And had the identity been taken before the policy appeared: the operator's connection
        # reads no part of the table, where it was shown ten rows and no error.
        with pytest.raises(sde.EngineError, match="row-level security"):
            mine.count("events")
        with pytest.raises(sde.EngineError, match="row-level security"):
            mine.key_range("events", ["id"])


def test_postgres_turns_row_security_off_again_on_a_connection_opened_later() -> None:
    """The mode is the adapter's, not one connection's: an operator that reconnects is not back to
    reading what a policy shows it."""
    with postgres_owner() as (_root, mine, local):
        mine._cx.execute("ALTER TABLE events ENABLE ROW LEVEL SECURITY")
        mine._cx.execute("ALTER TABLE events FORCE ROW LEVEL SECURITY")
        mine._cx.execute(f"CREATE POLICY few ON events USING (id <= {SHOWN})")
        again = PostgresEngine(local)
        again.whole_answers()  # before it is connected
        again.connect()
        try:
            with pytest.raises(sde.EngineError, match="row-level security"):
                again.count("events")
            again.close()
            again.connect()
            with pytest.raises(sde.EngineError, match="row-level security"):
                again.count("events")
        finally:
            again.close()
        # A session's adapter is never told, and reads what its policy shows it: the client's own
        # decision about a runtime login.
        with PostgresEngine(local) as runtime:
            assert runtime.count("events") == SHOWN


def test_clickhouse_refuses_a_table_with_a_row_policy_whoever_it_names() -> None:
    """A policy for the operator's own login shows it some of the rows; one for another login
    shows it all of them and would still not be on the copy. Either refuses the table."""
    dsn = os.environ.get("SDE_CLICKHOUSE_DSN")
    if not dsn:
        pytest.skip("SDE_CLICKHOUSE_DSN is required: this is about a live server's own policies")
    name = "sde_whole_" + uuid4().hex[:16]
    parts = urlsplit(dsn)
    local = urlunsplit((parts.scheme, parts.netloc, "/" + name, parts.query, ""))
    with ClickHouseEngine(dsn) as root:
        try:
            root._cx.command(f"CREATE DATABASE {name} ENGINE=Atomic")
            root._cx.command(
                f"CREATE TABLE {name}.events (id Int64, value Int32) "
                f"ENGINE = ReplacingMergeTree ORDER BY (id)"
            )
            root._cx.command(
                f"INSERT INTO {name}.events SELECT number, number FROM numbers({ROWS})"
            )
            root._cx.command(f"CREATE USER {name}_other IDENTIFIED WITH no_password")
            me = root._cx.query("SELECT currentUser()").result_rows[0][0]
            with ClickHouseEngine(local) as mine, ClickHouseEngine(local) as probe:
                operator = native_operator(mine, [probe])
                operator.identity("events")  # the control: no policy, and the table is taken
                for to, shown in ((me, SHOWN), (f"{name}_other", ROWS)):
                    root._cx.command(
                        f"CREATE ROW POLICY hide ON {name}.events FOR SELECT "
                        f"USING id < {SHOWN} TO {to}"
                    )
                    assert mine.count("events") == shown, "the premise"
                    with pytest.raises(sde.MigrationRefused, match="row policies"):
                        operator.identity("events")
                    root._cx.command(f"DROP ROW POLICY hide ON {name}.events")
                operator.identity("events")
        finally:
            root._cx.command(f"DROP ROW POLICY IF EXISTS hide ON {name}.events")
            root._cx.command(f"DROP USER IF EXISTS {name}_other")
            root._cx.command(f"DROP DATABASE IF EXISTS {name}")


def test_a_backfill_and_a_cutover_refuse_a_source_with_a_row_policy_and_change_nothing(
    tmp_path: Any,
) -> None:
    """The whole operation, from a ClickHouse source: the policy is found where the operator takes
    the identity of the cutover's tables, which is before its first barrier."""
    from test_local_cutover_live import fixture

    with fixture("clickhouse", tmp_path) as (executor, plan, old, roles, source, _target, *_):
        root = roles[source].operator
        me = root._cx.query("SELECT currentUser()").result_rows[0][0]
        table = f"{roles[source].namespace}.source_events"
        root._cx.command(f"CREATE ROW POLICY hide ON {table} FOR SELECT USING id < 2 TO {me}")
        try:
            assert root.count("source_events") == 1, "the premise: one row of the two"
            # A backfill would copy the one row it is shown, and is refused as the cutover is.
            with pytest.raises(sde.MigrationRefused, match="row policies"):
                executor.backfill()
            with pytest.raises(sde.MigrationRefused, match="row policies"):
                executor.execute(plan)
        finally:
            root._cx.command(f"DROP ROW POLICY IF EXISTS hide ON {table}")
        # Nothing was paused and nothing recorded: the application writes as it did, and the same
        # plan runs once the operator is shown the table.
        old.save("Event", {"id": 3, "value": 33})
        assert root.count("source_events") == 3
        with executor.store.lock():
            assert executor.store.read()["execution"] is None
        receipt = executor.execute(plan).as_record()
        assert receipt["outcome"] == "success" and receipt["verification"]["matched"] is True

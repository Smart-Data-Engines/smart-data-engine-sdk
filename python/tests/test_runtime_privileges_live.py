"""A signed generation-bearing session must run on credentials that cannot issue DDL."""

from __future__ import annotations

import base64
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

import sde
from sde.engines.clickhouse import ClickHouseEngine
from sde.engines.postgres import PostgresEngine
from sde.placement import WATERMARK_TABLE

PROJECT = "1" * 32


@dataclass
class Roles:
    operator: Any
    runtime: Any
    namespace: str
    username: str

    def command(self, statement: str, *, runtime: bool = False) -> None:
        engine = self.runtime if runtime else self.operator
        if engine.dialect == "postgres":
            engine._cx.execute(statement)
        else:
            engine._cx.command(statement)

    def grant(self, table: str, *, revoke: bool = False) -> None:
        # Every name here is a fixed SDK/test identifier or generated hexadecimal text.
        action, direction = ("REVOKE", "FROM") if revoke else ("GRANT", "TO")
        quote = '"' if self.operator.dialect == "postgres" else "`"
        self.command(
            f"{action} {'SELECT' if revoke else 'SELECT, INSERT'} ON "
            f"{quote}{self.namespace}{quote}.{quote}{table}{quote} "
            f"{direction} {quote}{self.username}{quote}"
        )

    def metadata_rows(self) -> list[Any]:
        if self.operator.dialect == "postgres":
            return list(
                self.operator._cx.execute(f'SELECT map_version FROM "{WATERMARK_TABLE}"').fetchall()
            )
        return list(
            self.operator._cx.query(f"SELECT map_version FROM `{WATERMARK_TABLE}`").result_rows
        )

    def exists(self) -> bool:
        if self.operator.dialect == "postgres":
            return (
                self.operator._cx.execute("SELECT to_regclass(%s)", (WATERMARK_TABLE,)).fetchone()[
                    0
                ]
                is not None
            )
        return bool(self.operator._cx.query(f"EXISTS TABLE `{WATERMARK_TABLE}`").result_rows[0][0])


def cleanup_dsn(dsn: str) -> str:
    """Give fixture destruction its own finite budget; never change the measured runtime."""
    parts = urlsplit(dsn)
    options = dict(parse_qsl(parts.query, keep_blank_values=True))
    timeout = max(300.0, float(options.get("send_receive_timeout", "0")))
    options["send_receive_timeout"] = str(timeout)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(options), parts.fragment))


@contextmanager
def runtime_roles(dialect: str, *, operator_login: bool = False) -> Iterator[Roles]:
    """A schema or database of its own, with a restricted runtime login on it.

    ``operator_login`` (ClickHouse) makes the operator a login of its own rather than the
    server's administrator, as a deployment with an administrator beside it has it: everything
    on its database with grant option, and the system tables it reads. No global grant, so a
    login left behind by a killed run covers no other test's tables.
    """
    assert dialect == "clickhouse" or not operator_login, "PostgreSQL's operator is the DSN's"
    dsn = os.environ.get("SDE_POSTGRES_DSN" if dialect == "postgres" else "SDE_CLICKHOUSE_DSN")
    if not dsn:
        pytest.skip(f"{dialect} is required for restricted runtime qualification")
    name = "sde_roles_" + uuid4().hex[:16]
    username, password = name + "_app", secrets.token_hex(24)
    if dialect == "postgres":
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        with PostgresEngine(dsn) as operator:
            runtime: Any = None
            created = False
            try:
                operator._cx.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(username), sql.Literal(password)
                    )
                )
                created = True
                operator._cx.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
                operator._cx.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(name)))
                operator._cx.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                        sql.Identifier(name), sql.Identifier(username)
                    )
                )
                runtime = PostgresEngine(
                    make_conninfo(
                        dsn, user=username, password=password, options=f"-csearch_path={name}"
                    )
                )
                runtime.connect()
                yield Roles(operator, runtime, name, username)
            finally:
                if runtime:
                    runtime.close()
                operator._cx.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name))
                )
                if created:
                    operator._cx.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(username)))
    else:
        parts = urlsplit(dsn)
        with ClickHouseEngine(dsn) as root:
            created = False
            runtime = None
            operator_user = None
            try:
                root._cx.command(
                    f"CREATE USER {username} IDENTIFIED WITH sha256_password BY '{password}'"
                )
                created = True
                root._cx.command(f"CREATE DATABASE {name} ENGINE=Atomic")
                root._cx.command(f"GRANT SELECT ON system.settings TO {username}")
                local = urlunsplit(
                    (parts.scheme, parts.netloc, "/" + name, parts.query, parts.fragment)
                )
                if operator_login:
                    operator_user, secret = name + "_op", secrets.token_hex(24)
                    root._cx.command(
                        f"CREATE USER {operator_user} IDENTIFIED WITH sha256_password BY '{secret}'"
                    )
                    root._cx.command(f"GRANT ALL ON {name}.* TO {operator_user} WITH GRANT OPTION")
                    root._cx.command(f"GRANT SELECT ON system.* TO {operator_user}")
                    local = urlunsplit(
                        (
                            parts.scheme,
                            f"{operator_user}:{secret}@{parts.hostname}:{parts.port}",
                            "/" + name,
                            parts.query,
                            parts.fragment,
                        )
                    )
                with ClickHouseEngine(local) as operator:
                    app = urlunsplit(
                        (
                            parts.scheme,
                            f"{username}:{password}@{parts.hostname}:{parts.port}",
                            "/" + name,
                            parts.query,
                            parts.fragment,
                        )
                    )
                    runtime = ClickHouseEngine(app)
                    runtime.connect()
                    yield Roles(operator, runtime, name, username)
            finally:
                if runtime:
                    runtime.close()
                # A large run can leave many physical parts to delete. This is outside the
                # workload, and a short runtime receive timeout must not become its cleanup SLA.
                with ClickHouseEngine(cleanup_dsn(dsn)) as cleanup:
                    cleanup._cx.command(f"DROP DATABASE IF EXISTS {name} SYNC")
                    if created:
                        cleanup._cx.command(f"DROP USER {username}")
                    if operator_user:
                        cleanup._cx.command(f"DROP USER IF EXISTS {operator_user}")


@contextmanager
def outsider(dialect: str, name: str, *grants: str, role: bool = False) -> Iterator[str]:
    """A principal beside the operation, holding ``grants`` (``{who}`` is its quoted name).

    Created and dropped by the server's administrator, around the block alone: a global grant
    left on the shared test server would refuse every later cutover there. PostgreSQL's is a
    role; ClickHouse's is a user, or a role with ``role``.
    """
    dsn = os.environ.get("SDE_POSTGRES_DSN" if dialect == "postgres" else "SDE_CLICKHOUSE_DSN")
    if not dsn:
        pytest.skip(f"{dialect} is required for a principal beside the operation")
    quote = '"' if dialect == "postgres" else "`"
    who = f"{quote}{name}{quote}"
    with PostgresEngine(dsn) if dialect == "postgres" else ClickHouseEngine(dsn) as admin:

        def run(statement: str) -> None:
            if dialect == "postgres":
                admin._cx.execute(statement)
            else:
                admin._cx.command(statement)

        if dialect == "postgres" or role:
            run(f"CREATE ROLE {who}")
        else:
            run(f"CREATE USER {who} IDENTIFIED WITH sha256_password BY '{secrets.token_hex(24)}'")
        try:
            for grant in grants:
                run(grant.format(who=who))
            yield name
        finally:
            if dialect == "postgres":
                run(f"DROP OWNED BY {who}")
                run(f"DROP ROLE IF EXISTS {who}")
            else:
                run(f"DROP {'ROLE' if role else 'USER'} IF EXISTS {who}")


@pytest.fixture(params=["postgres", "clickhouse"])
def roles(request: pytest.FixtureRequest) -> Iterator[Roles]:
    with runtime_roles(request.param) as value:
        yield value


def document(roles: Roles, *, signed: bool = True) -> tuple[sde.LogicalModel, sde.PlacementMap]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sde.clear_registry()

    @sde.entity
    class Event:
        id: int

    model = sde.build_model(Event)
    raw: dict[str, Any] = {
        "contract": 4,
        "project_id": PROJECT,
        "model_version": model.version,
        "map_version": 7,
        "groups": {
            "Event": {
                "write_epoch": 1,
                "source": {
                    "engine": "db",
                    "id": "source",
                    "layout": {
                        "tables": {"Event": "events"},
                        "columns": {
                            "Event": {
                                "id": "bigint" if roles.operator.dialect == "postgres" else "Int64"
                            }
                        },
                    },
                },
            }
        },
    }
    public = None
    if signed:
        key = Ed25519PrivateKey.generate()
        raw["signature"] = {
            "alg": "ed25519",
            "value": base64.b64encode(key.sign(sde.canonical_bytes(raw))).decode(),
        }
        public = key.public_key().public_bytes_raw()
    return model, sde.load_map(raw, model=model, public_key=public)


def test_existing_metadata_allows_signed_runtime_without_ddl(roles: Roles) -> None:
    model, placement = document(roles)
    sde.prepare_schema(model, placement, {"db": roles.operator}, project_id=PROJECT)
    roles.operator.map_watermark()  # Existing installations can already have this table.
    roles.grant("events")
    roles.grant(WATERMARK_TABLE)
    session = sde.Session(model, placement, {"db": roles.runtime}, project_id=PROJECT)
    session.save("Event", {"id": 1})
    assert session.get("Event", {"id": 1}) == {"id": 1}
    assert session.rollback_protection.protection == "enforced"
    assert roles.operator.map_watermark() == 7
    create = (
        "CREATE TABLE forbidden_table (id bigint)"
        if roles.operator.dialect == "postgres"
        else "CREATE TABLE forbidden_table (id Int64) ENGINE=MergeTree ORDER BY id"
    )
    with pytest.raises(Exception, match=r"permission denied|Not enough privileges"):
        roles.command(create, runtime=True)
    with pytest.raises(sde.EngineError, match=r"must be owner|Not enough privileges"):
        roles.runtime.write_fence("events", project_id=PROJECT).freeze("6" * 32)
    assert roles.operator.write_fence("events", project_id=PROJECT).state().holds == ()


def test_provisioning_prepares_metadata_without_adopting_the_map(roles: Roles) -> None:
    model, placement = document(roles)
    sde.prepare_schema(model, placement, {"db": roles.operator}, project_id=PROJECT)
    assert roles.exists(), "signed provisioning left bookkeeping creation to runtime"
    assert roles.metadata_rows() == []
    roles.operator.record_map_version(2, model_version=model.version)
    sde.prepare_schema(model, placement, {"db": roles.operator}, project_id=PROJECT)
    assert roles.metadata_rows() == [(2,)]


def test_unreadable_metadata_is_not_an_empty_watermark(roles: Roles) -> None:
    model, placement = document(roles)
    sde.prepare_schema(model, placement, {"db": roles.operator}, project_id=PROJECT)
    roles.operator.map_watermark()
    roles.operator.record_map_version(9, model_version=model.version)
    roles.grant("events")
    roles.grant(WATERMARK_TABLE)
    roles.grant(WATERMARK_TABLE, revoke=True)
    with pytest.raises(sde.EngineError, match=r"permission denied|Not enough privileges"):
        sde.Session(model, placement, {"db": roles.runtime}, project_id=PROJECT)
    assert roles.metadata_rows() == [(9,)]


def test_unsigned_provisioning_and_runtime_need_no_watermark(roles: Roles) -> None:
    model, placement = document(roles, signed=False)
    sde.prepare_schema(model, placement, {"db": roles.operator}, project_id=PROJECT)
    assert not roles.exists()
    roles.grant("events")
    session = sde.Session(model, placement, {"db": roles.runtime}, project_id=PROJECT)
    session.save("Event", {"id": 1})
    assert session.get("Event", {"id": 1}) == {"id": 1}
    assert session.rollback_protection.protection == "not_applicable"
    assert not roles.exists()


def test_unmapped_supplied_engine_gets_runtime_bookkeeping_too(roles: Roles) -> None:
    with runtime_roles(roles.operator.dialect) as spare:
        model, placement = document(roles)
        sde.prepare_schema(
            model, placement, {"db": roles.operator, "spare": spare.operator}, project_id=PROJECT
        )
        roles.grant("events")
        roles.grant(WATERMARK_TABLE)
        spare.grant(WATERMARK_TABLE)
        session = sde.Session(
            model, placement, {"db": roles.runtime, "spare": spare.runtime}, project_id=PROJECT
        )
        session.save("Event", {"id": 1})
        assert session.get("Event", {"id": 1}) == {"id": 1}
        assert session.rollback_protection.participating == ("db", "spare")
        assert spare.metadata_rows() == [(7,)]


def test_runtime_connections_keep_the_callers_budget_during_long_cleanup() -> None:
    from sde.engines._clickhouse_connection import parse_dsn

    original_dsn = os.environ.get("SDE_CLICKHOUSE_DSN")
    if not original_dsn:
        pytest.skip("clickhouse is required for cleanup isolation")
    expected = parse_dsn(original_dsn).send_receive_timeout
    with runtime_roles("clickhouse") as roles:
        assert roles.operator._cx.timeout.read_timeout == expected
        assert roles.runtime._cx.timeout.read_timeout == expected
        assert parse_dsn(roles.operator._dsn).send_receive_timeout == expected
        assert parse_dsn(roles.runtime._dsn).send_receive_timeout == expected


def test_cleanup_waits_for_a_real_reader_without_extending_runtime_timeouts() -> None:
    """Atomic DROP SYNC can outlive the data-path inactivity default."""
    import threading
    import time

    finished = threading.Event()
    failures: list[BaseException] = []
    reader: threading.Thread | None = None
    began_cleanup = 0.0
    try:
        with runtime_roles("clickhouse") as roles:
            roles.command("CREATE TABLE cleanup_reader (id Int64) ENGINE=MergeTree ORDER BY id")
            roles.command("INSERT INTO cleanup_reader SELECT number FROM numbers(200)")
            roles.grant("cleanup_reader")
            marker = "cleanup_reader_" + uuid4().hex
            # This independent deliberately slow reader chooses its own explicit allowance.
            # The actual fixture runtime below retains the caller's original15-second default.
            parts = urlsplit(roles.runtime._dsn)
            options = dict(parse_qsl(parts.query))
            options["send_receive_timeout"] = "60"
            runtime_dsn = urlunsplit(
                (parts.scheme, parts.netloc, parts.path, urlencode(options), parts.fragment)
            )

            def hold_reader() -> None:
                try:
                    with ClickHouseEngine(runtime_dsn) as client:
                        result = client._cx.query(
                            "SELECT sleepEachRow(0.1) FROM cleanup_reader /* " + marker + " */",
                            settings={
                                "max_block_size": 1,
                                "max_execution_time": 25,
                                "function_sleep_max_microseconds_per_block": 25000000,
                            },
                        )
                        assert len(result.result_rows) == 200
                except BaseException as error:
                    failures.append(error)
                finally:
                    finished.set()

            reader = threading.Thread(target=hold_reader, daemon=True)
            reader.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                active = roles.operator._cx.query(
                    "SELECT count() FROM system.processes WHERE "
                    "startsWith(query, 'SELECT sleepEachRow') "
                    "AND position(query, {marker:String}) > 0",
                    parameters={"marker": marker},
                ).result_rows[0][0]
                if active:
                    break
                time.sleep(0.02)
            else:
                if failures:
                    raise failures[0]
                raise AssertionError("the independent native reader never acquired its table")
            assert roles.runtime._cx.timeout.read_timeout == 15
            began_cleanup = time.monotonic()
        assert time.monotonic() - began_cleanup > 15
        assert finished.wait(5)
        assert not failures
    finally:
        if reader is not None:
            reader.join(timeout=35)
            assert not reader.is_alive(), "the bounded native reader did not finish"

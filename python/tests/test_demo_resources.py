"""Disposable native resources prove ownership before grants, cleanup and crash recovery."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest

from sde import PhysicalLayout
from sde.engines.clickhouse import ClickHouseEngine
from sde.engines.postgres import PostgresEngine
from sde_demo import resources

BINDING = "engine"


@pytest.fixture
def admin_dsns() -> dict[str, str]:
    return {
        dialect: value
        for dialect, name in (
            ("postgres", "SDE_POSTGRES_DSN"),
            ("clickhouse", "SDE_CLICKHOUSE_DSN"),
        )
        if (value := os.environ.get(name))
    }


class Harness:
    def __init__(self, root: Path, dialect: str, admin_dsns: dict[str, str]) -> None:
        if dialect not in admin_dsns:
            pytest.skip(f"{dialect} is required for demo resource ownership tests")
        self.root, self.dialect, self.admin_dsns = root, dialect, admin_dsns
        self.namespace_ids: set[str] = set()
        self.user_ids: set[str] = set()
        self.records: list[dict[str, Any]] = []

    @contextmanager
    def admin(self) -> Iterator[Any]:
        kind = PostgresEngine if self.dialect == "postgres" else ClickHouseEngine
        with kind(self.admin_dsns[self.dialect]) as engine:
            yield engine

    def record(self) -> dict[str, Any]:
        return json.loads((self.root / "resources.json").read_bytes())

    def remember(self, *, explicitly_created: bool = False) -> None:
        if not (self.root / "resources.json").exists():
            return
        record = self.record()
        entry = record["engines"][BINDING]
        if not any(prior["allocation_id"] == record["allocation_id"] for prior in self.records):
            self.records.append(record)
        with self.admin() as engine:
            namespace, principal = (
                resources._namespace(engine, entry),
                resources._principal(engine, entry),
            )
            if namespace is not None:
                assert explicitly_created or namespace["marker"] == entry["owner_marker"]
                self.namespace_ids.add(namespace["id"])
            if principal is not None:
                marker_matches = (
                    principal["marker"] == entry["owner_marker"]
                    if self.dialect == "postgres"
                    else principal["default_database"] == entry["namespace"]
                )
                assert explicitly_created or marker_matches
                self.user_ids.add(principal["id"])

    def allocate(self) -> dict[str, Any]:
        result = resources.allocate(self.root, {BINDING: self.dialect}, self.admin_dsns)
        self.remember()
        return result

    def credentials(self, purpose: str) -> dict[str, str]:
        return json.loads((self.root / f"{purpose}-credentials.json").read_bytes())

    @contextmanager
    def client(self, purpose: str) -> Iterator[Any]:
        kind = PostgresEngine if self.dialect == "postgres" else ClickHouseEngine
        with kind(self.credentials(purpose)[BINDING]) as engine:
            yield engine

    def tables(self) -> None:
        numeric = "bigint" if self.dialect == "postgres" else "Int64"
        layout = PhysicalLayout(
            tables={"Event": "events", "Bookkeeping": "sde_map_state", "Other": "ungranted"},
            columns={name: {"id": numeric} for name in ("Event", "Bookkeeping", "Other")},
        )
        with self.client("operator") as engine:
            engine.ensure_schema(layout, keys={name: ("id",) for name in layout.tables})

    def cleanup(self) -> None:
        # Tests that deliberately replace an object record the new native id immediately after
        # their own CREATE. Cleanup refuses an incarnation the test did not create.
        with self.admin() as engine:
            for record in self.records:
                entry = record["engines"][BINDING]
                namespace = resources._namespace(engine, entry)
                if namespace is not None:
                    assert namespace["id"] in self.namespace_ids
                    resources._drop_namespace(engine, {**entry, "namespace_identity": namespace})
                principal = resources._principal(engine, entry)
                if principal is not None:
                    assert principal["id"] in self.user_ids
                    resources._drop_user(engine, entry)


@pytest.fixture
def harness(tmp_path: Path, dialect: str, admin_dsns: dict[str, str]) -> Iterator[Harness]:
    result = Harness(tmp_path / "demo", dialect, admin_dsns)
    try:
        yield result
    finally:
        result.remember()
        result.cleanup()


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_allocate_grant_reset_and_allocate_fresh(harness: Harness) -> None:
    record = harness.allocate()
    assert record["status"] == "ready"
    assert record["engines"][BINDING]["namespace_identity"] is not None
    assert record["engines"][BINDING]["runtime_identity"] is not None
    for purpose in ("operator", "runtime"):
        path = harness.root / record["credential_files"][purpose]
        assert path.stat().st_mode & 0o777 == 0o600
        dsn = harness.credentials(purpose)[BINDING]
        assert dsn not in json.dumps(record)
        if purpose == "runtime":
            assert unquote(urlsplit(dsn).password or "") not in json.dumps(record)
    original = (harness.root / "resources.json").read_bytes()
    assert harness.allocate() == record
    assert (harness.root / "resources.json").read_bytes() == original
    harness.tables()
    resources.grant_tables(harness.root, {BINDING: ["events", "sde_map_state"]}, harness.admin_dsns)
    with harness.client("runtime") as engine:
        if harness.dialect == "postgres":
            engine._cx.execute('INSERT INTO "events" VALUES (1)')
            assert engine._cx.execute('SELECT id FROM "events"').fetchall() == [(1,)]
            with pytest.raises(Exception, match="permission denied"):
                engine._cx.execute('SELECT id FROM "ungranted"')
            with pytest.raises(Exception, match="permission denied"):
                engine._cx.execute('CREATE TABLE "forbidden" (id bigint)')
        else:
            engine._cx.command('INSERT INTO "events" VALUES (1)')
            assert engine._cx.query('SELECT id FROM "events" FINAL').result_rows == [(1,)]
            with pytest.raises(Exception, match="ACCESS_DENIED"):
                engine._cx.query('SELECT id FROM "ungranted"')
            with pytest.raises(Exception, match="ACCESS_DENIED"):
                engine._cx.command(
                    'CREATE TABLE "forbidden" (id Int64) ENGINE=MergeTree ORDER BY id'
                )
    finished = resources.reset(harness.root, harness.admin_dsns)
    assert finished["status"] == "reset"
    assert resources.reset(harness.root, harness.admin_dsns) == finished
    assert not (harness.root / "operator-credentials.json").exists()
    assert not (harness.root / "runtime-credentials.json").exists()
    another = harness.allocate()
    assert another["allocation_id"] != record["allocation_id"]
    assert another["engines"][BINDING]["namespace"] != record["engines"][BINDING]["namespace"]
    resources.reset(harness.root, harness.admin_dsns)


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_ready_allocate_does_not_restore_revoked_table_grants(harness: Harness) -> None:
    record = harness.allocate()
    harness.tables()
    resources.grant_tables(harness.root, {BINDING: ["events"]}, harness.admin_dsns)
    entry = record["engines"][BINDING]
    with harness.admin() as engine:
        if harness.dialect == "postgres":
            engine._cx.execute(
                f'REVOKE SELECT, INSERT ON "{entry["namespace"]}"."events" '
                f'FROM "{entry["runtime_user"]}"'
            )
        else:
            engine._cx.command(
                f"REVOKE SELECT, INSERT ON `{entry['namespace']}`.`events` "
                f"FROM `{entry['runtime_user']}`"
            )
    harness.allocate()
    resources.verify(harness.root)
    with (
        harness.client("runtime") as engine,
        pytest.raises(Exception, match=r"permission denied|ACCESS_DENIED"),
    ):
        if harness.dialect == "postgres":
            engine._cx.execute('SELECT id FROM "events"')
        else:
            engine._cx.query('SELECT id FROM "events"')


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_changed_namespace_marker_refuses_before_any_grant_or_reset(harness: Harness) -> None:
    record = harness.allocate()
    entry = record["engines"][BINDING]
    with harness.admin() as engine:

        def comment(value: str) -> None:
            if harness.dialect == "postgres":
                from psycopg import sql

                engine._cx.execute(
                    sql.SQL("COMMENT ON SCHEMA {} IS {}").format(
                        sql.Identifier(entry["namespace"]), sql.Literal(value)
                    )
                )
            else:
                # ClickHouse 24.8 cannot ALTER a database comment. Reuse the owned empty
                # database's planned UUID so that the marker is the independent guard.
                resources._drop_namespace(engine, entry)
                engine._cx.command(
                    f"CREATE DATABASE `{entry['namespace']}` UUID '{entry['expected_uuid']}' "
                    f"ENGINE=Atomic COMMENT '{value}'"
                )

        comment("foreign-marker")
        try:
            for action in (
                lambda: resources.allocate(
                    harness.root, {BINDING: harness.dialect}, harness.admin_dsns
                ),
                lambda: resources.grant_tables(harness.root, {BINDING: []}, harness.admin_dsns),
                lambda: resources.reset(harness.root, harness.admin_dsns),
                lambda: resources.verify(harness.root),
            ):
                with pytest.raises(resources.ResourceRefused, match="marker"):
                    action()
            assert resources._namespace(engine, entry)["id"] == entry["namespace_identity"]["id"]
        finally:
            comment(entry["owner_marker"])


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_replaced_namespace_with_the_same_marker_is_not_ours(harness: Harness) -> None:
    record = harness.allocate()
    entry = record["engines"][BINDING]
    with harness.admin() as engine:
        resources._drop_namespace(engine, entry)
        if harness.dialect == "postgres":
            engine._cx.execute(f'CREATE SCHEMA "{entry["namespace"]}"')
            from psycopg import sql

            engine._cx.execute(
                sql.SQL("COMMENT ON SCHEMA {} IS {}").format(
                    sql.Identifier(entry["namespace"]), sql.Literal(entry["owner_marker"])
                )
            )
        else:
            engine._cx.command(
                f"CREATE DATABASE `{entry['namespace']}` ENGINE=Atomic "
                f"COMMENT '{entry['owner_marker']}'"
            )
        replacement = resources._namespace(engine, entry)
        harness.remember(explicitly_created=True)
        assert replacement["id"] != entry["namespace_identity"]["id"]
        for action in (
            lambda: resources.allocate(
                harness.root, {BINDING: harness.dialect}, harness.admin_dsns
            ),
            lambda: resources.grant_tables(harness.root, {BINDING: []}, harness.admin_dsns),
            lambda: resources.reset(harness.root, harness.admin_dsns),
            lambda: resources.verify(harness.root),
        ):
            with pytest.raises(resources.ResourceRefused, match=r"identity|UUID|endpoint"):
                action()
        assert resources._namespace(engine, entry) == replacement


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_replaced_runtime_with_our_password_is_not_our_recorded_identity(harness: Harness) -> None:
    record = harness.allocate()
    entry = record["engines"][BINDING]
    password = unquote(urlsplit(harness.credentials("runtime")[BINDING]).password or "")
    with harness.admin() as engine:
        if harness.dialect == "postgres":
            engine._cx.execute(
                f'REVOKE USAGE ON SCHEMA "{entry["namespace"]}" FROM "{entry["runtime_user"]}"'
            )
        resources._drop_user(engine, entry)
        if harness.dialect == "postgres":
            from psycopg import sql

            engine._cx.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(entry["runtime_user"]), sql.Literal(password)
                )
            )
            engine._cx.execute(
                sql.SQL("COMMENT ON ROLE {} IS {}").format(
                    sql.Identifier(entry["runtime_user"]), sql.Literal(entry["owner_marker"])
                )
            )
        else:
            engine._cx.command(
                f"CREATE USER `{entry['runtime_user']}` IDENTIFIED WITH sha256_password "
                f"BY '{password}' DEFAULT DATABASE `{entry['namespace']}`"
            )
        replacement = resources._principal(engine, entry)
        harness.remember(explicitly_created=True)
        assert replacement["id"] != entry["runtime_identity"]["id"]
        with pytest.raises(resources.ResourceRefused, match="runtime native identity"):
            resources.reset(harness.root, harness.admin_dsns)
        assert resources._principal(engine, entry) == replacement


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_granted_table_replacement_is_refused(harness: Harness) -> None:
    harness.allocate()
    harness.tables()
    resources.grant_tables(harness.root, {BINDING: ["events"]}, harness.admin_dsns)
    with harness.client("operator") as engine:
        if harness.dialect == "postgres":
            engine._cx.execute('DROP TABLE "events"')
        else:
            engine._cx.command('DROP TABLE "events" SYNC')
    harness.tables()
    for action in (
        lambda: resources.grant_tables(harness.root, {BINDING: ["events"]}, harness.admin_dsns),
        lambda: resources.reset(harness.root, harness.admin_dsns),
        lambda: resources.verify(harness.root),
    ):
        with pytest.raises(resources.ResourceRefused, match="table"):
            action()


class Crash(BaseException):
    pass


CREATE_STEPS = [
    (dialect, stage)
    for dialect in ("postgres", "clickhouse")
    for stage in (
        "intent_written",
        "credential_written:operator",
        "credential_written:runtime",
        "credentials_ready",
        "namespace_created:engine",
        "namespace_recorded:engine",
        "ready",
    )
] + [("clickhouse", stage) for stage in ("runtime_created:engine", "runtime_recorded:engine")]


@pytest.mark.parametrize(("dialect", "stage"), CREATE_STEPS)
def test_allocation_resumes_completed_native_steps_without_replacing_credentials(
    stage: str, harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(step: str) -> None:
        if step == stage:
            harness.remember()
            raise Crash(step)

    with monkeypatch.context() as patch:
        patch.setattr(resources, "_after_step", crash)
        with pytest.raises(Crash):
            resources.allocate(harness.root, {BINDING: harness.dialect}, harness.admin_dsns)
    original = harness.record()
    credentials = {path.name: path.read_bytes() for path in harness.root.glob("*-credentials.json")}
    resumed = harness.allocate()
    assert resumed["allocation_id"] == original["allocation_id"]
    assert resumed["status"] == "ready"
    assert all(
        (harness.root / name).read_bytes() == payload for name, payload in credentials.items()
    )


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
@pytest.mark.parametrize(
    "stage",
    [
        "reset_intent",
        "namespace_dropped:engine",
        "user_drop_intent:engine",
        "runtime_dropped:engine",
        "reset_complete",
    ],
)
def test_reset_resumes_without_reusing_or_forgetting_ids(
    stage: str, harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.allocate()
    harness.tables()
    resources.grant_tables(harness.root, {BINDING: ["events"]}, harness.admin_dsns)

    def crash(step: str) -> None:
        if step == stage:
            raise Crash(step)

    with monkeypatch.context() as patch:
        patch.setattr(resources, "_after_step", crash)
        with pytest.raises(Crash):
            resources.reset(harness.root, harness.admin_dsns)
    assert resources.reset(harness.root, harness.admin_dsns)["status"] == "reset"


@pytest.mark.parametrize(
    ("dialect", "stage"),
    [
        ("postgres", "namespace_created:engine"),
        ("clickhouse", "namespace_created:engine"),
        ("clickhouse", "runtime_created:engine"),
    ],
)
def test_sigkill_after_create_recovers_native_ownership(stage: str, harness: Harness) -> None:
    script = """\
import os,sys
from pathlib import Path
from sde_demo import resources
def pause(step):
    if step == sys.argv[3]:
        print('READY',flush=True)
        sys.stdin.readline()
resources._after_step=pause
dialect=sys.argv[2]
dsn=os.environ['SDE_POSTGRES_DSN' if dialect=='postgres' else 'SDE_CLICKHOUSE_DSN']
resources.allocate(Path(sys.argv[1]),{'engine':dialect},{dialect:dsn})
"""
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", script, str(harness.root), harness.dialect, stage],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert select.select([child.stdout], [], [], 30)[0], "child did not reach CREATE checkpoint"
        line = child.stdout.readline()
        if line != "READY\n":
            output, error = child.communicate(timeout=10)
            pytest.fail(f"unexpected child output: {line + output!r}; {error!r}")
        harness.remember()
        child.kill()
        output, error = child.communicate(timeout=10)
        assert child.returncode == -signal.SIGKILL, (output, error)
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)
    original_ids = (set(harness.namespace_ids), set(harness.user_ids))
    assert harness.allocate()["status"] == "ready"
    assert original_ids[0] <= harness.namespace_ids
    assert original_ids[1] <= harness.user_ids
    assert len(harness.namespace_ids) == len(harness.user_ids) == 1


@pytest.mark.parametrize(
    ("dialect", "dsn"),
    [
        ("postgres", "postgresql://user:secret@192.0.2.1/db"),
        ("postgres", "host=127.0.0.1 hostaddr=192.0.2.1 dbname=db user=user password=secret"),
        ("postgres", "postgresql://user:secret@localhost,192.0.2.1/db"),
        ("clickhouse", "clickhouse://user:secret@example.invalid/db"),
        ("clickhouse", "clickhouse://user:secret@127.0.0.1/db?host=192.0.2.1"),
    ],
)
def test_nonloopback_and_redirecting_bindings_refuse_before_creating_files(
    dialect: str, dsn: str, tmp_path: Path
) -> None:
    with pytest.raises(resources.ResourceRefused) as error:
        resources.allocate(tmp_path / "demo", {"engine": dialect}, {dialect: dsn})
    assert "secret" not in str(error.value)
    assert not (tmp_path / "demo/resources.json").exists()


@pytest.mark.parametrize("dialect", ["clickhouse"])
@pytest.mark.parametrize("authentication", ["no_password", "different_password"])
def test_unrecorded_clickhouse_user_must_prove_the_exact_saved_secret(
    harness: Harness, authentication: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(step: str) -> None:
        if step == "namespace_recorded:engine":
            harness.remember()
            raise Crash(step)

    with monkeypatch.context() as patch:
        patch.setattr(resources, "_after_step", crash)
        with pytest.raises(Crash):
            resources.allocate(harness.root, {BINDING: harness.dialect}, harness.admin_dsns)
    entry = harness.record()["engines"][BINDING]
    assert entry["runtime_identity"] is None
    with harness.admin() as engine:
        clause = (
            "IDENTIFIED WITH no_password"
            if authentication == "no_password"
            else "IDENTIFIED WITH sha256_password BY 'not-the-persisted-demo-secret'"
        )
        engine._cx.command(
            f"CREATE USER `{entry['runtime_user']}` {clause} "
            f"DEFAULT DATABASE `{entry['namespace']}`"
        )
        harness.remember(explicitly_created=True)
        identity = resources._principal(engine, entry)
        before = (harness.root / "resources.json").read_bytes()
        for action in (
            lambda: resources.allocate(
                harness.root, {BINDING: harness.dialect}, harness.admin_dsns
            ),
            lambda: resources.reset(harness.root, harness.admin_dsns),
        ):
            with pytest.raises(resources.ResourceRefused, match="saved credential"):
                action()
            assert (harness.root / "resources.json").read_bytes() == before
            assert resources._principal(engine, entry) == identity
            assert resources._namespace(engine, entry) == entry["namespace_identity"]


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
@pytest.mark.parametrize("stage", ["grant_intent:engine:events", "grant_applied:engine:events"])
def test_grant_recovery_preserves_the_table_incarnation(
    harness: Harness, stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.allocate()
    harness.tables()

    def crash(step: str) -> None:
        if step == stage:
            raise Crash(step)

    with monkeypatch.context() as patch:
        patch.setattr(resources, "_after_step", crash)
        with pytest.raises(Crash):
            resources.grant_tables(harness.root, {BINDING: ["events"]}, harness.admin_dsns)
    pending = harness.record()["engines"][BINDING]["tables"]["events"]
    assert pending["phase"] == "granting"
    resources.grant_tables(harness.root, {BINDING: ["events"]}, harness.admin_dsns)
    finished = harness.record()["engines"][BINDING]["tables"]["events"]
    assert finished == {"identity": pending["identity"], "phase": "granted"}
    with harness.client("runtime") as engine:
        if harness.dialect == "postgres":
            assert engine._cx.execute('SELECT count(*) FROM "events"').fetchone() == (0,)
        else:
            assert engine._cx.query('SELECT count(*) FROM "events" FINAL').result_rows == [(0,)]


@pytest.mark.parametrize(
    ("dialect", "stage"),
    [
        ("postgres", "credentials_ready"),
        ("clickhouse", "credentials_ready"),
        ("postgres", "namespace_created:engine"),
        ("clickhouse", "namespace_created:engine"),
        ("clickhouse", "runtime_created:engine"),
    ],
)
def test_reset_cleans_only_proved_resources_from_partial_allocation(
    harness: Harness, stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(step: str) -> None:
        if step == stage:
            harness.remember()
            raise Crash(step)

    with monkeypatch.context() as patch:
        patch.setattr(resources, "_after_step", crash)
        with pytest.raises(Crash):
            resources.allocate(harness.root, {BINDING: harness.dialect}, harness.admin_dsns)
    finished = resources.reset(harness.root, harness.admin_dsns)
    assert finished["status"] == "reset"
    assert resources.reset(harness.root, harness.admin_dsns) == finished
    with harness.admin() as engine:
        assert resources._namespace(engine, finished["engines"][BINDING]) is None
        assert resources._principal(engine, finished["engines"][BINDING]) is None


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_verify_ready_allocation_uses_local_credentials_without_writing(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.allocate()
    harness.tables()
    resources.grant_tables(harness.root, {BINDING: ["events", "sde_map_state"]}, harness.admin_dsns)
    before = {path.name: path.read_bytes() for path in harness.root.glob("*.json")}
    record = harness.record()
    monkeypatch.setattr(resources, "_grant", lambda *args: pytest.fail("verification regranted"))
    monkeypatch.setattr(
        resources, "_write", lambda *args, **kwargs: pytest.fail("verification wrote")
    )
    assert resources.verify(harness.root) == record
    assert {path.name: path.read_bytes() for path in harness.root.glob("*.json")} == before


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
@pytest.mark.parametrize("purpose", ["operator", "runtime"])
def test_verify_refuses_changed_saved_credentials(harness: Harness, purpose: str) -> None:
    harness.allocate()
    path = harness.root / resources.FILES[purpose]
    original = path.read_bytes()
    try:
        path.write_bytes(original + b" ")
        with pytest.raises(resources.ResourceRefused, match="credentials changed"):
            resources.verify(harness.root)
    finally:
        path.write_bytes(original)


@pytest.mark.parametrize("dialect", ["postgres"])
def test_verify_refuses_changed_marker_after_table_grant(harness: Harness) -> None:
    from psycopg import sql

    record = harness.allocate()
    harness.tables()
    resources.grant_tables(harness.root, {BINDING: ["events"]}, harness.admin_dsns)
    entry = record["engines"][BINDING]
    statement = sql.SQL("COMMENT ON SCHEMA {} IS {}")
    with harness.admin() as engine:
        engine._cx.execute(
            statement.format(sql.Identifier(entry["namespace"]), sql.Literal("foreign-owner"))
        )
        try:
            before = {path.name: path.read_bytes() for path in harness.root.glob("*.json")}
            with pytest.raises(resources.ResourceRefused, match="marker"):
                resources.verify(harness.root)
            assert {path.name: path.read_bytes() for path in harness.root.glob("*.json")} == before
        finally:
            engine._cx.execute(
                statement.format(
                    sql.Identifier(entry["namespace"]), sql.Literal(entry["owner_marker"])
                )
            )

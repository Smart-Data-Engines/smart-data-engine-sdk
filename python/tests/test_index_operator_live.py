"""An index is built on the tables in force while the application keeps writing to them.

Every native wait here is held deterministically instead of timed. PostgreSQL's ``CREATE INDEX
CONCURRENTLY`` waits in its last phase for every transaction with an older snapshot, so one open
REPEATABLE READ transaction holds the build there; ClickHouse runs ``MATERIALIZE INDEX`` on the
merge pool, so ``SYSTEM STOP MERGES`` holds the mutation while inserts go on (both measured,
PostgreSQL 15.19 and ClickHouse 24.8.14.39). A write that succeeds while the build is held is a
write the build did not pause.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from test_runtime_privileges_live import Roles, runtime_roles

import sde
from sde.engines.clickhouse import ClickHouseEngine
from sde.engines.postgres import PostgresEngine
from sde.local_cutover import CutoverRecoveryRequired, LocalCutover
from sde.physical import index_method

PROJECT = "1" * 32
TABLE = "initial_events"
KEYS = {"Event": ["id"]}

KEPT = {
    "postgres": {"entity": "Event", "name": "sde_i_kept_000001", "columns": ["value", "id"]},
    "clickhouse": {
        "entity": "Event",
        "name": "sde_i_kept_000001",
        "columns": ["id"],
        "method": "bloom_filter",
        "granularity": 1,
    },
}
ADDED = {
    "postgres": [{"columns": ["value"]}, {"columns": ["id"], "method": "brin"}],
    "clickhouse": [
        {"columns": ["value"], "method": "minmax", "granularity": 4},
        {"columns": ["value"], "method": "set", "max_rows": 100, "granularity": 2},
    ],
}


@dataclass
class Build:
    operator: LocalCutover
    plan: Any
    old: Any
    roles: dict[str, Roles]
    model: Any
    public: bytes
    signed: Callable[[dict[str, Any]], dict[str, Any]]
    engine: str
    root: Path

    @property
    def role(self) -> Roles:
        return self.roles[self.engine]

    @property
    def names(self) -> list[str]:
        return [str(index["name"]) for index in self.plan.added]

    def session(self) -> Any:
        """A process that loads the active map now, on the fixture's runtime logins."""
        runtime = {name: role.runtime for name, role in self.roles.items()}
        return sde.Session(self.model, self.operator.active_map(), runtime, project_id=PROJECT)

    def reconnect(self) -> LocalCutover:
        """Fresh dedicated connections after a deadline closed them, and a fresh operator."""
        for name, role in self.roles.items():
            role.operator.close()
            role.operator.connect()
            if name == "postgres":
                role.operator._cx.execute('SET search_path TO "' + role.namespace + '"')
            role.runtime.close()
            role.runtime.connect()
        self.operator = LocalCutover(
            self.root,
            model=self.model,
            project_id=PROJECT,
            public_key=self.public,
            operators={name: role.operator for name, role in self.roles.items()},
            runtime={name: [role.runtime] for name, role in self.roles.items()},
        )
        return self.operator


@contextmanager
def initial(
    engine: str, root: Path, *, kept: bool = False, added: int = 1, budget_ms: int = 60000
) -> Iterator[Build]:
    """A source-only map in force, a process writing on it, an operator, and a build authorization.

    ``kept`` gives the map in force a physical design of its own (contract 5), which the next map
    must carry unchanged; without it the next map raises the contract from 4 to 5, because the
    first index is where a physical design first appears.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    with runtime_roles("postgres") as pg, runtime_roles("clickhouse") as ch:
        roles = {"postgres": pg, "clickhouse": ch}
        sde.clear_registry()

        @sde.entity
        class Event:
            id: int
            value: sde.Int32

        model = sde.build_model(Event)
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes_raw()

        def signed(raw: dict[str, Any]) -> dict[str, Any]:
            document = deepcopy(raw)
            document.pop("signature", None)
            document["signature"] = {
                "alg": "ed25519",
                "value": base64.b64encode(key.sign(sde.canonical_bytes(document))).decode(),
            }
            return document

        layout: dict[str, Any] = {
            "tables": {"Event": TABLE},
            "columns": {
                "Event": {
                    "id": "bigint" if engine == "postgres" else "Int64",
                    "value": "integer" if engine == "postgres" else "Int32",
                }
            },
        }
        if kept:
            layout["indexes"] = [KEPT[engine]]
        current = signed(
            {
                "contract": 5 if kept else 4,
                "project_id": PROJECT,
                "model_version": model.version,
                "map_version": 1,
                "groups": {
                    "Event": {
                        "write_epoch": 1,
                        "source": {"id": "source", "engine": engine, "layout": layout},
                    }
                },
            }
        )
        parsed = sde.load_map(current, model=model, public_key=public)
        operators = {name: role.operator for name, role in roles.items()}
        runtime = {name: role.runtime for name, role in roles.items()}
        sde.prepare_schema(model, parsed, operators, project_id=PROJECT)
        roles[engine].grant(TABLE)
        for role in roles.values():
            role.grant("sde_map_state")
        old = sde.Session(model, parsed, runtime, project_id=PROJECT)
        old.save("Event", {"id": 1, "value": 11})
        operator = LocalCutover(
            root,
            model=model,
            project_id=PROJECT,
            public_key=public,
            operators=operators,
            runtime={name: [engine_] for name, engine_ in runtime.items()},
        )
        operator.enroll(current)
        identity = uuid4().hex
        prepared = deepcopy(current)
        prepared["contract"], prepared["map_version"] = 5, 2
        design = prepared["groups"]["Event"]["source"]["layout"]
        design["indexes"] = [
            *design.get("indexes", []),
            *(
                {"entity": "Event", "name": sde.index_build_name(identity, position), **shape}
                for position, shape in enumerate(ADDED[engine][:added], start=1)
            ),
        ]
        plan = sde.load_index_plan(
            signed(
                {
                    "kind": "sde-index",
                    "protocol": 1,
                    "index_id": identity,
                    "project_id": PROJECT,
                    "group": "Event",
                    "current": current,
                    "prepared": signed(prepared),
                    "build_budget_ms": budget_ms,
                }
            ),
            model=model,
            project_id=PROJECT,
            public_key=public,
        )
        yield Build(operator, plan, old, roles, model, public, signed, engine, root)


# --- the engine's own catalogue, read without the code under test ----------------------------


def pg_indexes(role: Roles) -> dict[str, tuple[bool, bool, str, str]]:
    """Non-primary indexes of the table: name -> (valid and ready, unique, method, object id)."""
    rows = role.operator._cx.execute(
        "SELECT c.relname, i.indisvalid AND i.indisready, i.indisunique, am.amname, c.oid::text "
        "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_am am ON am.oid = c.relam "
        "WHERE i.indrelid = to_regclass(%s) AND NOT i.indisprimary",
        (TABLE,),
    ).fetchall()
    return {str(row[0]): (bool(row[1]), bool(row[2]), str(row[3]), str(row[4])) for row in rows}


def ch_indexes(role: Roles) -> dict[str, str]:
    rows = role.operator._cx.query(
        "SELECT name, type_full FROM system.data_skipping_indices "
        "WHERE database = {d:String} AND table = {t:String}",
        parameters={"d": role.namespace, "t": TABLE},
    ).result_rows
    return {str(name): str(kind) for name, kind in rows}


def ch_materializations(role: Roles, name: str) -> list[bool]:
    """``is_done`` of every live MATERIALIZE INDEX mutation of ``name``, oldest first."""
    rows = role.operator._cx.query(
        "SELECT is_done FROM system.mutations WHERE database = {d:String} AND table = {t:String} "
        "AND command = {c:String} AND NOT is_killed ORDER BY create_time",
        parameters={"d": role.namespace, "t": TABLE, "c": f"MATERIALIZE INDEX {name}"},
    ).result_rows
    return [bool(done) for (done,) in rows]


def assert_built(build: Build) -> None:
    if build.engine == "postgres":
        found = pg_indexes(build.role)
        for index in build.plan.added:
            valid, unique, method, _ = found[index["name"]]
            assert valid and not unique and method == index_method(index)
    else:
        found_ch = ch_indexes(build.role)
        for index in build.plan.added:
            method = index_method(index)
            expected = f"set({index['max_rows']})" if method == "set" else method
            assert found_ch[index["name"]] == expected
            # Found again after any interruption, never started twice.
            assert ch_materializations(build.role, index["name"]) == [True]
    source = build.plan.prepared.groups["Event"].source
    assert build.role.operator.validate_schema(source.layout, keys=KEYS) == ()


def assert_absent(build: Build) -> None:
    if build.engine == "postgres":
        assert not set(build.names) & set(pg_indexes(build.role))
    else:
        assert not set(build.names) & set(ch_indexes(build.role))
        for name in build.names:
            assert all(ch_materializations(build.role, name))  # nothing left materializing
    current = build.plan.current.groups["Event"].source
    assert build.role.operator.validate_schema(current.layout, keys=KEYS) == ()


# --- holding a build deterministically ---------------------------------------------------------


@contextmanager
def admin(role: Roles) -> Iterator[Callable[..., list[tuple[Any, ...]]]]:
    """A connection of its own, for use beside the operator's, from any thread."""
    if role.operator.dialect == "postgres":
        import psycopg
        from psycopg.conninfo import make_conninfo

        connection = psycopg.connect(
            make_conninfo(role.operator._dsn, options="-csearch_path=" + role.namespace),
            autocommit=True,
        )

        def run_pg(statement: str, params: Any = None) -> list[tuple[Any, ...]]:
            cursor = connection.execute(statement, params)
            return list(cursor.fetchall()) if cursor.description else []

        try:
            yield run_pg
        finally:
            connection.close()
    else:
        engine = ClickHouseEngine(role.operator._dsn)
        engine.connect()

        def run_ch(statement: str, params: Any = None) -> list[tuple[Any, ...]]:
            if statement.startswith("SELECT"):
                return list(engine._cx.query(statement, parameters=params).result_rows)
            engine._cx.command(statement, parameters=params)
            return []

        try:
            yield run_ch
        finally:
            engine.close()


@contextmanager
def held(build: Build, run: Callable[..., Any]) -> Iterator[Callable[[], None]]:
    """Hold the engine's work on any index build of the table until ``release`` (or the exit)."""
    lock = threading.Lock()
    released = False
    if build.engine == "postgres":
        import psycopg
        from psycopg.conninfo import make_conninfo

        holder = psycopg.connect(
            make_conninfo(build.role.operator._dsn, options="-csearch_path=" + build.role.namespace)
        )
        holder.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        holder.execute("SELECT 1")  # the snapshot every later concurrent build waits for

        def release() -> None:
            nonlocal released
            with lock:
                if not released:
                    released = True
                    holder.rollback()

        try:
            yield release
        finally:
            release()
            holder.close()
    else:
        where = f"`{build.role.namespace}`.`{TABLE}`"
        run(f"SYSTEM STOP MERGES {where}")

        def release() -> None:
            nonlocal released
            with lock:
                if not released:
                    released = True
                    run(f"SYSTEM START MERGES {where}")

        try:
            yield release
        finally:
            release()


def wait_for(condition: Callable[[], Any], what: str, seconds: float = 30) -> Any:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def wait_until_held(build: Build, run: Callable[..., Any], name: str) -> int | None:
    """Block until the engine is working on ``name`` and held; the PostgreSQL backend's pid."""
    if build.engine == "postgres":
        rows = wait_for(
            lambda: run(
                "SELECT pid FROM pg_stat_progress_create_index "
                "WHERE index_relid = to_regclass(%s) AND phase = 'waiting for old snapshots'",
                [f'"{name}"'],
            ),
            "the concurrent build to wait for the old snapshot",
        )
        return int(rows[0][0])
    wait_for(
        lambda: run(
            "SELECT 1 FROM system.mutations WHERE database = currentDatabase() "
            "AND table = {t:String} AND command = {c:String} AND NOT is_done",
            {"t": TABLE, "c": f"MATERIALIZE INDEX {name}"},
        ),
        "the materialization to be pending",
    )
    return None


def unfinished(build: Build, run: Callable[..., Any], name: str) -> bool:
    if build.engine == "postgres":
        rows = run("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(%s)", [name])
        return bool(rows) and rows[0][0] is False
    return bool(
        run(
            "SELECT 1 FROM system.mutations WHERE database = currentDatabase() "
            "AND table = {t:String} AND command = {c:String} AND NOT is_done",
            {"t": TABLE, "c": f"MATERIALIZE INDEX {name}"},
        )
    )


def fresh_runtime(roles: dict[str, Roles]) -> dict[str, Any]:
    engines: dict[str, Any] = {}
    for name, role in roles.items():
        engines[name] = (PostgresEngine if name == "postgres" else ClickHouseEngine)(
            role.runtime._dsn
        )
        engines[name].connect()
    return engines


class Crash(BaseException):
    pass


def matches(step: str, checkpoint: str) -> bool:
    """``index_build:intent`` names the intent of every index's build step, whatever its name."""
    phase, _, suffix = checkpoint.partition(":")
    if phase in ("index_build", "index_drop"):
        return step.startswith(phase + "_") and step.endswith(":" + suffix)
    return step == checkpoint


def crash_at(checkpoint: str) -> Callable[[str], None]:
    def hook(step: str) -> None:
        if matches(step, checkpoint):
            raise Crash(step)

    return hook


def quiet(_step: str) -> None:
    return None


# --- the build ---------------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ["postgres", "clickhouse"])
def test_an_index_builds_on_the_live_table_while_the_application_writes(
    engine: str, tmp_path: Path
) -> None:
    with initial(engine, tmp_path) as build:
        for start in (2, 1002):  # a batch holds at most 1000 rows
            build.old.save_many(
                "Event", [{"id": i, "value": i % 97} for i in range(start, start + 1000)]
            )
        name = build.names[0]
        engines = fresh_runtime(build.roles)
        writer = sde.Session(build.model, build.plan.current, engines, project_id=PROJECT)
        during: list[int] = []
        failures: list[BaseException] = []
        try:
            with admin(build.role) as run, held(build, run) as release:

                def application() -> None:
                    try:
                        wait_until_held(build, run, name)
                        for identity in range(10_000, 10_020):
                            writer.save("Event", {"id": identity, "value": 7})
                            during.append(identity)
                        # The writes landed while the index was still being built.
                        assert unfinished(build, run, name)
                    except BaseException as exc:
                        failures.append(exc)
                    finally:
                        release()

                thread = threading.Thread(target=application)
                thread.start()
                try:
                    receipt = build.operator.index(build.plan).as_record()
                finally:
                    thread.join(timeout=60)
        finally:
            for value in engines.values():
                value.close()
        assert not failures, failures
        assert during == list(range(10_000, 10_020))
        assert receipt["outcome"] == "built" and receipt["recovered"] is False
        assert receipt["map_version"] == 2
        assert receipt["map_fingerprint"] == build.plan.prepared.fingerprint
        assert [row["name"] for row in receipt["indexes"]] == build.names
        rows = receipt["indexes"]
        assert {(row["engine"], row["entity"], row["table"]["name"]) for row in rows} == {
            (engine, "Event", TABLE)
        }
        active = build.operator.active_map()
        assert active.fingerprint == build.plan.prepared.fingerprint
        assert active.groups["Event"].write_epoch == 1  # nobody was fenced
        status = build.operator.status()
        assert (status["active_map_version"], status["recorded_map_version"]) == (2, 2)
        assert status["plan_id"] is None
        assert_built(build)
        # The process on the map that was in force never noticed: same table, same generation.
        build.old.save("Event", {"id": 20_000, "value": 1})
        assert build.old.get("Event", {"id": 20_000}) == {"id": 20_000, "value": 1}
        fresh = build.session()
        assert fresh.count("Event") == 1 + 2000 + 20 + 1
        assert fresh.get("Event", {"id": 10_005}) == {"id": 10_005, "value": 7}
        assert build.operator.index(build.plan).as_record() == receipt
        envelope = json.loads(build.operator.store.path.read_text())
        assert envelope["storage_contract"] == 3
        assert envelope["payload"]["indexes"][build.plan.index_id]["receipt"] == receipt


@pytest.mark.parametrize("engine", ["postgres", "clickhouse"])
def test_kept_indexes_stay_and_several_new_ones_build(engine: str, tmp_path: Path) -> None:
    with initial(engine, tmp_path, kept=True, added=2) as build:
        assert build.plan.current.contract == 5
        receipt = build.operator.index(build.plan).as_record()
        assert receipt["outcome"] == "built"
        assert [row["name"] for row in receipt["indexes"]] == build.names
        assert len(build.names) == 2
        assert_built(build)
        kept = KEPT[engine]["name"]
        assert kept in (pg_indexes(build.role) if engine == "postgres" else ch_indexes(build.role))


# --- recovery ----------------------------------------------------------------------------------

CHECKPOINTS = [
    "index_prepared",
    "index_build:intent",
    "index_build:done",
    "index_qualify:intent",
    "index_qualify:done",
    "index_decision",
    "index_watermarks:intent",
    "index_watermarks:done",
    "index_publish:intent",
    "index_publish:done",
]


@pytest.mark.parametrize("checkpoint", CHECKPOINTS)
@pytest.mark.parametrize("engine", ["postgres", "clickhouse"])
def test_the_build_resumes_after_every_checkpoint(
    engine: str, checkpoint: str, tmp_path: Path
) -> None:
    with initial(engine, tmp_path) as build:
        build.operator._after_step = crash_at(checkpoint)
        with pytest.raises(Crash):
            build.operator.index(build.plan)
        build.operator._after_step = quiet
        build.old.save("Event", {"id": 2, "value": 22})  # nothing is paused between attempts
        with pytest.raises(CutoverRecoveryRequired, match="requires resume"):
            build.operator.index(build.plan)
        receipt = build.operator.resume().as_record()
        assert receipt["outcome"] == "built" and receipt["recovered"] is True
        assert build.operator.active_map().fingerprint == build.plan.prepared.fingerprint
        assert_built(build)
        build.old.save("Event", {"id": 3, "value": 33})
        assert build.session().get("Event", {"id": 3}) == {"id": 3, "value": 33}
        assert build.operator.index(build.plan).as_record() == receipt


def test_a_crash_after_completion_leaves_only_the_receipt(tmp_path: Path) -> None:
    with initial("postgres", tmp_path) as build:
        build.operator._after_step = crash_at("index_complete")
        with pytest.raises(Crash):
            build.operator.index(build.plan)
        build.operator._after_step = quiet
        with pytest.raises(sde.MigrationRefused, match="no unfinished local operation"):
            build.operator.resume()
        receipt = build.operator.index(build.plan).as_record()
        assert receipt["outcome"] == "built" and receipt["recovered"] is False
        assert_built(build)


# --- abandonment -------------------------------------------------------------------------------

BEFORE_DECISION = CHECKPOINTS[:5]
AFTER_DECISION = CHECKPOINTS[5:]


@pytest.mark.parametrize("checkpoint", BEFORE_DECISION)
@pytest.mark.parametrize("engine", ["postgres", "clickhouse"])
def test_abandoning_before_the_decision_removes_our_indexes_and_keeps_the_map(
    engine: str, checkpoint: str, tmp_path: Path
) -> None:
    with initial(engine, tmp_path) as build:
        build.operator._after_step = crash_at(checkpoint)
        with pytest.raises(Crash):
            build.operator.index(build.plan)
        build.operator._after_step = quiet
        receipt = build.operator.abandon().as_record()
        assert receipt["outcome"] == "abandoned" and receipt["recovered"] is True
        assert receipt["map_version"] == 1
        assert receipt["map_fingerprint"] == build.plan.current.fingerprint
        assert build.operator.active_map().fingerprint == build.plan.current.fingerprint
        status = build.operator.status()
        assert (status["active_map_version"], status["recorded_map_version"]) == (1, 1)
        assert_absent(build)
        build.old.save("Event", {"id": 2, "value": 22})
        assert build.old.get("Event", {"id": 2}) == {"id": 2, "value": 22}
        # The authorization is spent: the same build answers with its abandonment.
        assert build.operator.index(build.plan).as_record() == receipt
        with pytest.raises(sde.MigrationRefused, match="no unfinished index build"):
            build.operator.abandon()


@pytest.mark.parametrize("checkpoint", AFTER_DECISION)
def test_a_decided_build_cannot_be_abandoned(checkpoint: str, tmp_path: Path) -> None:
    with initial("postgres", tmp_path) as build:
        build.operator._after_step = crash_at(checkpoint)
        with pytest.raises(Crash):
            build.operator.index(build.plan)
        build.operator._after_step = quiet
        with pytest.raises(sde.MigrationRefused, match="cannot be abandoned"):
            build.operator.abandon()
        assert build.operator.store.read()["execution"]["decision"] == "built"
        assert build.operator.resume().as_record()["outcome"] == "built"
        assert_built(build)


@pytest.mark.parametrize(
    ("checkpoint", "finish"),
    [
        ("index_abandoned", "resume"),
        ("index_abandoned", "abandon"),
        ("index_drop:intent", "resume"),
        ("index_drop:done", "abandon"),
    ],
)
@pytest.mark.parametrize("engine", ["postgres", "clickhouse"])
def test_an_interrupted_abandonment_completes_on_resume_or_retry(
    engine: str, checkpoint: str, finish: str, tmp_path: Path
) -> None:
    with initial(engine, tmp_path) as build:
        build.operator._after_step = crash_at("index_build:done")
        with pytest.raises(Crash):
            build.operator.index(build.plan)
        build.operator._after_step = crash_at(checkpoint)
        with pytest.raises(Crash):
            build.operator.abandon()
        build.operator._after_step = quiet
        # Once decided, an abandonment stays one: a recovery removes, it never builds again.
        assert build.operator.store.read()["execution"]["decision"] == "abandoned"
        finished = build.operator.resume() if finish == "resume" else build.operator.abandon()
        receipt = finished.as_record()
        assert receipt["outcome"] == "abandoned"
        assert build.operator.active_map().fingerprint == build.plan.current.fingerprint
        assert_absent(build)


def test_there_is_nothing_to_abandon_without_an_unfinished_build(tmp_path: Path) -> None:
    with (
        initial("postgres", tmp_path) as build,
        pytest.raises(sde.MigrationRefused, match="no unfinished index build"),
    ):
        build.operator.abandon()


# --- what the build refuses --------------------------------------------------------------------

FOREIGN = [
    ("postgres", "relation", ['CREATE TABLE "{name}" (x integer)'], "another relation"),
    (
        "postgres",
        "other_table",
        [
            "CREATE TABLE other_events (value integer)",
            'CREATE INDEX "{name}" ON other_events (value)',
        ],
        "not ours",
    ),
    ("postgres", "unique", ['CREATE UNIQUE INDEX "{name}" ON initial_events (value)'], "not ours"),
    (
        "postgres",
        "method",
        ['CREATE INDEX "{name}" ON initial_events USING hash (value)'],
        "not ours",
    ),
    (
        "postgres",
        "partial",
        ['CREATE INDEX "{name}" ON initial_events (value) WHERE value > 0'],
        "not ours",
    ),
    (
        "clickhouse",
        "type",
        ["ALTER TABLE initial_events ADD INDEX {name} value TYPE set(10) GRANULARITY 4"],
        "not ours",
    ),
    (
        "clickhouse",
        "column",
        ["ALTER TABLE initial_events ADD INDEX {name} id TYPE minmax GRANULARITY 4"],
        "not ours",
    ),
    (
        "clickhouse",
        "granularity",
        ["ALTER TABLE initial_events ADD INDEX {name} value TYPE minmax GRANULARITY 1"],
        "not ours",
    ),
]


@pytest.mark.parametrize(
    ("engine", "case", "statements", "message"), FOREIGN, ids=[row[1] for row in FOREIGN]
)
def test_a_foreign_object_under_the_bound_name_is_refused_before_any_ddl(
    engine: str, case: str, statements: list[str], message: str, tmp_path: Path
) -> None:
    with initial(engine, tmp_path) as build:
        name = build.names[0]
        for statement in statements:
            build.role.command(statement.format(name=name))
        before = pg_indexes(build.role) if engine == "postgres" else ch_indexes(build.role)
        with pytest.raises(sde.MigrationRefused, match=message):
            build.operator.index(build.plan)
        assert build.operator.store.read()["execution"] is None
        assert build.operator.active_map().fingerprint == build.plan.current.fingerprint
        after = pg_indexes(build.role) if engine == "postgres" else ch_indexes(build.role)
        assert after == before  # neither adopted nor removed


def test_a_foreign_index_that_appears_mid_build_stops_publication_not_abandonment(
    tmp_path: Path,
) -> None:
    with initial("postgres", tmp_path) as build:
        build.operator._after_step = crash_at("index_build:intent")
        with pytest.raises(Crash):
            build.operator.index(build.plan)
        build.operator._after_step = quiet
        name = build.names[0]
        build.role.command(f'CREATE UNIQUE INDEX "{name}" ON {TABLE} (value)')
        with pytest.raises(CutoverRecoveryRequired) as refused:
            build.operator.resume()
        assert isinstance(refused.value.__cause__, sde.MigrationRefused)
        assert "not ours" in str(refused.value.__cause__)
        assert build.operator.active_map().fingerprint == build.plan.current.fingerprint
        assert build.operator.abandon().as_record()["outcome"] == "abandoned"
        assert pg_indexes(build.role)[name][1] is True  # somebody else's unique index, untouched


@pytest.mark.parametrize("engine", ["postgres", "clickhouse"])
def test_another_operations_barrier_refuses_the_build_until_it_is_released(
    engine: str, tmp_path: Path
) -> None:
    with initial(engine, tmp_path) as build:
        fence = build.role.operator.write_fence(TABLE, project_id=PROJECT)
        fence.freeze("e" * 32)
        try:
            with pytest.raises(sde.MigrationRefused, match="without another barrier"):
                build.operator.index(build.plan)
            assert build.operator.store.read()["execution"] is None
            assert_absent(build)
        finally:
            fence.release("e" * 32)
        assert build.operator.index(build.plan).as_record()["outcome"] == "built"


@pytest.mark.parametrize("engine", ["postgres", "clickhouse"])
def test_a_barrier_that_appears_mid_build_stops_publication_not_abandonment(
    engine: str, tmp_path: Path
) -> None:
    with initial(engine, tmp_path) as build:
        build.operator._after_step = crash_at("index_build:done")
        with pytest.raises(Crash):
            build.operator.index(build.plan)
        build.operator._after_step = quiet
        fence = build.role.operator.write_fence(TABLE, project_id=PROJECT)
        fence.freeze("e" * 32)
        try:
            with pytest.raises(CutoverRecoveryRequired) as refused:
                build.operator.resume()
            assert isinstance(refused.value.__cause__, sde.MigrationRefused)
            assert build.operator.active_map().fingerprint == build.plan.current.fingerprint
            assert build.operator.abandon().as_record()["outcome"] == "abandoned"
            assert "e" * 32 in fence.state().holds  # the other operation's barrier stands
            assert_absent(build)
        finally:
            fence.release("e" * 32)


def test_an_index_in_force_lost_during_the_build_stops_publication(tmp_path: Path) -> None:
    """Qualification reads the whole next design back, not only the indexes it built.

    Somebody dropped an index the map in force declares while the build was under way: the next
    map declares it too, and publishing it would promise a design the engine does not hold.
    """
    with initial("postgres", tmp_path, kept=True) as build:
        build.operator._after_step = crash_at("index_build:done")
        with pytest.raises(Crash):
            build.operator.index(build.plan)
        build.operator._after_step = quiet
        build.role.command(f'DROP INDEX "{KEPT["postgres"]["name"]}"')
        with pytest.raises(CutoverRecoveryRequired) as refused:
            build.operator.resume()
        assert isinstance(refused.value.__cause__, sde.MigrationRefused)
        assert KEPT["postgres"]["name"] in str(refused.value.__cause__)
        assert build.operator.active_map().fingerprint == build.plan.current.fingerprint
        assert build.operator.abandon().as_record()["outcome"] == "abandoned"


# --- the engines' own unfinished work ----------------------------------------------------------


def test_our_unfinished_postgres_leftover_is_reported_dropped_and_built_again(
    tmp_path: Path,
) -> None:
    import psycopg

    with initial("postgres", tmp_path) as build:
        build.operator._after_step = crash_at("index_build:intent")
        with pytest.raises(Crash):
            build.operator.index(build.plan)
        build.operator._after_step = quiet
        name = build.names[0]
        with admin(build.role) as run, held(build, run):
            run("SET statement_timeout = '1s'")
            with pytest.raises(psycopg.errors.QueryCanceled):
                run(f'CREATE INDEX CONCURRENTLY "{name}" ON {TABLE} (value)')
        leftover = pg_indexes(build.role)[name]
        assert leftover[0] is False  # what an interrupted concurrent build leaves
        layout = build.plan.prepared.groups["Event"].source.layout
        assert {
            finding.aspect: finding.found
            for finding in build.role.operator.validate_schema(layout, keys=KEYS)
        } == {f"index {name}": "btree on ['value'], not valid (an unfinished concurrent build)"}
        receipt = build.operator.resume().as_record()
        assert receipt["outcome"] == "built" and receipt["recovered"] is True
        assert_built(build)
        assert pg_indexes(build.role)[name][3] != leftover[3]  # built again, not adopted


def spawn(build: Build, checkpoint: str) -> Any:
    import subprocess
    import sys

    from psycopg.conninfo import make_conninfo

    payload = {
        "directory": str(build.root),
        "project_id": PROJECT,
        "model": sde.neutral_declaration(build.model),
        "public_key": build.public.hex(),
        "plan": build.plan.as_record(),
        "checkpoint": checkpoint,
        "operators": {
            name: (
                make_conninfo(role.operator._dsn, options="-csearch_path=" + role.namespace)
                if name == "postgres"
                else role.operator._dsn
            )
            for name, role in build.roles.items()
        },
        "runtime": {name: role.runtime._dsn for name, role in build.roles.items()},
    }
    worker = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("_index_worker.py"))],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert worker.stdin is not None
    worker.stdin.write(json.dumps(payload))
    worker.stdin.close()
    return worker


def expect_line(worker: Any, line: str) -> None:
    import select

    assert worker.stdout is not None
    assert select.select([worker.stdout], [], [], 30)[0], f"worker never printed {line}"
    found = worker.stdout.readline()
    if found != line + "\n":
        assert worker.stderr is not None
        raise AssertionError(found + worker.stderr.read())


def kill(worker: Any) -> None:
    import os
    import signal

    if worker.poll() is None:
        os.kill(worker.pid, signal.SIGKILL)
    worker.wait(timeout=10)


@pytest.mark.parametrize("engine", ["postgres", "clickhouse"])
def test_a_killed_operator_mid_build_resumes_the_same_build(engine: str, tmp_path: Path) -> None:
    with initial(engine, tmp_path) as build:
        build.old.save_many("Event", [{"id": i, "value": i % 13} for i in range(2, 502)])
        name = build.names[0]
        leftover = None
        with admin(build.role) as run, held(build, run) as release:
            worker = spawn(build, "native")
            try:
                expect_line(worker, "STARTED")
                pid = wait_until_held(build, run, name)
                build.old.save("Event", {"id": 5000, "value": 5})  # writes go on mid-build
                kill(worker)
                if pid is not None:
                    # The server notices a vanished client only when it next talks to it; stand
                    # in for that moment, which is when the build is abandoned server-side.
                    run("SELECT pg_terminate_backend(%s)", [pid])
                    wait_for(
                        lambda: not run("SELECT 1 FROM pg_stat_activity WHERE pid = %s", [pid]),
                        "the killed build's backend to exit",
                    )
            finally:
                kill(worker)
            if engine == "postgres":
                release()
                leftover = pg_indexes(build.role)[name]
                assert leftover[0] is False
            else:
                # Resumed while the killed process's materialization is still held: recovery must
                # find that mutation and wait for it - not start a second one, and not publish
                # before it is done. The pool is released only once the build step is under way.
                def release_during_the_build(step: str) -> None:
                    if matches(step, "index_build:intent"):
                        threading.Timer(1.0, release).start()

                build.operator._after_step = release_during_the_build
            receipt = build.operator.resume().as_record()
            build.operator._after_step = quiet
        assert receipt["outcome"] == "built" and receipt["recovered"] is True
        assert_built(build)
        if leftover is not None:
            assert pg_indexes(build.role)[name][3] != leftover[3]
        assert build.session().get("Event", {"id": 5000}) == {"id": 5000, "value": 5}


@pytest.mark.parametrize("engine", ["postgres", "clickhouse"])
def test_a_killed_operator_between_watermark_and_publication_publishes_on_resume(
    engine: str, tmp_path: Path
) -> None:
    with initial(engine, tmp_path) as build:
        worker = spawn(build, "index_publish:intent")
        try:
            expect_line(worker, "READY")
            kill(worker)
        finally:
            kill(worker)
        assert build.operator.active_map().fingerprint == build.plan.current.fingerprint
        receipt = build.operator.resume().as_record()
        assert receipt["outcome"] == "built" and receipt["recovered"] is True
        assert build.operator.active_map().fingerprint == build.plan.prepared.fingerprint
        assert_built(build)


@pytest.mark.parametrize("engine", ["postgres", "clickhouse"])
def test_the_build_budget_bounds_a_held_build_and_recovery_finishes_it(
    engine: str, tmp_path: Path
) -> None:
    # The first build is held for good, so any budget ends it; the resume after the release has
    # to fit a DROP and a CREATE INDEX CONCURRENTLY into the same signed budget, which 2.5 s did
    # not always leave on a loaded two-core machine.
    with initial(engine, tmp_path, budget_ms=6000) as build:
        with admin(build.role) as run, held(build, run) as release:
            started = time.monotonic()
            with pytest.raises(CutoverRecoveryRequired, match="build budget"):
                build.operator.index(build.plan)
            assert time.monotonic() - started < 20
            operator = build.reconnect()
            if engine == "clickhouse":
                # Recovery is bounded by the same signed budget, not by the 30 s watchdog.
                started = time.monotonic()
                with pytest.raises(CutoverRecoveryRequired, match="deadline"):
                    operator.resume()
                assert time.monotonic() - started < 20
                operator = build.reconnect()
                # Abandoned while its materialization is still held: the mutation is killed and
                # the index leaves the catalogue without waiting for the merge pool.
                receipt = operator.abandon().as_record()
                assert receipt["outcome"] == "abandoned"
                assert_absent(build)
            release()
        if engine == "postgres":
            receipt = operator.resume().as_record()
            assert receipt["outcome"] == "built" and receipt["recovered"] is True
            assert_built(build)


def test_the_build_adds_its_own_history_and_leaves_the_rest_untouched(tmp_path: Path) -> None:
    """History from before stays as it was: the build adds its own record and nothing else."""
    with initial("postgres", tmp_path) as build:
        before = build.operator.store.read()
        receipt = build.operator.index(build.plan).as_record()
        after = build.operator.store.read()
        assert after["completed"] == before["completed"]
        assert after["retired_names"] == before["retired_names"]
        assert after["stages"] == before.get("stages", {})
        assert list(after["indexes"]) == [build.plan.index_id]
        assert after["indexes"][build.plan.index_id]["receipt"] == receipt


# --- the operator command line -----------------------------------------------------------------


def operator_cli(build: Build, directory: Path) -> tuple[list[str], dict[str, str]]:
    import os
    import sys

    from psycopg.conninfo import make_conninfo

    config: dict[str, Any] = {
        "protocol": 1,
        "project_id": PROJECT,
        "model": sde.neutral_declaration(build.model),
        "public_keys": {"primary": base64.b64encode(build.public).decode()},
        "engines": {},
    }
    environment = dict(os.environ)
    for name, role in build.roles.items():
        op_name, run_name = "SDE_INDEX_" + name.upper(), "SDE_INDEX_APP_" + name.upper()
        operator_dsn = role.operator._dsn
        if name == "postgres":
            operator_dsn = make_conninfo(operator_dsn, options="-csearch_path=" + role.namespace)
        environment[op_name], environment[run_name] = operator_dsn, role.runtime._dsn
        config["engines"][name] = {
            "dialect": name,
            "operator_dsn_env": op_name,
            "runtime_dsn_envs": [run_name],
        }
    config_path = directory / "config.json"
    config_path.write_text(json.dumps(config))
    base = [
        sys.executable,
        "-m",
        "sde_operator",
        "--project-dir",
        str(build.operator.store.root),
        "--config",
        str(config_path),
    ]
    return base, environment


def run_cli(command: list[str], environment: dict[str, str]) -> tuple[int, Any]:
    import subprocess

    done = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=90)
    return done.returncode, json.loads(done.stdout if done.returncode == 0 else done.stderr)


@pytest.mark.parametrize("engine", ["postgres", "clickhouse"])
def test_the_operator_cli_builds_and_repeats_the_same_receipt(engine: str, tmp_path: Path) -> None:
    with initial(engine, tmp_path / "state") as build:
        base, environment = operator_cli(build, tmp_path)
        packet = tmp_path / "index.json"
        packet.write_text(json.dumps(build.plan.as_record()))
        results = [run_cli([*base, "index", "--plan", str(packet)], environment) for _ in range(2)]
        assert [code for code, _ in results] == [0, 0]
        assert results[0][1] == results[1][1]
        assert results[0][1]["outcome"] == "built"
        assert_built(build)
        stored = build.operator.store.path.read_text() + json.dumps(results)
        for role in build.roles.values():
            assert role.runtime._dsn not in stored


def test_the_operator_cli_abandons_an_unfinished_build_once(tmp_path: Path) -> None:
    with initial("clickhouse", tmp_path / "state") as build:
        build.operator._after_step = crash_at("index_build:done")
        with pytest.raises(Crash):
            build.operator.index(build.plan)
        base, environment = operator_cli(build, tmp_path)
        code, receipt = run_cli([*base, "abandon"], environment)
        assert code == 0 and receipt["outcome"] == "abandoned"
        assert_absent(build)
        code, refusal = run_cli([*base, "abandon"], environment)
        assert code == 2 and refusal == {
            "error": "refused",
            "message": "there is no unfinished index build or staging to abandon",
        }

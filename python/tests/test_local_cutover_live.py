"""Execute local cutover with lost fan-out and recovery from durable checkpoints."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from test_runtime_privileges_live import runtime_roles

import sde
from sde.local_cutover import LocalCutover
from sde.placement import WATERMARK_TABLE

PROJECT = "1" * 32


@contextmanager
def fixture(source: str, root: Path, *, budget_ms: int = 30000) -> Iterator[tuple[Any, ...]]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    with runtime_roles("postgres") as pg, runtime_roles("clickhouse") as ch:
        roles = {"postgres": pg, "clickhouse": ch}
        operators = {name: value.operator for name, value in roles.items()}
        runtime = {name: value.runtime for name, value in roles.items()}
        target = "clickhouse" if source == "postgres" else "postgres"
        sde.clear_registry()

        @sde.entity
        class Event:
            id: int
            value: sde.Int32

        model = sde.build_model(Event)
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes_raw()

        def signed(body: dict[str, Any]) -> dict[str, Any]:
            body = deepcopy(body)
            body.pop("signature", None)
            body["signature"] = {
                "alg": "ed25519",
                "value": base64.b64encode(key.sign(sde.canonical_bytes(body))).decode(),
            }
            return body

        def material(name: str, identity: str) -> dict[str, Any]:
            return {
                "engine": name,
                "id": identity,
                "layout": {
                    "tables": {"Event": identity + "_events"},
                    "columns": {
                        "Event": {
                            "id": "bigint" if name == "postgres" else "Int64",
                            "value": "integer" if name == "postgres" else "Int32",
                        }
                    },
                },
            }

        before = signed(
            {
                "contract": 4,
                "project_id": PROJECT,
                "model_version": model.version,
                "map_version": 1,
                "groups": {
                    "Event": {
                        "write_epoch": 1,
                        "source": material(source, "source"),
                        "derived": [{**material(target, "copy"), "lag_budget_ms": 60000}],
                        "also_write": ["copy"],
                    }
                },
            }
        )
        success, abort = deepcopy(before), deepcopy(before)
        success["map_version"], abort["map_version"] = 2, 3
        success["groups"]["Event"] = {"write_epoch": 3, "source": material(target, "copy")}
        abort["groups"]["Event"] = {"write_epoch": 2, "source": material(source, "source")}
        success, abort = signed(success), signed(abort)
        parsed = sde.load_map(before, model=model, public_key=public)
        request = sde.verification_request(
            parsed,
            group="Event",
            project_id=PROJECT,
            request_id=uuid4().hex,
            requested_at=datetime.now(UTC).isoformat(),
        )
        packet = signed(
            {
                "kind": "sde-cutover",
                "protocol": 1,
                "plan_id": uuid4().hex,
                "project_id": PROJECT,
                "group": "Event",
                "pause_budget_ms": budget_ms,
                "query_impact_digest": "a" * 64,
                "before": before,
                "success": success,
                "abort": abort,
                "verification": request.as_record(),
            }
        )
        plan = sde.load_cutover_plan(packet, model=model, project_id=PROJECT, public_key=public)
        sde.prepare_schema(model, parsed, operators, project_id=PROJECT)
        roles[source].grant("source_events")
        roles[target].grant("copy_events")
        for value in roles.values():
            value.grant(WATERMARK_TABLE)
        old = sde.Session(model, parsed, runtime, project_id=PROJECT)
        old.save("Event", {"id": 1, "value": 11})
        old.save("Event", {"id": 2, "value": 22})
        executor = LocalCutover(
            root,
            model=model,
            project_id=PROJECT,
            public_key=public,
            operators=operators,
            runtime={name: [value] for name, value in runtime.items()},
            chunk_rows=1,
        )
        executor.enroll(before)
        try:
            yield executor, plan, old, roles, source, target, model, public
        finally:
            # A crash can leave a permanently detached table. Restore only the test's own objects
            # before the enclosing namespace fixture removes them.
            for value in roles.values():
                if value.operator.dialect == "clickhouse":
                    tables = value.operator._cx.query(
                        "SELECT table FROM system.detached_tables WHERE database=currentDatabase()"
                    ).result_rows
                    for (table,) in tables:
                        value.operator._cx.command(
                            "ATTACH TABLE `" + table.replace("`", "``") + "`"
                        )


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_cutover_repairs_lost_fanout_and_rejects_old_reads_and_writes(
    source: str, tmp_path: Path
) -> None:
    with fixture(source, tmp_path) as (executor, plan, old, roles, _, target, model, _public):
        actual = roles[target].runtime.insert

        def failed_copy(_table: str, _values: Any) -> None:
            raise sde.EngineError("controlled missing copy")

        roles[target].runtime.insert = failed_copy
        try:
            old.save("Event", {"id": 3, "value": 33})
        finally:
            roles[target].runtime.insert = actual
        assert roles[target].operator.get("copy_events", {"id": 3}) is None
        receipt = executor.execute(plan).as_record()
        assert receipt["outcome"] == "success"
        assert receipt["verification"]["matched"] is True
        assert receipt["within_budget"] is True
        current = sde.Session(
            model,
            executor.active_map(),
            {name: value.runtime for name, value in roles.items()},
            project_id=PROJECT,
        )
        for identity in (1, 2, 3):
            assert current.get("Event", {"id": identity}) == {
                "id": identity,
                "value": identity * 11,
            }
        with pytest.raises(sde.EngineError):
            old.get("Event", {"id": 1})
        with pytest.raises(sde.EngineError):
            old.save("Event", {"id": 4, "value": 44})
        with pytest.raises(sde.EngineError):
            roles[target].runtime.insert(
                "copy_events", {"id": 5, "value": 55, sde.WRITE_EPOCH_COLUMN: 1}
            )
        current.save("Event", {"id": 6, "value": 66})
        assert current.get("Event", {"id": 6}) == {"id": 6, "value": 66}
        assert executor.execute(plan).as_record() == receipt
        retired = roles[source].operator.write_fence("source_events", project_id=PROJECT)
        with pytest.raises(sde.MigrationRefused, match="backwards"):
            retired.advance(plan.maintenance_epoch)


class Crash(BaseException):
    pass


BEFORE_DECISION = ["prepared"] + [
    phase + suffix
    for phase in (
        "deny_target",
        "freeze_target",
        "freeze_source",
        "deny_source",
        "maintenance_epoch",
        "release_maintenance",
        "repair",
        "verify_frozen",
    )
    for suffix in (":intent", ":done")
]
AFTER_DECISION = ["decision:success"] + [
    phase + suffix
    for phase in (
        "confirm_source_denied",
        "retire_source",
        "activate_target",
        "watermarks",
        "publish_map",
        "enable_runtime",
        "release_runtime",
    )
    for suffix in (":intent", ":done")
]


@pytest.mark.parametrize("checkpoint", BEFORE_DECISION + AFTER_DECISION)
def test_restart_obeys_the_durable_decision(checkpoint: str, tmp_path: Path) -> None:
    with fixture("postgres", tmp_path) as (
        executor,
        plan,
        _old,
        roles,
        source,
        target,
        model,
        public,
    ):

        def crash(step: str) -> None:
            if step == checkpoint:
                raise Crash(step)

        executor._after_step = crash
        with pytest.raises(Crash):
            executor.execute(plan)
        recovered = LocalCutover(
            tmp_path,
            model=model,
            project_id=PROJECT,
            public_key=public,
            operators={name: value.operator for name, value in roles.items()},
            runtime={name: [value.runtime] for name, value in roles.items()},
            chunk_rows=1,
        )
        receipt = recovered.resume().as_record()
        expected = "abort" if checkpoint in BEFORE_DECISION else "success"
        assert receipt["outcome"] == expected
        assert receipt["recovered"] is True
        assert receipt["within_budget"] is False
        active = sde.Session(
            model,
            recovered.active_map(),
            {name: value.runtime for name, value in roles.items()},
            project_id=PROJECT,
        )
        assert active.get("Event", {"id": 1}) == {"id": 1, "value": 11}
        active.save("Event", {"id": 3, "value": 33})
        assert active.get("Event", {"id": 3}) == {"id": 3, "value": 33}
        retired = source if expected == "success" else target
        table = "source_events" if expected == "success" else "copy_events"
        with pytest.raises(sde.EngineError):
            roles[retired].runtime.get(table, {"id": 1})


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_a_mismatching_repair_aborts_without_changing_authority(
    source: str, tmp_path: Path
) -> None:
    with fixture(source, tmp_path) as (executor, plan, old, roles, _, target, model, _public):
        actual = roles[target].operator.copy_in

        def omit_one(table: str, rows: Any) -> None:
            actual(table, [row for row in rows if row["id"] != 2])

        roles[target].operator.copy_in = omit_one
        try:
            receipt = executor.execute(plan).as_record()
        finally:
            roles[target].operator.copy_in = actual
        assert receipt["outcome"] == "abort"
        assert receipt["reason"] == "verification_mismatch"
        assert receipt["verification"]["matched"] is False
        active = sde.Session(
            model,
            executor.active_map(),
            {name: value.runtime for name, value in roles.items()},
            project_id=PROJECT,
        )
        assert active.get("Event", {"id": 2}) == {"id": 2, "value": 22}
        with pytest.raises(sde.EngineError):
            old.save("Event", {"id": 3, "value": 33})
        with pytest.raises(sde.EngineError):
            roles[target].runtime.get("copy_events", {"id": 1})


def test_a_lost_response_after_completion_returns_the_same_receipt(tmp_path: Path) -> None:
    with fixture("postgres", tmp_path) as (
        executor,
        plan,
        _old,
        _roles,
        _source,
        _target,
        _model,
        _key,
    ):

        def crash(step: str) -> None:
            if step == "completed":
                raise Crash(step)

        executor._after_step = crash
        with pytest.raises(Crash):
            executor.execute(plan)
        executor._after_step = lambda _step: None
        first = executor.execute(plan).as_record()
        second = executor.execute(plan).as_record()
        assert first == second and first["outcome"] == "success"
        assert executor.status()["plan_id"] is None


@pytest.mark.parametrize("checkpoint", ["repair:done", "activate_target:done", "native:detach"])
def test_hard_killed_worker_releases_project_lock_and_recovers(
    checkpoint: str, tmp_path: Path
) -> None:
    import json
    import os
    import select
    import signal
    import subprocess
    import sys

    from psycopg.conninfo import make_conninfo

    with fixture("postgres", tmp_path) as (
        _executor,
        plan,
        _old,
        roles,
        _source,
        _target,
        model,
        public,
    ):
        operator_dsns = {
            "postgres": make_conninfo(
                roles["postgres"].operator._dsn,
                options="-csearch_path=" + roles["postgres"].namespace,
            ),
            "clickhouse": roles["clickhouse"].operator._dsn,
        }
        payload = {
            "operators": operator_dsns,
            "runtime": {name: role.runtime._dsn for name, role in roles.items()},
            "model": sde.neutral_declaration(model),
            "public_key": public.hex(),
            "plan": plan.as_record(),
            "project_id": PROJECT,
            "directory": str(tmp_path),
            "checkpoint": checkpoint,
        }
        worker = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("_cutover_worker.py"))],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        lock_probe = None
        try:
            assert worker.stdin is not None and worker.stdout is not None
            worker.stdin.write(json.dumps(payload))
            worker.stdin.close()
            readable, _, _ = select.select([worker.stdout], [], [], 25)
            assert readable, "worker did not reach its durable checkpoint"
            line = worker.stdout.readline()
            if line != "READY\n":
                assert worker.stderr is not None
                raise AssertionError(worker.stderr.read())
            lock_code = (
                "from pathlib import Path; import sys; from sde._local_state import transaction; "
                "guard=transaction(Path(sys.argv[1])); guard.__enter__(); "
                "print('LOCKED',flush=True); guard.__exit__(None,None,None)"
            )
            lock_probe = subprocess.Popen(
                [sys.executable, "-c", lock_code, str(tmp_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert lock_probe.stdout is not None
            readable, _, _ = select.select([lock_probe.stdout], [], [], 0.2)
            assert not readable, "a second process entered the active executor's project lock"
            os.kill(worker.pid, signal.SIGKILL)
            worker.wait(timeout=10)
            output, error = lock_probe.communicate(timeout=10)
            assert lock_probe.returncode == 0 and output == "LOCKED\n", error
            recovered = LocalCutover(
                tmp_path,
                model=model,
                project_id=PROJECT,
                public_key=public,
                operators={name: value.operator for name, value in roles.items()},
                runtime={name: [value.runtime] for name, value in roles.items()},
                chunk_rows=1,
            )
            receipt = recovered.resume().as_record()
            assert receipt["outcome"] == (
                "success" if checkpoint == "activate_target:done" else "abort"
            )
            active = sde.Session(
                model,
                recovered.active_map(),
                {name: value.runtime for name, value in roles.items()},
                project_id=PROJECT,
            )
            assert active.get("Event", {"id": 2}) == {"id": 2, "value": 22}
        finally:
            if worker.poll() is None:
                os.kill(worker.pid, signal.SIGKILL)
                worker.wait(timeout=10)
            if lock_probe is not None and lock_probe.poll() is None:
                lock_probe.kill()
                lock_probe.wait(timeout=10)


@pytest.mark.parametrize("where", ["postgres", "clickhouse", "after_decision"])
def test_wall_clock_watchdog_interrupts_waiting_and_preserves_recovery(
    where: str, tmp_path: Path
) -> None:
    import time

    from sde.local_cutover import CutoverRecoveryRequired

    with fixture("postgres", tmp_path, budget_ms=10000) as (
        executor,
        plan,
        _old,
        roles,
        _source,
        _target,
        model,
        public,
    ):
        checkpoint = "retire_source:intent" if where == "after_decision" else "repair:intent"
        reached = []

        def delay(step: str) -> None:
            if step != checkpoint:
                return
            reached.append((step, time.monotonic()))
            if where == "clickhouse":
                roles["clickhouse"].operator._cx.query(
                    "SELECT sleep(20)",
                    settings={"function_sleep_max_microseconds_per_block": 25000000},
                )
            else:
                roles["postgres"].operator._cx.execute("SELECT pg_sleep(20)")

        executor._after_step = delay
        try:
            with pytest.raises(CutoverRecoveryRequired, match="deadline"):
                executor.execute(plan)
            assert len(reached) == 1 and reached[0][0] == checkpoint
            elapsed = time.monotonic() - reached[0][1]
            # Give setup/verification enough room to reach the injected phase under CI load.
            # The actual native wait must still be interrupted well before its 20-second end.
            assert elapsed < 15, f"watchdog did not interrupt the query: {elapsed}"
        finally:
            for name, role in roles.items():
                role.operator.close()
                role.operator.connect()
                if name == "postgres":
                    role.operator._cx.execute('SET search_path TO "' + role.namespace + '"')
                role.runtime.close()
                role.runtime.connect()
        recovered = LocalCutover(
            tmp_path,
            model=model,
            project_id=PROJECT,
            public_key=public,
            operators={name: role.operator for name, role in roles.items()},
            runtime={name: [role.runtime] for name, role in roles.items()},
            chunk_rows=1,
        )
        receipt = recovered.resume().as_record()
        assert receipt["outcome"] == ("success" if where == "after_decision" else "abort")
        assert receipt["recovered"] is True and receipt["within_budget"] is False
        current = sde.Session(
            model,
            recovered.active_map(),
            {name: role.runtime for name, role in roles.items()},
            project_id=PROJECT,
        )
        assert current.get("Event", {"id": 1}) == {"id": 1, "value": 11}


def test_uncertain_decision_sync_requires_reconfirmation_before_activation(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import json

    from sde import _local_state as storage
    from sde.local_cutover import CutoverRecoveryRequired

    with fixture("postgres", tmp_path) as (
        executor,
        plan,
        _old,
        roles,
        _source,
        _target,
        model,
        public,
    ):
        original = storage.sync_directory
        failed = []

        def fail_decision(directory: Path) -> None:
            if directory == tmp_path and not failed:
                current = json.loads(executor.store.path.read_bytes())["payload"]
                running = current["execution"]
                if running is not None and running["decision"] == "success":
                    failed.append(True)
                    raise OSError("controlled decision fsync failure")
            original(directory)

        monkeypatch.setattr(storage, "sync_directory", fail_decision)
        with pytest.raises(CutoverRecoveryRequired):
            executor.execute(plan)
        assert failed == [True]
        assert (
            roles["postgres"]
            .operator.write_fence("source_events", project_id=PROJECT)
            .state()
            .epoch
            == 1
        )
        assert (
            roles["clickhouse"]
            .operator.write_fence("copy_events", project_id=PROJECT)
            .state()
            .epoch
            == 2
        )
        monkeypatch.setattr(storage, "sync_directory", original)
        recovered = LocalCutover(
            tmp_path,
            model=model,
            project_id=PROJECT,
            public_key=public,
            operators={name: role.operator for name, role in roles.items()},
            runtime={name: [role.runtime] for name, role in roles.items()},
            chunk_rows=1,
        )
        assert recovered.resume().as_record()["outcome"] == "success"


def test_different_aliases_cannot_make_the_target_be_the_source(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    with fixture("postgres", tmp_path) as (
        _executor,
        original_plan,
        _old,
        roles,
        _source,
        _target,
        model,
        _public,
    ):
        raw = original_plan.as_record()
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes_raw()
        source = raw["before"]["groups"]["Event"]["source"]
        raw["before"]["groups"]["Event"]["derived"][0]["layout"] = deepcopy(source["layout"])
        raw["success"]["groups"]["Event"]["source"]["layout"] = deepcopy(source["layout"])

        def sign(document: dict[str, Any]) -> None:
            document.pop("signature", None)
            document["signature"] = {
                "alg": "ed25519",
                "value": base64.b64encode(key.sign(sde.canonical_bytes(document))).decode(),
            }

        for name in ("before", "success", "abort"):
            sign(raw[name])
        before = sde.load_map(raw["before"], model=model, public_key=public)
        raw["verification"] = sde.verification_request(
            before,
            group="Event",
            project_id=PROJECT,
            request_id=uuid4().hex,
            requested_at=datetime.now(UTC).isoformat(),
        ).as_record()
        sign(raw)
        plan = sde.load_cutover_plan(raw, model=model, project_id=PROJECT, public_key=public)
        aliased = LocalCutover(
            tmp_path / "aliased",
            model=model,
            project_id=PROJECT,
            public_key=public,
            operators={
                "postgres": roles["postgres"].operator,
                "clickhouse": roles["postgres"].operator,
            },
            runtime={
                "postgres": [roles["postgres"].runtime],
                "clickhouse": [roles["postgres"].runtime],
            },
        )
        aliased.enroll(raw["before"])
        with pytest.raises(sde.MigrationRefused, match="same physical table"):
            aliased.execute(plan)
        assert roles["postgres"].operator.count("source_events") == 2
        assert (
            roles["postgres"]
            .operator.write_fence("source_events", project_id=PROJECT)
            .state()
            .holds
            == ()
        )


ABORT_CHECKPOINTS = ["decision:abort"] + [
    phase + suffix
    for phase in (
        "abort_source_barrier",
        "confirm_target_denied",
        "abort_target_barrier",
        "abort_target_epoch",
        "activate_abort_source",
        "watermarks",
        "publish_map",
        "enable_runtime",
        "release_runtime",
    )
    for suffix in (":intent", ":done")
]


@pytest.mark.parametrize("checkpoint", ABORT_CHECKPOINTS)
def test_interrupted_abort_restores_only_the_complete_source(
    checkpoint: str, tmp_path: Path
) -> None:
    with fixture("postgres", tmp_path) as (executor, plan, old, roles, _, target, model, _):
        actual = roles[target].operator.copy_in

        def omit_one(table: str, rows: Any) -> None:
            actual(table, [row for row in rows if row["id"] != 2])

        roles[target].operator.copy_in = omit_one

        def crash(step: str) -> None:
            if step == checkpoint:
                raise Crash(step)

        executor._after_step = crash
        try:
            with pytest.raises(Crash):
                executor.execute(plan)
        finally:
            roles[target].operator.copy_in = actual
            executor._after_step = lambda _step: None
        assert executor.status()["decision"] == "abort"
        if checkpoint in (
            "publish_map:done",
            "enable_runtime:intent",
            "enable_runtime:done",
            "release_runtime:intent",
            "release_runtime:done",
        ):
            assert executor.status()["active_map_version"] == plan.abort.map_version
        receipt = executor.resume().as_record()
        assert receipt["outcome"] == "abort" and receipt["verification"]["matched"] is False
        active = sde.Session(
            model,
            executor.active_map(),
            {name: role.runtime for name, role in roles.items()},
            project_id=PROJECT,
        )
        assert active.get("Event", {"id": 2}) == {"id": 2, "value": 22}
        active.save("Event", {"id": 4, "value": 44})
        assert active.get("Event", {"id": 4}) == {"id": 4, "value": 44}
        with pytest.raises(sde.EngineError):
            old.save("Event", {"id": 5, "value": 55})
        with pytest.raises(sde.EngineError):
            roles[target].runtime.get("copy_events", {"id": 1})


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_cli_completes_cutover_with_environment_only_connections(
    source: str, tmp_path: Path
) -> None:
    import json
    import os
    import subprocess
    import sys

    from psycopg.conninfo import make_conninfo

    with fixture(source, tmp_path / "state") as (
        executor,
        plan,
        _old,
        roles,
        _source,
        _target,
        model,
        public,
    ):
        config = {
            "protocol": 1,
            "project_id": PROJECT,
            "model": sde.neutral_declaration(model),
            "public_keys": {"primary": base64.b64encode(public).decode()},
            "engines": {},
        }
        environment = dict(os.environ)
        for name, role in roles.items():
            op_name, run_name = "SDE_CLI_" + name.upper(), "SDE_CLI_APP_" + name.upper()
            operator_dsn = role.operator._dsn
            if name == "postgres":
                operator_dsn = make_conninfo(
                    operator_dsn, options="-csearch_path=" + role.namespace
                )
            environment[op_name], environment[run_name] = operator_dsn, role.runtime._dsn
            config["engines"][name] = {
                "dialect": name,
                "operator_dsn_env": op_name,
                "runtime_dsn_envs": [run_name],
            }
        config_path, packet_path = tmp_path / "config.json", tmp_path / "packet.json"
        config_path.write_text(json.dumps(config))
        packet_path.write_text(json.dumps(plan.as_record()))
        base = [
            sys.executable,
            "-m",
            "sde_operator",
            "--project-dir",
            str(executor.store.root),
            "--config",
            str(config_path),
        ]
        run = subprocess.run(
            [*base, "execute", "--plan", str(packet_path)],
            env=environment,
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert run.returncode == 0, run.stderr
        receipt = json.loads(run.stdout)
        assert receipt["outcome"] == "success" and receipt["verification"]["matched"] is True
        for name in list(environment):
            if name.startswith("SDE_CLI_"):
                del environment[name]
        status = subprocess.run(
            [*base, "status"], env=environment, capture_output=True, text=True, timeout=10
        )
        assert status.returncode == 0, status.stderr
        assert json.loads(status.stdout)["active_map_version"] == plan.success.map_version
        stored = executor.store.path.read_text() + run.stdout + run.stderr
        for role in roles.values():
            assert role.runtime._dsn not in stored
        active = sde.Session(
            model,
            executor.active_map(),
            {name: role.runtime for name, role in roles.items()},
            project_id=PROJECT,
        )
        active.save("Event", {"id": 8, "value": 88})
        assert active.get("Event", {"id": 8}) == {"id": 8, "value": 88}


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
@pytest.mark.parametrize("epoch", [2, 3])
def test_recovery_completes_a_partial_native_generation_change(
    source: str, epoch: int, tmp_path: Path, monkeypatch: Any
) -> None:
    from sde.engines._write_fences import ClickHouseFences, PostgresFences

    with fixture(source, tmp_path) as (executor, plan, _old, roles, _, target, model, _):
        kind = ClickHouseFences if target == "clickhouse" else PostgresFences
        original = kind.add_constraint
        reached = []

        def interrupt(backend: Any, table: str, name: str, expression: str) -> None:
            original(backend, table, name, expression)
            if table == "copy_events" and name.endswith("min_" + str(epoch)):
                reached.append(name)
                raise Crash(name)

        with monkeypatch.context() as patch:
            patch.setattr(kind, "add_constraint", interrupt)
            with pytest.raises(Crash):
                executor.execute(plan)
        assert len(reached) == 1
        fence = roles[target].operator.write_fence("copy_events", project_id=PROJECT).state()
        assert fence.lower_epoch == epoch and fence.epoch is None and fence.closed
        receipt = executor.resume().as_record()
        assert receipt["outcome"] == ("abort" if epoch == 2 else "success")
        active = sde.Session(
            model,
            executor.active_map(),
            {name: role.runtime for name, role in roles.items()},
            project_id=PROJECT,
        )
        assert active.get("Event", {"id": 2}) == {"id": 2, "value": 22}
        active.save("Event", {"id": 7, "value": 77})
        assert active.get("Event", {"id": 7}) == {"id": 7, "value": 77}


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_recovery_refuses_a_replaced_target_table(source: str, tmp_path: Path) -> None:
    from sde import CutoverRecoveryRequired

    with fixture(source, tmp_path) as (executor, plan, _old, roles, _, target, _model, _):

        def crash(step: str) -> None:
            if step == "repair:done":
                raise Crash(step)

        executor._after_step = crash
        with pytest.raises(Crash):
            executor.execute(plan)
        executor._after_step = lambda _step: None
        if target == "postgres":
            roles[target].command('ALTER TABLE "copy_events" RENAME TO "displaced_copy"')
            roles[target].command(
                'CREATE TABLE "copy_events" (LIKE "displaced_copy" INCLUDING ALL)'
            )
        else:
            roles[target].command("RENAME TABLE `copy_events` TO `displaced_copy`")
            roles[target].command("CREATE TABLE `copy_events` AS `displaced_copy`")
        with pytest.raises(CutoverRecoveryRequired) as failed:
            executor.resume()
        assert "physical table was replaced" in str(failed.value.__cause__)
        assert roles[target].operator.count("copy_events") == 0
        assert roles[source].operator.count("source_events") == 2


def test_cutover_drains_an_open_application_transaction_before_repair(tmp_path: Path) -> None:
    import threading
    import time

    from sde.engines.clickhouse import ClickHouseEngine
    from sde.engines.postgres import PostgresEngine

    with fixture("postgres", tmp_path) as (executor, plan, _old, roles, _, _, model, _):
        written, release = threading.Event(), threading.Event()
        errors = []
        observed = []
        operator_pid = roles["postgres"].operator._cx.info.backend_pid

        def writer() -> None:
            try:
                with (
                    PostgresEngine(roles["postgres"].runtime._dsn) as pg,
                    ClickHouseEngine(roles["clickhouse"].runtime._dsn) as ch,
                ):
                    active = sde.Session(
                        model, plan.before, {"postgres": pg, "clickhouse": ch}, project_id=PROJECT
                    )
                    with active.transaction("Event"):
                        active.save("Event", {"id": 9, "value": 99})
                        written.set()
                        assert release.wait(15), "the barrier was never observed waiting"
            except BaseException as error:
                errors.append(error)
                written.set()

        def observer() -> None:
            try:
                with PostgresEngine(roles["postgres"].operator._dsn) as monitor:
                    until = time.monotonic() + 12
                    while time.monotonic() < until:
                        pending = monitor._cx.execute(
                            "SELECT count(*) FROM pg_locks WHERE pid=%s AND NOT granted",
                            [operator_pid],
                        ).fetchone()[0]
                        if pending:
                            observed.append("waiting_on_transaction")
                            release.set()
                            return
                        time.sleep(0.02)
                    raise AssertionError("cutover did not wait on the started transaction")
            except BaseException as error:
                errors.append(error)
                release.set()

        worker = threading.Thread(target=writer)
        watcher = threading.Thread(target=observer)

        def hook(step: str) -> None:
            if step == "freeze_source:intent":
                watcher.start()

        executor._after_step = hook
        worker.start()
        try:
            assert written.wait(10) and not errors, errors
            receipt = executor.execute(plan).as_record()
            assert receipt["outcome"] == "success"
        finally:
            release.set()
            worker.join(timeout=15)
            if watcher.ident is not None:
                watcher.join(timeout=15)
        assert not worker.is_alive() and not watcher.is_alive()
        assert not errors, errors
        assert observed == ["waiting_on_transaction"]
        current = sde.Session(
            model,
            executor.active_map(),
            {name: role.runtime for name, role in roles.items()},
            project_id=PROJECT,
        )
        assert current.get("Event", {"id": 9}) == {"id": 9, "value": 99}


def test_enrollment_accepts_integral_json_numbers_like_the_map_loader(tmp_path: Path) -> None:
    with fixture("postgres", tmp_path / "initial") as (
        _executor,
        plan,
        _old,
        roles,
        _,
        _,
        model,
        public,
    ):
        document = plan.as_record()["before"]
        document["contract"] = 4.0
        document["map_version"] = 1.0
        document["groups"]["Event"]["write_epoch"] = 1.0
        expected = sde.load_map(document, model=model, public_key=public, require_signature=True)
        local = LocalCutover(
            tmp_path / "integral",
            model=model,
            project_id=PROJECT,
            public_key=public,
            operators={name: role.operator for name, role in roles.items()},
            runtime={name: [role.runtime] for name, role in roles.items()},
        )
        local.enroll(document)
        assert local.active_map().fingerprint == expected.fingerprint
        local.enroll(document)


def test_enrollment_persists_the_verified_input_snapshot(tmp_path: Path, monkeypatch: Any) -> None:
    from sde import placement

    with fixture("postgres", tmp_path / "initial") as (
        _executor,
        plan,
        _old,
        roles,
        _,
        _,
        model,
        public,
    ):
        document = plan.as_record()["before"]
        original = placement._verify_signature

        def change_caller(raw: Any, keys: Any) -> Any:
            result = original(raw, keys)
            document["map_version"] = 99
            return result

        local = LocalCutover(
            tmp_path / "snapshot",
            model=model,
            project_id=PROJECT,
            public_key=public,
            operators={name: role.operator for name, role in roles.items()},
            runtime={name: [role.runtime] for name, role in roles.items()},
        )
        with monkeypatch.context() as patch:
            patch.setattr(placement, "_verify_signature", change_caller)
            local.enroll(document)
        assert document["map_version"] == 99
        assert local.active_map().fingerprint == plan.before.fingerprint

"""A staging that cannot finish is abandoned: its own copy goes, the map in force stays.

Before its decision a staging holds nothing the application relies on - the prepared map is not
published, no process writes to the copy - so abandoning it removes the tables it created and
keeps everything else as it was. After the decision the next map is decided and only a resume
remains; the copy then leaves through its cutover's abort.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_staging_operator_live import PROJECT, initial

import sde
from sde.local_cutover import CutoverRecoveryRequired

BEFORE_DECISION = [
    "stage_prepared",
    "stage_create_Event:intent",
    "stage_create_Event:done",
    "stage_generation_Event:intent",
    "stage_generation_Event:done",
    "stage_indexes:intent",
    "stage_indexes:done",
    "stage_qualify:intent",
    "stage_qualify:done",
    "stage_grants:intent",
    "stage_grants:done",
]
AFTER_DECISION = [
    "stage_decision",
    "stage_watermarks:intent",
    "stage_watermarks:done",
    "stage_publish:intent",
    "stage_publish:done",
]


class Crash(BaseException):
    pass


def crash_at(checkpoint: str) -> Callable[[str], None]:
    def hook(step: str) -> None:
        if step == checkpoint:
            raise Crash(step)

    return hook


def quiet(_step: str) -> None:
    return None


def copy_table(stage: Any) -> str:
    return str(stage.prepared.groups["Event"].derived[0].layout.tables["Event"])


def exists(role: Any, table: str) -> bool:
    if role.operator.dialect == "postgres":
        row = role.operator._cx.execute("SELECT to_regclass(%s)", (f'"{table}"',)).fetchone()
        return row[0] is not None
    return bool(
        role.operator._cx.query(
            "SELECT count() FROM system.tables WHERE database = {d:String} AND name = {t:String}",
            parameters={"d": role.namespace, "t": table},
        ).result_rows[0][0]
    )


def runtime_grants(role: Any, table: str) -> int:
    """ClickHouse grants of the fixture's runtime login on ``table`` - they survive DROP TABLE."""
    return int(
        role.operator._cx.query(
            "SELECT count() FROM system.grants WHERE user_name = {u:String} "
            "AND database = {d:String} AND table = {t:String}",
            parameters={"u": role.username, "d": role.namespace, "t": table},
        ).result_rows[0][0]
    )


def assert_abandoned(
    operator: Any, stage: Any, old: Any, roles: Any, target: str, receipt: dict[str, Any]
) -> None:
    assert receipt["outcome"] == "abandoned"
    assert receipt["map_version"] == stage.current.map_version
    assert receipt["map_fingerprint"] == stage.current.fingerprint
    assert operator.active_map().fingerprint == stage.current.fingerprint
    table = copy_table(stage)
    assert not exists(roles[target], table)
    if target == "clickhouse":
        assert runtime_grants(roles[target], table) == 0
    status = operator.status()
    assert (status["active_map_version"], status["recorded_map_version"]) == (1, 1)
    assert status["plan_id"] is None
    old.save("Event", {"id": 90, "value": 9})  # the source was never paused
    assert old.get("Event", {"id": 90}) == {"id": 90, "value": 9}
    envelope = json.loads(operator.store.path.read_text())
    assert envelope["storage_contract"] == 4
    assert envelope["payload"]["stages"][stage.stage_id]["receipt"] == receipt


@pytest.mark.parametrize("checkpoint", BEFORE_DECISION)
@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_abandoning_before_the_decision_removes_only_the_copy(
    source: str, checkpoint: str, tmp_path: Path
) -> None:
    with initial(source, tmp_path) as (operator, stage, old, roles, *_, target):
        operator._after_step = crash_at(checkpoint)
        with pytest.raises(Crash):
            operator.stage(stage)
        operator._after_step = quiet
        receipt = operator.abandon().as_record()
        assert receipt["recovered"] is True
        (row,) = receipt["tables"]
        created = checkpoint not in ("stage_prepared", "stage_create_Event:intent")
        assert (row["identity"] is not None) is created
        if created:
            assert row["identity"]["name"] == copy_table(stage)
        assert_abandoned(operator, stage, old, roles, target, receipt)
        # The authorization is spent: the same staging answers with its abandonment.
        assert operator.stage(stage).as_record() == receipt
        with pytest.raises(sde.MigrationRefused, match="no unfinished index build or staging"):
            operator.abandon()


@pytest.mark.parametrize("checkpoint", AFTER_DECISION)
def test_a_prepared_staging_cannot_be_abandoned(checkpoint: str, tmp_path: Path) -> None:
    with initial("postgres", tmp_path) as (operator, stage, _old, roles, *_, target):
        operator._after_step = crash_at(checkpoint)
        with pytest.raises(Crash):
            operator.stage(stage)
        operator._after_step = quiet
        with pytest.raises(sde.MigrationRefused, match="cannot be abandoned"):
            operator.abandon()
        assert operator.store.read()["execution"]["decision"] == "prepared"
        assert operator.resume().as_record()["outcome"] == "prepared"
        assert exists(roles[target], copy_table(stage))


@pytest.mark.parametrize(
    ("checkpoint", "finish"),
    [
        ("stage_abandoned", "resume"),
        ("stage_abandoned", "abandon"),
        ("stage_drop_Event:intent", "resume"),
        ("stage_drop_Event:done", "abandon"),
        ("stage_revoke:intent", "resume"),
    ],
)
def test_an_interrupted_abandonment_completes_on_resume_or_retry(
    checkpoint: str, finish: str, tmp_path: Path
) -> None:
    with initial("postgres", tmp_path) as (operator, stage, old, roles, *_, target):
        assert target == "clickhouse"  # the revoke step exists only there
        operator._after_step = crash_at("stage_grants:done")
        with pytest.raises(Crash):
            operator.stage(stage)
        assert runtime_grants(roles[target], copy_table(stage)) == 2  # SELECT and INSERT
        operator._after_step = crash_at(checkpoint)
        with pytest.raises(Crash):
            operator.abandon()
        operator._after_step = quiet
        # Once decided, an abandonment stays one: a recovery removes, it never builds again.
        assert operator.store.read()["execution"]["decision"] == "abandoned"
        finished = operator.resume() if finish == "resume" else operator.abandon()
        receipt = finished.as_record()
        assert receipt["tables"][0]["identity"]["name"] == copy_table(stage)
        assert_abandoned(operator, stage, old, roles, target, receipt)


def test_a_staging_blocked_by_another_barrier_is_abandoned(tmp_path: Path) -> None:
    """The reason abandonment exists: a staging whose every resume is refused."""
    with initial("postgres", tmp_path) as (operator, stage, old, roles, *_, target):
        operator._after_step = crash_at("stage_create_Event:done")
        with pytest.raises(Crash):
            operator.stage(stage)
        operator._after_step = quiet
        fence = roles["postgres"].operator.write_fence("initial_events", project_id=PROJECT)
        fence.freeze("e" * 32)
        try:
            for _ in range(2):
                with pytest.raises(CutoverRecoveryRequired) as refused:
                    operator.resume()
                assert isinstance(refused.value.__cause__, sde.MigrationRefused)
            receipt = operator.abandon().as_record()
            assert receipt["outcome"] == "abandoned"
            assert "e" * 32 in fence.state().holds  # the other operation's barrier stands
            assert not exists(roles[target], copy_table(stage))
            assert operator.active_map().fingerprint == stage.current.fingerprint
        finally:
            fence.release("e" * 32)
        old.save("Event", {"id": 91, "value": 1})


def test_a_foreign_table_under_the_copy_name_is_left_alone(tmp_path: Path) -> None:
    with initial("postgres", tmp_path) as (operator, stage, _old, roles, *_, target):
        operator._after_step = crash_at("stage_prepared")
        with pytest.raises(Crash):
            operator.stage(stage)
        operator._after_step = quiet
        table = copy_table(stage)
        roles[target].command(f"CREATE TABLE `{table}` (id Int64) ENGINE = MergeTree ORDER BY id")
        receipt = operator.abandon().as_record()
        assert receipt["outcome"] == "abandoned"
        assert receipt["tables"][0]["identity"] is None  # nothing of this staging's was there
        assert exists(roles[target], table)  # somebody else's table, untouched
        assert operator.active_map().fingerprint == stage.current.fingerprint


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_a_killed_staging_leaves_a_table_the_abandonment_finds_by_its_marker(
    source: str, tmp_path: Path
) -> None:
    """Killed after CREATE, before the identity was recorded: only the marker says whose it is."""
    import os
    import select
    import signal
    import subprocess
    import sys

    from psycopg.conninfo import make_conninfo

    with initial(source, tmp_path) as (operator, stage, old, roles, model, public, *_, target):
        payload = {
            "directory": str(tmp_path),
            "project_id": PROJECT,
            "model": sde.neutral_declaration(model),
            "public_key": public.hex(),
            "plan": stage.as_record(),
            "checkpoint": "native:created",
            "operators": {
                name: (
                    make_conninfo(role.operator._dsn, options="-csearch_path=" + role.namespace)
                    if name == "postgres"
                    else role.operator._dsn
                )
                for name, role in roles.items()
            },
            "runtime": {name: role.runtime._dsn for name, role in roles.items()},
        }
        worker = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("_staging_worker.py"))],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert worker.stdin is not None and worker.stdout is not None
            worker.stdin.write(json.dumps(payload))
            worker.stdin.close()
            assert select.select([worker.stdout], [], [], 25)[0], "worker missed its checkpoint"
            line = worker.stdout.readline()
            if line != "READY\n":
                assert worker.stderr is not None
                raise AssertionError(worker.stderr.read())
            os.kill(worker.pid, signal.SIGKILL)
            worker.wait(timeout=10)
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=10)
        (row,) = operator.store.read()["execution"]["tables"]
        assert row["identity"] is None and exists(roles[target], copy_table(stage))
        receipt = operator.abandon().as_record()
        assert receipt["tables"][0]["identity"]["name"] == copy_table(stage)
        assert_abandoned(operator, stage, old, roles, target, receipt)


def test_the_operator_cli_abandons_a_staging(tmp_path: Path) -> None:
    import base64
    import os
    import subprocess
    import sys

    from psycopg.conninfo import make_conninfo

    root = tmp_path / "state"
    with initial("postgres", root) as (operator, stage, old, roles, model, public, *_):
        operator._after_step = crash_at("stage_qualify:done")
        with pytest.raises(Crash):
            operator.stage(stage)
        config: dict[str, Any] = {
            "protocol": 1,
            "project_id": PROJECT,
            "model": sde.neutral_declaration(model),
            "public_keys": {"primary": base64.b64encode(public).decode()},
            "engines": {},
        }
        environment = dict(os.environ)
        for name, role in roles.items():
            op_name, run_name = "SDE_ABANDON_" + name.upper(), "SDE_ABANDON_APP_" + name.upper()
            dsn = role.operator._dsn
            if name == "postgres":
                dsn = make_conninfo(dsn, options="-csearch_path=" + role.namespace)
            environment[op_name], environment[run_name] = dsn, role.runtime._dsn
            config["engines"][name] = {
                "dialect": name,
                "operator_dsn_env": op_name,
                "runtime_dsn_envs": [run_name],
            }
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config))
        command = [
            sys.executable,
            "-m",
            "sde_operator",
            "--project-dir",
            str(operator.store.root),
            "--config",
            str(config_path),
            "abandon",
        ]
        done = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=90)
        assert done.returncode == 0, done.stderr
        receipt = json.loads(done.stdout)
        assert receipt["outcome"] == "abandoned"
        again = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=90)
        assert again.returncode == 2
        assert json.loads(again.stderr) == {
            "error": "refused",
            "message": "there is no unfinished index build or staging to abandon",
        }
        old.save("Event", {"id": 92, "value": 2})

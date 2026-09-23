"""An existing source stays authoritative through durable preparation and the later cutover."""

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

PROJECT = "1" * 32


@contextmanager
def initial(
    source: str,
    root: Path,
    *,
    target: str | None = None,
    indexes: list[dict[str, Any]] | None = None,
) -> Iterator[tuple[Any, ...]]:
    """A source-only map, an old session on it, an operator, and a staging packet for one copy.

    ``target`` defaults to the other engine - a move, staging protocol 1. Naming the source's own
    engine prepares a relayout under staging protocol 2, and ``indexes`` gives the copy a physical
    design the source does not have, which is what a relayout is for.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    with runtime_roles("postgres") as pg, runtime_roles("clickhouse") as ch:
        roles = {"postgres": pg, "clickhouse": ch}
        if target is None:
            target = "clickhouse" if source == "postgres" else "postgres"
        protocol = 2 if target == source else 1
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

        def material(engine: str, table: str, identity: str) -> dict[str, Any]:
            return {
                "id": identity,
                "engine": engine,
                "layout": {
                    "tables": {"Event": table},
                    "columns": {
                        "Event": {
                            "id": "bigint" if engine == "postgres" else "Int64",
                            "value": "integer" if engine == "postgres" else "Int32",
                        }
                    },
                },
            }

        current = signed(
            {
                "contract": 4,
                "project_id": PROJECT,
                "model_version": model.version,
                "map_version": 1,
                "groups": {
                    "Event": {
                        "write_epoch": 1,
                        "source": material(source, "initial_events", "source"),
                    }
                },
            }
        )
        parsed = sde.load_map(current, model=model, public_key=public)
        operators = {name: role.operator for name, role in roles.items()}
        runtime = {name: role.runtime for name, role in roles.items()}
        sde.prepare_schema(model, parsed, operators, project_id=PROJECT)
        roles[source].grant("initial_events")
        for role in roles.values():
            role.grant("sde_map_state")
        old = sde.Session(model, parsed, runtime, project_id=PROJECT)
        old.save("Event", {"id": 1, "value": 11})
        operator = sde.LocalCutover(
            root,
            model=model,
            project_id=PROJECT,
            public_key=public,
            operators=operators,
            runtime={name: [engine] for name, engine in runtime.items()},
        )
        operator.enroll(current)
        stage_id = uuid4().hex
        prepared = deepcopy(current)
        prepared["map_version"] = 2
        copy = {
            **material(target, sde.staging_table_name(stage_id, 1), "copy"),
            "lag_budget_ms": 30000,
        }
        if indexes:
            copy["layout"]["indexes"] = indexes
            prepared["contract"] = 5  # a physical design first appears in the fresh copy
        prepared["groups"]["Event"].update(derived=[copy], also_write=["copy"])
        prepared = signed(prepared)
        stage = sde.load_staging_plan(
            signed(
                {
                    "kind": "sde-stage",
                    "protocol": protocol,
                    "stage_id": stage_id,
                    "project_id": PROJECT,
                    "group": "Event",
                    "current": current,
                    "prepared": prepared,
                }
            ),
            model=model,
            project_id=PROJECT,
            public_key=public,
        )
        yield operator, stage, old, roles, model, public, signed, material, source, target


def cutover(stage: Any, signed: Any, model: Any, public: bytes) -> Any:
    """The cutover that follows ``stage``: protocol 2 when the staging was a relayout."""
    protocol = stage.as_record()["protocol"]
    before = stage.as_record()["prepared"]
    source = before["groups"]["Event"]["source"]
    target = deepcopy(before["groups"]["Event"]["derived"][0])
    target.pop("lag_budget_ms")
    success, abort = deepcopy(before), deepcopy(before)
    epoch = before["groups"]["Event"]["write_epoch"]
    success["map_version"], abort["map_version"] = (
        before["map_version"] + 1,
        before["map_version"] + 2,
    )
    success["groups"]["Event"] = {"source": target, "write_epoch": epoch + 2}
    abort["groups"]["Event"] = {"source": source, "write_epoch": epoch + 1}
    request = sde.verification_request(
        stage.prepared,
        project_id=PROJECT,
        group="Event",
        request_id=uuid4().hex,
        requested_at=datetime.now(UTC).isoformat(),
    )
    return sde.load_cutover_plan(
        signed(
            {
                "kind": "sde-cutover",
                "protocol": protocol,
                "plan_id": uuid4().hex,
                "project_id": PROJECT,
                "group": "Event",
                "before": before,
                "success": signed(success),
                "abort": signed(abort),
                "verification": request.as_record(),
                "pause_budget_ms": 30000,
                "query_impact_digest": "a" * 64,
            }
        ),
        model=model,
        project_id=PROJECT,
        public_key=public,
    )


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_stage_retains_old_source_and_then_cutover_repairs_its_missing_fanout(
    source: str, tmp_path: Path
) -> None:
    with initial(source, tmp_path) as (
        operator,
        stage,
        old,
        roles,
        model,
        public,
        signed,
        _,
        _,
        target,
    ):
        result = operator.stage(stage).as_record()
        assert result["outcome"] == "prepared"
        assert result["map_fingerprint"] == stage.prepared.fingerprint
        old.save("Event", {"id": 2, "value": 22})
        assert old.get("Event", {"id": 2}) == {"id": 2, "value": 22}
        target_table = stage.prepared.groups["Event"].derived[0].layout.tables["Event"]
        assert roles[target].operator.count(target_table) == 0
        active = sde.Session(
            model,
            operator.active_map(),
            {name: role.runtime for name, role in roles.items()},
            project_id=PROJECT,
        )
        active.save("Event", {"id": 3, "value": 33})
        assert roles[target].operator.get(target_table, {"id": 3}) is not None
        assert operator.stage(stage).as_record() == result
        final = operator.execute(cutover(stage, signed, model, public)).as_record()
        assert final["outcome"] == "success"
        moved = sde.Session(
            model,
            operator.active_map(),
            {name: role.runtime for name, role in roles.items()},
            project_id=PROJECT,
        )
        for identity in (1, 2, 3):
            assert moved.get("Event", {"id": identity}) == {"id": identity, "value": identity * 11}
        with pytest.raises(sde.EngineError):
            old.get("Event", {"id": 1})


def _indexes(roles: Any, table: str) -> list[tuple[str, ...]]:
    """What the engine's own catalogue says the table's indexes are - not what the map says."""
    if roles.operator.dialect == "postgres":
        return [
            (str(name),)
            for (name,) in roles.operator._cx.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname=%s AND tablename=%s "
                "AND indexname NOT LIKE '%%_pkey' ORDER BY indexname",
                (roles.namespace, table),
            ).fetchall()
        ]
    return [
        tuple(str(value) for value in row)
        for row in roles.operator._cx.query(
            "SELECT name, type, expr FROM system.data_skipping_indices "
            "WHERE database={d:String} AND table={t:String} ORDER BY name",
            parameters={"d": roles.namespace, "t": table},
        ).result_rows
    ]


@pytest.mark.parametrize("engine", ["postgres", "clickhouse"])
def test_a_relayout_in_the_same_engine_stages_and_cuts_over_like_a_move(
    engine: str, tmp_path: Path
) -> None:
    """A new physical design in the engine the group is already in, by the same two steps.

    Staging protocol 2 prepares a copy in the source's own engine under fresh names and with the
    design; cutover protocol 2 repairs what the old session wrote only to the source, activates the
    copy and revokes the old tables. The design is read back from the engine's catalogue, and the
    old session is refused - the same guarantees a move gives, in one engine.
    """
    index: dict[str, Any] = {"entity": "Event", "name": "sde_i_relayout_000001"}
    index.update(
        {"columns": ["value"]}
        if engine == "postgres"
        else {"columns": ["value"], "method": "minmax", "granularity": 4}
    )
    with initial(engine, tmp_path, target=engine, indexes=[index]) as (
        operator,
        stage,
        old,
        roles,
        model,
        public,
        signed,
        _,
        _,
        target,
    ):
        assert target == engine
        assert stage.as_record()["protocol"] == 2
        result = operator.stage(stage).as_record()
        assert result["outcome"] == "prepared"
        copy_table = stage.prepared.groups["Event"].derived[0].layout.tables["Event"]
        assert copy_table != "initial_events"
        expected = [("sde_i_relayout_000001",)] if engine == "postgres" else [
            ("sde_i_relayout_000001", "minmax", "value")
        ]
        assert _indexes(roles[engine], copy_table) == expected
        assert _indexes(roles[engine], "initial_events") == []
        old.save("Event", {"id": 2, "value": 22})  # the source only: the copy misses it for now
        runtime = {name: role.runtime for name, role in roles.items()}
        active = sde.Session(model, operator.active_map(), runtime, project_id=PROJECT)
        active.save("Event", {"id": 3, "value": 33})
        assert roles[engine].operator.get(copy_table, {"id": 3}) is not None
        final = operator.execute(cutover(stage, signed, model, public)).as_record()
        assert final["outcome"] == "success"
        moved = sde.Session(model, operator.active_map(), runtime, project_id=PROJECT)
        for identity in (1, 2, 3):
            assert moved.get("Event", {"id": identity}) == {"id": identity, "value": identity * 11}
        assert operator.active_map().groups["Event"].source.layout.tables["Event"] == copy_table
        with pytest.raises(sde.EngineError):
            old.get("Event", {"id": 1})


class Crash(BaseException):
    pass


CHECKPOINTS = ["stage_prepared", "stage_decision"] + [
    phase + suffix
    for phase in (
        "stage_create_Event",
        "stage_generation_Event",
        "stage_indexes",
        "stage_qualify",
        "stage_grants",
        "stage_watermarks",
        "stage_publish",
    )
    for suffix in (":intent", ":done")
]


@pytest.mark.parametrize("checkpoint", CHECKPOINTS)
def test_staging_resumes_the_same_preparation_after_every_checkpoint(
    checkpoint: str, tmp_path: Path
) -> None:
    with initial("postgres", tmp_path) as (operator, stage, old, roles, model, _, _, _, _, _):

        def crash(step: str) -> None:
            if step == checkpoint:
                raise Crash(step)

        operator._after_step = crash
        with pytest.raises(Crash):
            operator.stage(stage)
        old.save("Event", {"id": 2, "value": 22})
        operator._after_step = lambda _step: None
        receipt = operator.resume().as_record()
        assert receipt["outcome"] == "prepared"
        assert receipt["recovered"] is True
        assert operator.active_map().fingerprint == stage.prepared.fingerprint
        active = sde.Session(
            model,
            operator.active_map(),
            {name: role.runtime for name, role in roles.items()},
            project_id=PROJECT,
        )
        active.save("Event", {"id": 3, "value": 33})
        assert active.get("Event", {"id": 3}) == {"id": 3, "value": 33}


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
@pytest.mark.parametrize("first_outcome", ["success", "abort"])
def test_a_second_staging_and_cutover_preserve_history_and_never_reuse_a_table(
    source: str, first_outcome: str, tmp_path: Path
) -> None:
    with initial(source, tmp_path) as (
        operator,
        stage,
        old,
        roles,
        model,
        public,
        signed,
        material,
        _source,
        target,
    ):
        first_stage_receipt = operator.stage(stage).as_record()
        first_cutover = cutover(stage, signed, model, public)
        original = roles[target].operator.copy_in
        if first_outcome == "abort":
            roles[target].operator.copy_in = lambda _table, _rows: None
        try:
            first = operator.execute(first_cutover).as_record()
        finally:
            roles[target].operator.copy_in = original
        assert first["outcome"] == first_outcome
        active_before = operator.store.read()["active_map"]
        active_source = active_before["groups"]["Event"]["source"]["engine"]
        next_target = "clickhouse" if active_source == "postgres" else "postgres"
        epoch = active_before["groups"]["Event"]["write_epoch"]
        identity = uuid4().hex
        prepared = deepcopy(active_before)
        prepared["map_version"] = first_cutover.abort.map_version + 1
        fresh = {
            **material(next_target, sde.staging_table_name(identity, 1), "next-copy"),
            "lag_budget_ms": 30000,
        }
        prepared["groups"]["Event"].update(derived=[fresh], also_write=["next-copy"])
        next_stage = sde.load_staging_plan(
            signed(
                {
                    "kind": "sde-stage",
                    "protocol": 1,
                    "stage_id": identity,
                    "project_id": PROJECT,
                    "group": "Event",
                    "current": active_before,
                    "prepared": signed(prepared),
                }
            ),
            model=model,
            project_id=PROJECT,
            public_key=public,
        )
        operator.stage(next_stage)
        assert operator.active_map().groups["Event"].write_epoch == epoch
        assert operator.stage(stage).as_record() == first_stage_receipt
        final = operator.execute(cutover(next_stage, signed, model, public)).as_record()
        assert final["outcome"] == "success"
        active = sde.Session(
            model,
            operator.active_map(),
            {name: role.runtime for name, role in roles.items()},
            project_id=PROJECT,
        )
        assert active.get("Event", {"id": 1}) == {"id": 1, "value": 11}
        active.save("Event", {"id": 4, "value": 44})
        assert active.get("Event", {"id": 4}) == {"id": 4, "value": 44}
        with pytest.raises(sde.EngineError):
            old.save("Event", {"id": 5, "value": 55})
        history = operator.store.read()
        assert len(history["stages"]) == 2
        assert len(history["completed"]) == 2
        names = {
            table["identity"]["name"]
            for value in history["stages"].values()
            for table in value["receipt"]["tables"]
        }
        assert len(names) == 2


def test_stage_will_not_adopt_an_unrelated_preexisting_target(tmp_path: Path) -> None:
    with initial("postgres", tmp_path) as (
        operator,
        stage,
        _old,
        roles,
        _model,
        _,
        _,
        _,
        _,
        target,
    ):
        layout = stage.prepared.groups["Event"].derived[0].layout
        roles[target].operator.ensure_schema(layout, keys={"Event": ["id"]})
        roles[target].operator.insert(layout.tables["Event"], {"id": 99, "value": 99})
        with pytest.raises(sde.MigrationRefused, match="already exists"):
            operator.stage(stage)
        assert operator.store.read()["execution"] is None
        assert roles[target].operator.get(layout.tables["Event"], {"id": 99}) == {
            "id": 99,
            "value": 99,
        }
        assert operator.active_map().fingerprint == stage.current.fingerprint


@pytest.mark.parametrize(
    ("source", "checkpoint"),
    [
        ("postgres", "native:created"),
        ("clickhouse", "native:pg-before-comment"),
        ("postgres", "stage_publish:done"),
    ],
)
def test_a_killed_staging_process_recovers_owned_creation_and_publication(
    source: str, checkpoint: str, tmp_path: Path
) -> None:
    import json
    import os
    import select
    import signal
    import subprocess
    import sys

    from psycopg.conninfo import make_conninfo

    with initial(source, tmp_path) as (operator, stage, old, roles, model, public, _, _, _, target):
        payload = {
            "directory": str(tmp_path),
            "project_id": PROJECT,
            "model": sde.neutral_declaration(model),
            "public_key": public.hex(),
            "plan": stage.as_record(),
            "checkpoint": checkpoint,
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
            assert select.select([worker.stdout], [], [], 25)[0], (
                "worker missed the staging checkpoint"
            )
            line = worker.stdout.readline()
            if line != "READY\n":
                assert worker.stderr is not None
                raise AssertionError(worker.stderr.read())
            old.save("Event", {"id": 2, "value": 22})
            os.kill(worker.pid, signal.SIGKILL)
            worker.wait(timeout=10)
            target_table = stage.prepared.groups["Event"].derived[0].layout.tables["Event"]
            if checkpoint == "native:pg-before-comment":
                found = (
                    roles[target]
                    .operator._cx.execute("SELECT to_regclass(%s)", [target_table])
                    .fetchone()[0]
                )
                assert found is None
            result = operator.resume().as_record()
            assert result["outcome"] == "prepared"
            assert result["recovered"] is True
            active = sde.Session(
                model,
                operator.active_map(),
                {name: role.runtime for name, role in roles.items()},
                project_id=PROJECT,
            )
            assert active.get("Event", {"id": 2}) == {"id": 2, "value": 22}
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=10)


def test_completed_stage_retry_reconfirms_an_uncertain_directory_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sde import _local_state

    with initial("postgres", tmp_path) as (operator, stage, *_):
        original = _local_state.sync_directory
        failed = False

        def fail_final(path: Path) -> None:
            nonlocal failed
            state = operator.store.read()
            if stage.stage_id in state.get("stages", {}) and not failed:
                failed = True
                raise OSError("controlled final directory sync failure")
            original(path)

        monkeypatch.setattr(_local_state, "sync_directory", fail_final)
        with pytest.raises(sde.CutoverRecoveryRequired):
            operator.stage(stage)
        assert failed
        assert operator.store.read()["execution"] is None
        confirmed: list[Path] = []

        def confirm(path: Path) -> None:
            confirmed.append(path)
            original(path)

        monkeypatch.setattr(_local_state, "sync_directory", confirm)
        receipt = operator.stage(stage).as_record()
        assert receipt["outcome"] == "prepared"
        assert operator.store.root in confirmed
        assert operator.active_map().fingerprint == stage.prepared.fingerprint


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
@pytest.mark.parametrize("changed", ["source", "target", "watermark", "target_hold"])
def test_staging_recovery_refuses_changed_native_objects_before_publication(
    source: str, changed: str, tmp_path: Path
) -> None:
    from sde.engines._staging import NativeStaging

    with initial(source, tmp_path) as (operator, stage, _old, roles, _, _, _, _, _, target):

        def crash(step: str) -> None:
            if step == "stage_qualify:done":
                raise Crash(step)

        operator._after_step = crash
        with pytest.raises(Crash):
            operator.stage(stage)
        group = stage.prepared.groups["Event"]
        layout = group.derived[0].layout if changed == "target" else group.source.layout
        binding = target if changed in ("target", "watermark", "target_hold") else source
        table = layout.tables["Event"]
        native = operator.native[binding]
        if changed == "target_hold":
            table = group.derived[0].layout.tables["Event"]
            roles[target].operator.write_fence(table, project_id=PROJECT).freeze("e" * 32)
        elif changed == "watermark":
            native.command("DROP TABLE " + native.quote("sde_map_state"))
            roles[target].operator.map_watermark()
            roles[target].grant("sde_map_state")
        else:
            marker = NativeStaging(native).marker(table)[1]
            native.command("DROP TABLE " + native.quote(table))
            if changed == "target":
                NativeStaging(native).create_table(
                    entity="Event", layout=layout, keys={"Event": ["id"]}, marker=marker
                )
            else:
                roles[source].operator.ensure_schema(layout, keys={"Event": ["id"]})
                roles[source].operator.write_fence(table, project_id=PROJECT).prepare(1)
                roles[source].grant(table)
        operator._after_step = lambda _step: None
        with pytest.raises(sde.CutoverRecoveryRequired) as refused:
            operator.resume()
        assert isinstance(refused.value.__cause__, sde.MigrationRefused)
        assert operator.active_map().fingerprint == stage.current.fingerprint
        assert operator.store.read()["execution"] is not None


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_staging_cli_publishes_a_usable_map_and_repeats_the_same_receipt(
    source: str, tmp_path: Path
) -> None:
    import json
    import os
    import subprocess
    import sys

    from psycopg.conninfo import make_conninfo

    with initial(source, tmp_path / "state") as (operator, stage, _, roles, model, public, *_):
        config = {
            "protocol": 1,
            "project_id": PROJECT,
            "model": sde.neutral_declaration(model),
            "public_keys": {"primary": base64.b64encode(public).decode()},
            "engines": {},
        }
        environment = dict(os.environ)
        for name, role in roles.items():
            op_name, run_name = "SDE_STAGE_" + name.upper(), "SDE_STAGE_APP_" + name.upper()
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
        packet_path.write_text(json.dumps(stage.as_record()))
        base = [
            sys.executable,
            "-m",
            "sde_operator",
            "--project-dir",
            str(operator.store.root),
            "--config",
            str(config_path),
        ]
        receipts = []
        for _ in range(2):
            run = subprocess.run(
                [*base, "stage", "--plan", str(packet_path)],
                env=environment,
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert run.returncode == 0, run.stderr
            receipts.append(json.loads(run.stdout))
        assert receipts[0] == receipts[1]
        assert receipts[0]["outcome"] == "prepared"
        active = sde.Session(
            model,
            operator.active_map(),
            {name: role.runtime for name, role in roles.items()},
            project_id=PROJECT,
        )
        active.save("Event", {"id": 8, "value": 88})
        assert active.get("Event", {"id": 8}) == {"id": 8, "value": 88}
        stored = operator.store.path.read_text() + json.dumps(receipts)
        for role in roles.values():
            assert role.runtime._dsn not in stored

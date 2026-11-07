"""Erasure by the local operator, against PostgreSQL and ClickHouse (``sde.erasure``).

Every test starts from the staging fixture: a group in force on one engine, an operator enrolled on
its map, and a staging packet that adds a copy in the other engine - the moment a person's rows are
in the most places at once. The control in each is a row of another key, which has to survive.
Without both engines the fixture skips, and CI's step that fails on a skipped live test catches it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import stat
from pathlib import Path
from typing import Any

import pytest
from test_staging_operator_live import PROJECT, cutover, initial

import sde
from sde.erasure import ErasureReceipt, ErasureRequest
from sde.errors import MigrationRefused
from sde.local_cutover import CutoverRecoveryRequired

ERASED = (734512981, 734512982)
"""Keys no count, version or size in a receipt is likely to spell."""


def _runtime(roles: dict[str, Any]) -> dict[str, Any]:
    return {name: role.runtime for name, role in roles.items()}


def _tables(stage: Any) -> tuple[str, str]:
    copy = stage.prepared.groups["Event"].derived[0].layout.tables["Event"]
    return "initial_events", copy


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_an_erasure_reaches_the_source_and_the_copy_a_migration_fills(
    source: str, tmp_path: Path
) -> None:
    with initial(source, tmp_path) as (
        operator, stage, _old, roles, model, public, signed, _, _, target,
    ):
        operator.stage(stage)
        active = sde.Session(model, operator.active_map(), _runtime(roles), project_id=PROJECT)
        for identity in (2, *ERASED):
            active.save("Event", {"id": identity, "value": 7})
        source_table, copy_table = _tables(stage)
        request = ErasureRequest.build(
            model, "Event", [{"id": ERASED[0]}, {"id": ERASED[1]}, {"id": 99}]
        )

        plan = operator.erase(request)
        assert {(t.role, t.engine, t.table, t.matching) for t in plan.tables} == {
            ("source", source, source_table, 2),
            ("copy", target, copy_table, 2),
        }
        # Planning changed nothing.
        assert roles[source].operator.get(source_table, {"id": ERASED[0]}) is not None

        receipt = operator.erase(request, execute=True)
        assert isinstance(receipt, ErasureReceipt)
        record = receipt.as_record()
        assert record["outcome"] == "erased"
        assert {(t["role"], t["deleted"], t["remaining"]) for t in record["tables"]} == {
            ("source", 2, 0),
            ("copy", 2, 0),
        }
        for identity in ERASED:
            assert roles[source].operator.get(source_table, {"id": identity}) is None
            assert roles[target].operator.get(copy_table, {"id": identity}) is None
        # The control: another key, in both tables, untouched.
        assert active.get("Event", {"id": 2}) == {"id": 2, "value": 7}
        assert roles[target].operator.get(copy_table, {"id": 2}) is not None

        # The cutover's frozen comparison after it: nothing comes back from either side.
        final = operator.execute(cutover(stage, signed, model, public)).as_record()
        assert final["outcome"] == "success"
        moved = sde.Session(model, operator.active_map(), _runtime(roles), project_id=PROJECT)
        assert moved.get("Event", {"id": ERASED[0]}) is None
        assert moved.get("Event", {"id": 2}) == {"id": 2, "value": 7}


def test_the_receipt_names_no_key_and_its_digests_are_the_documented_hmac(
    tmp_path: Path,
) -> None:
    with initial("postgres", tmp_path) as (operator, _stage, old, _roles, model, *_):
        for identity in ERASED:
            old.save("Event", {"id": identity, "value": 5})
        request = ErasureRequest.build(model, "Event", [{"id": ERASED[1]}, {"id": ERASED[0]}])
        record = operator.erase(request, execute=True).as_record()
        assert record["outcome"] == "erased"
        assert record["keys"] == 2

        root = operator.store.root
        stored = root / "erasures" / f"{record['id']}.json"
        assert json.loads(stored.read_text()) == record
        for path in (root / "erasure.key", stored):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        text = stored.read_text() + json.dumps(record)
        for identity in ERASED:
            assert str(identity) not in text

        secret = (root / "erasure.key").read_bytes()
        assert len(secret) == 32
        expected = {
            hmac.new(
                secret,
                sde.canonical_bytes({"entity": "Event", "key": {"id": identity}}),
                hashlib.sha256,
            ).hexdigest()
            for identity in ERASED
        }
        assert set(record["key_digests"]) == expected
        # The same request again finds nothing left, and keeps the same digests: one secret.
        again = operator.erase(request, execute=True).as_record()
        assert set(again["key_digests"]) == expected
        assert {t["matching"] for t in again["tables"]} == {0}


def test_an_erasure_beside_an_unfinished_operation_is_refused(tmp_path: Path) -> None:
    with initial("postgres", tmp_path) as (
        operator, stage, _old, _roles, model, public, signed, *_,
    ):
        operator.stage(stage)

        def stop(step: str) -> None:
            if step == "repair:intent":
                raise RuntimeError("stopped for the test")

        operator._after_step = stop
        with pytest.raises(CutoverRecoveryRequired):
            operator.execute(cutover(stage, signed, model, public))
        operator._after_step = lambda _step: None
        request = ErasureRequest.build(model, "Event", [{"id": 1}])
        with pytest.raises(MigrationRefused, match="unfinished operation"):
            operator.erase(request, execute=True)
        # The control: resumed, the cutover is aborted, and the same erasure runs.
        assert operator.resume().as_record()["outcome"] == "abort"
        assert operator.erase(request, execute=True).outcome == "erased"


def test_a_row_written_again_during_the_erasure_is_reported_not_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sde import erasure

    with initial("postgres", tmp_path) as (operator, _stage, old, _roles, model, *_):
        old.save("Event", {"id": ERASED[0], "value": 5})
        delete = erasure._PostgresAccess.delete

        def delete_then_the_application_writes_again(self: Any, *args: Any) -> int:
            gone = delete(self, *args)
            old.save("Event", {"id": ERASED[0], "value": 6})
            return gone

        monkeypatch.setattr(
            erasure._PostgresAccess, "delete", delete_then_the_application_writes_again
        )
        request = ErasureRequest.build(model, "Event", [{"id": ERASED[0]}])
        record = operator.erase(request, execute=True).as_record()
        assert record["outcome"] == "incomplete"
        assert [(t["deleted"], t["remaining"]) for t in record["tables"]] == [(1, 1)]


def test_a_table_a_move_retired_is_listed_with_its_rows_and_left_alone(tmp_path: Path) -> None:
    with initial("postgres", tmp_path) as (
        operator, stage, old, roles, model, public, signed, *_,
    ):
        old.save("Event", {"id": ERASED[0], "value": 5})
        operator.stage(stage)
        assert operator.execute(cutover(stage, signed, model, public)).as_record()[
            "outcome"
        ] == "success"
        request = ErasureRequest.build(model, "Event", [{"id": ERASED[0]}])
        plan = operator.erase(request)
        assert [(t.role, t.engine, t.matching) for t in plan.tables] == [
            ("source", "clickhouse", 1)
        ]
        assert [(t.engine, t.table, t.matching) for t in plan.outside] == [
            ("postgres", "initial_events", 1)
        ]
        assert plan.outside[0].statements[0].startswith("DELETE FROM ")

        record = operator.erase(request, execute=True).as_record()
        assert record["outcome"] == "erased"
        assert record["outside"] == [
            {
                "engine": "postgres",
                "dialect": "postgres",
                "table": "initial_events",
                "role": "outside",
                "matching": 1,
                "remaining": 1,
            }
        ]
        assert roles["postgres"].operator.get("initial_events", {"id": ERASED[0]}) is not None


def test_clickhouse_loses_every_version_of_the_key_not_only_the_one_final_reads(
    tmp_path: Path,
) -> None:
    with initial("clickhouse", tmp_path) as (operator, _stage, old, roles, model, *_):
        engine = roles["clickhouse"].operator
        # A background merge collapses the versions of a ReplacingMergeTree, so the three are
        # counted with merges stopped. A mutation waits on the merge pool, so they start again
        # before the erasure (docs/qualification/in-place-index, probe_merge_pool).
        engine._cx.command("SYSTEM STOP MERGES initial_events")
        try:
            for value in (5, 6, 7):  # three inserts, three parts, three versions of one key
                old.save("Event", {"id": ERASED[0], "value": value})
            old.save("Event", {"id": 2, "value": 2})
            versions = "SELECT count() FROM initial_events WHERE id = %(id)s"
            found = engine._cx.query(versions, parameters={"id": ERASED[0]})
            assert found.result_rows[0][0] == 3
        finally:
            engine._cx.command("SYSTEM START MERGES initial_events")

        request = ErasureRequest.build(model, "Event", [{"id": ERASED[0]}])
        record = operator.erase(request, execute=True).as_record()
        assert record["outcome"] == "erased"
        assert engine._cx.query(versions, parameters={"id": ERASED[0]}).result_rows[0][0] == 0
        # Gone from the parts, not masked: a lightweight DELETE leaves a row a read with the
        # deleted mask off still returns, until a merge.
        unmasked = engine._cx.query(
            versions, parameters={"id": ERASED[0]}, settings={"apply_deleted_mask": 0}
        )
        assert unmasked.result_rows[0][0] == 0
        assert engine._cx.query(versions, parameters={"id": 2}).result_rows[0][0] == 1


def test_a_key_is_typed_and_validated_before_anything_is_read() -> None:
    sde.clear_registry()

    @sde.entity
    class Account:
        id: int
        email: str

    model = sde.build_model(Account)
    with pytest.raises(MigrationRefused, match="no entity"):
        ErasureRequest.build(model, "Nobody", [{"id": 1}])
    with pytest.raises(MigrationRefused, match="exactly the fields"):
        ErasureRequest.build(model, "Account", [{"email": "a@example.com"}])
    with pytest.raises(MigrationRefused, match="is not a int64"):
        ErasureRequest.build(model, "Account", [{"id": "1"}])
    with pytest.raises(MigrationRefused, match="is not a int64"):
        ErasureRequest.build(model, "Account", [{"id": True}])
    with pytest.raises(MigrationRefused, match="at least one key"):
        ErasureRequest.build(model, "Account", [])
    twice = ErasureRequest.build(model, "Account", [{"id": 3}, {"id": 1}, {"id": 3}])
    assert twice.keys == ((1,), (3,))
    assert twice.digests(b"a" * 32) != twice.digests(b"b" * 32)


def test_the_cli_lists_erases_and_refuses_a_key_that_is_not_json(tmp_path: Path) -> None:
    """``sde-operator erase`` as a client runs it, in a process of its own."""
    import base64
    import os
    import subprocess
    import sys

    from psycopg.conninfo import make_conninfo

    with initial("postgres", tmp_path / "state") as (
        operator, _stage, old, roles, model, public, *_,
    ):
        old.save("Event", {"id": ERASED[0], "value": 5})
        config = {
            "protocol": 1,
            "project_id": PROJECT,
            "model": sde.neutral_declaration(model),
            "public_keys": {"primary": base64.b64encode(public).decode()},
            "engines": {},
        }
        environment = dict(os.environ)
        for name, role in roles.items():
            op_name, run_name = "SDE_ERASE_" + name.upper(), "SDE_ERASE_APP_" + name.upper()
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
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config))
        keys = tmp_path / "keys.jsonl"
        keys.write_text(json.dumps({"id": ERASED[0]}) + "\n\n" + json.dumps({"id": 2}) + "\n")
        base = [
            sys.executable, "-m", "sde_operator", "--project-dir", str(operator.store.root),
            "--config", str(config_path), "erase", "--entity", "Event", "--keys", str(keys),
        ]

        def run(*extra: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [*base, *extra], env=environment, capture_output=True, text=True, timeout=60
            )

        listed = run()
        assert listed.returncode == 0, listed.stderr
        assert json.loads(listed.stdout)["tables"][0]["matching"] == 1
        assert roles["postgres"].operator.get("initial_events", {"id": ERASED[0]}) is not None

        erased = run("--execute")
        assert erased.returncode == 0, erased.stderr
        receipt = json.loads(erased.stdout)
        assert receipt["outcome"] == "erased" and receipt["keys"] == 2
        assert str(ERASED[0]) not in erased.stdout + erased.stderr
        assert roles["postgres"].operator.get("initial_events", {"id": ERASED[0]}) is None

        bad = subprocess.run(
            [*base[:-2], "--key", "{not json"], env=environment, capture_output=True, text=True,
            timeout=60,
        )
        assert bad.returncode == 2
        assert json.loads(bad.stderr) == {
            "error": "refused", "message": "--key 0 is not a JSON object"
        }

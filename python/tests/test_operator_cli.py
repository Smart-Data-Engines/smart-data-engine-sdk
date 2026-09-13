"""The local operator CLI reads verified snapshots and emits no local connection secrets."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

import sde
from sde._cutover_project import ProjectState
from sde.testing.loader import model_from_neutral
from sde_operator import __main__ as operator_cli


@pytest.fixture
def enrolled(tmp_path: Path) -> tuple[list[str], Path, dict[str, Any]]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    declaration = {
        "entities": [{"name": "Event", "fields": [{"name": "id", "type": "int64"}], "key": ["id"]}]
    }
    model = model_from_neutral(declaration)
    key = Ed25519PrivateKey.generate()
    project = "1" * 32
    raw = {
        "contract": 4,
        "project_id": project,
        "model_version": model.version,
        "map_version": 1,
        "groups": {
            "Event": {
                "write_epoch": 1,
                "source": {
                    "engine": "db",
                    "id": "source",
                    "layout": {
                        "tables": {"Event": "events"},
                        "columns": {"Event": {"id": "bigint"}},
                    },
                },
            }
        },
    }
    raw["signature"] = {
        "alg": "ed25519",
        "value": base64.b64encode(key.sign(sde.canonical_bytes(raw))).decode(),
    }
    state = tmp_path / "state"
    ProjectState(state, project, model.version).enroll(raw, sde.canonical_bytes(raw))
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "protocol": 1,
                "project_id": project,
                "model": declaration,
                "public_keys": {
                    "primary": base64.b64encode(key.public_key().public_bytes_raw()).decode()
                },
                "engines": {
                    "db": {
                        "dialect": "postgres",
                        "operator_dsn_env": "SDE_CLI_ADMIN",
                        "runtime_dsn_envs": ["SDE_CLI_RUNTIME"],
                    }
                },
            }
        )
    )
    return ["--project-dir", str(state), "--config", str(config)], state, raw


def test_offline_status_and_map_do_not_need_connection_variables(
    enrolled: Any, capsys: Any
) -> None:
    args, _state, original = enrolled
    assert operator_cli.main([*args, "status"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["active_map_version"] == 1
    assert result["phase"] is None
    assert operator_cli.main([*args, "current-map"]) == 0
    assert json.loads(capsys.readouterr().out) == original


def test_current_map_prints_only_the_bytes_it_verified(
    enrolled: Any, capsys: Any, monkeypatch: Any
) -> None:
    args, state, original = enrolled
    from sde import placement

    real_verify = placement._verify_signature

    def swap(raw: Any, keys: Any) -> Any:
        result = real_verify(raw, keys)
        changed = {**original, "map_version": 999}
        (state / "active-map.json").write_text(json.dumps(changed))
        return result

    monkeypatch.setattr(placement, "_verify_signature", swap)
    assert operator_cli.main([*args, "current-map"]) == 0
    assert json.loads(capsys.readouterr().out) == original


def test_invalid_signature_is_a_machine_readable_refusal(enrolled: Any, capsys: Any) -> None:
    args, state, original = enrolled
    (state / "active-map.json").write_text(json.dumps({**original, "map_version": 2}))
    assert operator_cli.main([*args, "current-map"]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert json.loads(output.err)["error"] == "configuration_error"


def test_invalid_model_is_not_a_traceback(enrolled: Any, capsys: Any) -> None:
    args, _state, _original = enrolled
    path = Path(args[3])
    config = json.loads(path.read_bytes())
    config["model"] = None
    path.write_text(json.dumps(config))
    assert operator_cli.main([*args, "status"]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "configuration_error"


def test_driver_error_does_not_echo_connection_details(
    enrolled: Any, capsys: Any, monkeypatch: Any
) -> None:
    from sde.engines.postgres import PostgresEngine

    args, _state, _original = enrolled
    monkeypatch.setenv("SDE_CLI_ADMIN", "controlled-secret-dsn")

    def fail(_self: Any) -> None:
        raise sde.EngineError("controlled-secret-dsn")

    monkeypatch.setattr(PostgresEngine, "connect", fail)
    assert operator_cli.main([*args, "resume"]) == 3
    output = capsys.readouterr()
    assert not output.out and "controlled-secret-dsn" not in output.err
    assert json.loads(output.err)["error"] == "engine_error"

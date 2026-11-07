"""`sde-operator depart` on a live PostgreSQL: a listing changes nothing, `--execute` releases.

The command is what a client who is leaving runs, with the operator configuration they already
have. Their application may have stopped using the library and its runtime logins may be gone, so
the runtime variable this configuration names is deliberately left unset: departure reads and
changes only what the operator's own login does.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

import pytest

import sde
from sde._cutover_project import ProjectState
from sde.testing.loader import model_from_neutral
from sde_operator import __main__ as operator_cli

DSN = os.environ.get("SDE_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(DSN is None, reason="set SDE_POSTGRES_DSN to run")
PROJECT = "c" * 32
DECLARATION = {
    "entities": [
        {
            "name": "Event",
            "fields": [{"name": "id", "type": "int64"}, {"name": "body", "type": "string"}],
            "key": ["id"],
        }
    ]
}


@pytest.fixture
def leaving(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[list[str], Any]]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from sde.engines.postgres import PostgresEngine

    assert DSN is not None
    schema = "sde_cli_depart_" + uuid4().hex[:12]
    parts = urlsplit(DSN)
    query = "&".join(
        [*(item for item in [parts.query] if item), "options=" + quote(f"-csearch_path={schema}")]
    )
    admin = urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
    with PostgresEngine(DSN) as setup:
        setup._cx.execute(f'CREATE SCHEMA "{schema}"')
    monkeypatch.setenv("SDE_CLI_DEPART_ADMIN", admin)
    monkeypatch.delenv("SDE_CLI_DEPART_RUNTIME", raising=False)

    model = model_from_neutral(DECLARATION)
    key = Ed25519PrivateKey.generate()
    raw: dict[str, Any] = {
        "contract": 4,
        "project_id": PROJECT,
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
                        "columns": {"Event": {"id": "bigint", "body": "text"}},
                    },
                },
            }
        },
    }
    raw["signature"] = {
        "alg": "ed25519",
        "value": base64.b64encode(key.sign(sde.canonical_bytes(raw))).decode(),
    }
    public = key.public_key().public_bytes_raw()
    state = tmp_path / "state"
    ProjectState(state, PROJECT, model.version).enroll(raw, sde.canonical_bytes(raw))
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "protocol": 1,
                "project_id": PROJECT,
                "model": DECLARATION,
                "public_keys": {"primary": base64.b64encode(public).decode()},
                "engines": {
                    "db": {
                        "dialect": "postgres",
                        "operator_dsn_env": "SDE_CLI_DEPART_ADMIN",
                        "runtime_dsn_envs": ["SDE_CLI_DEPART_RUNTIME"],
                    }
                },
            }
        )
    )
    engine = PostgresEngine(admin)
    engine.connect()
    placement = sde.load_map(raw, model=model, public_key=public, require_signature=True)
    sde.prepare_schema(model, placement, {"db": engine}, project_id=PROJECT)
    try:
        yield ["--project-dir", str(state), "--config", str(config)], engine
    finally:
        engine.close()
        with PostgresEngine(DSN) as setup:
            setup._cx.execute(f'DROP SCHEMA "{schema}" CASCADE')


def _plain_insert(engine: Any, identity: int) -> bool:
    try:
        with engine._cx.cursor() as cursor:
            cursor.execute("INSERT INTO events (id, body) VALUES (%s, 'x')", [identity])
    except Exception:
        return False
    return True


def test_depart_lists_without_changing_then_releases_and_cleans_up(
    leaving: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    args, engine = leaving
    assert operator_cli.main([*args, "depart"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["ready"] is True
    (db,) = listed["engines"]
    assert db["engine"] == "db" and len(db["release"]) == 3
    kinds = [(finding["kind"], finding["name"]) for finding in db["findings"]]
    assert ("fence", "events") in kinds and ("bookkeeping", "sde_map_state") in kinds
    # Listing is only listing: the fence still refuses the client's own write.
    assert not _plain_insert(engine, 1)

    assert operator_cli.main([*args, "depart", "--cleanup"]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "refused"
    assert not _plain_insert(engine, 1)

    assert operator_cli.main([*args, "depart", "--execute"]) == 0
    released = json.loads(capsys.readouterr().out)
    assert released["executed"]["db"] == db["release"]
    assert released["after"]["engines"][0]["release"] == []
    assert _plain_insert(engine, 1)

    assert operator_cli.main([*args, "depart", "--execute", "--cleanup"]) == 0
    cleaned = json.loads(capsys.readouterr().out)
    assert cleaned["after"]["engines"][0]["findings"] == []
    assert cleaned["after"]["engines"][0]["cleanup"] == []
    with engine._cx.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM events")
        assert cursor.fetchone()[0] == 1
        cursor.execute("SELECT to_regclass('sde_map_state') IS NULL")
        assert cursor.fetchone()[0] is True

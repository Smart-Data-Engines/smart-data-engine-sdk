"""``examples/trading`` on all three engines, as a client runs it: provision, run, verify - from
Python, and from TypeScript for orders, fills and trades, each reading what the other wrote.

The map is the one the control plane issues for this model when the orderbook takes the depth:
placement map contract 6, ``DepthLevel`` on the orderbook with no write generation, ``Fill`` (with
``Order``) on PostgreSQL and ``MarketTrade`` on ClickHouse with theirs. Signed here with a key of
the test's own. Runs in the SDK's ``orderbook`` CI job, which has all three engines and Node;
locally, with ``SDE_ORDERBOOK_TCP`` and both DSNs, and the TypeScript library built
(``npm run build``).
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from _orderbook_live import ENABLED, REASON, TCP

import sde
from sde.engines.clickhouse import ClickHouseEngine
from sde.engines.postgres import PostgresEngine

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "trading"
POSTGRES = os.environ.get("SDE_POSTGRES_DSN")
CLICKHOUSE = os.environ.get("SDE_CLICKHOUSE_DSN")
PROJECT = "2" * 32

pytestmark = pytest.mark.skipif(
    not (ENABLED and TCP and POSTGRES and CLICKHOUSE),
    reason=f"{REASON}; this one also needs SDE_ORDERBOOK_TCP and both DSNs",
)


@pytest.fixture
def places() -> Iterator[dict[str, str]]:
    """A fresh PostgreSQL schema and ClickHouse database, so the example's tables are its own."""
    assert POSTGRES and CLICKHOUSE
    name = "sde_trading_" + uuid4().hex[:12]
    with PostgresEngine(POSTGRES) as pg, ClickHouseEngine(CLICKHOUSE) as ch:
        pg._cx.execute(f'CREATE SCHEMA "{name}"')
        ch._cx.command(f"CREATE DATABASE {name}")
        try:
            parts = urlsplit(CLICKHOUSE)
            separator = "&" if "?" in POSTGRES else "?"
            yield {
                "TRADING_PG_DSN": f"{POSTGRES}{separator}options=-csearch_path%3D{name}",
                "TRADING_CH_DSN": urlunsplit(
                    (parts.scheme, parts.netloc, "/" + name, parts.query, parts.fragment)
                ),
                "TRADING_OB_DSN": f"orderbook://{TCP}",
            }
        finally:
            pg._cx.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
            ch._cx.command(f"DROP DATABASE IF EXISTS {name} SYNC")


def _signed_map(directory: Path) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from sde.testing.loader import model_from_neutral

    model = model_from_neutral(json.loads((EXAMPLE / "model.json").read_text()))
    engines = {"DepthLevel": ("ob-main", "orderbook"), "Fill": ("pg-main", "postgres")}
    engines["MarketTrade"] = ("ch-main", "clickhouse")
    groups: dict[str, Any] = {}
    for group in sde.colocation_groups(model):
        engine, dialect = engines[group.name]
        layout = sde.default_layout(model, group, dialect=dialect)
        body: dict[str, Any] = {
            "source": {
                "id": f"{group.name}@{engine}",
                "engine": engine,
                "layout": {
                    "tables": dict(layout.tables),
                    "columns": {entity: dict(cols) for entity, cols in layout.columns.items()},
                },
            }
        }
        if dialect != "orderbook":
            body["write_epoch"] = 1
        groups[group.name] = body
    document: dict[str, Any] = {
        "contract": 6,
        "project_id": PROJECT,
        "model_version": model.version,
        "map_version": 1,
        "groups": groups,
    }
    key = Ed25519PrivateKey.generate()
    document["signature"] = {
        "alg": "ed25519",
        "key_id": "trading",
        "value": base64.b64encode(key.sign(sde.canonical_bytes(document))).decode(),
    }
    (directory / "map.json").write_text(json.dumps(document))
    (directory / "keys.json").write_text(
        json.dumps({"trading": base64.b64encode(key.public_key().public_bytes_raw()).decode()})
    )
    shutil.copyfile(EXAMPLE / "engines.example.json", directory / "engines.json")


def _run(command: list[str], env: dict[str, str], cwd: Path) -> dict[str, Any]:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, f"{command[:3]}\n{result.stdout}\n{result.stderr}"
    output: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return output


def test_the_trading_example_provisions_runs_and_verifies_from_both_libraries(
    places: dict[str, str], tmp_path: Path
) -> None:
    _signed_map(tmp_path)
    env = {
        **os.environ,
        **places,
        "TRADING_PG_ADMIN_DSN": places["TRADING_PG_DSN"],
        "TRADING_CH_ADMIN_DSN": places["TRADING_CH_DSN"],
    }
    common = ["--map", "map.json", "--keys", "keys.json", "--project", PROJECT]
    common += ["--engines", "engines.json"]
    python = [sys.executable, str(EXAMPLE / "trading.py")]
    assert _run([*python, "provision", *common], env, tmp_path)["provisioned"] == [
        "ch-main",
        "ob-main",
        "pg-main",
    ]
    run = uuid4().hex
    sizes = ["--books", "2", "--updates", "30"]
    window_file = ["--window", "window.json", "--workload", "accounts"]
    _run([*python, "run", *common, "--run", run, *sizes, *window_file], env, tmp_path)
    verified = _run([*python, "verify", *common, "--run", run, *sizes], env, tmp_path)
    assert verified["mismatches"] == []
    assert verified["verified"] == {"depth": 300, "trades": 60, "orders": 3, "fills": 6}
    # And verify is a check that can fail: asked for one more step than was written, it says so.
    longer = subprocess.run(
        [*python, "verify", *common, "--run", run, "--books", "2", "--updates", "31"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=600,
    )
    assert longer.returncode == 1, longer.stdout + longer.stderr
    assert "depth differs (150 of 155 rows)" in longer.stdout
    window = json.loads((tmp_path / "window.json").read_text())
    assert set(window["groups"]) == {"DepthLevel", "Fill", "MarketTrade"}
    assert "total_bytes" in window["groups"]["DepthLevel"]["missing"]
    # The accounts workload is what an index on Order.account would be for, and the window says so.
    filtered = [
        entry
        for shape in window["groups"]["Fill"]["shapes"]
        if shape["entity"] == "Order"
        for entry in shape.get("filtered_on", ())
    ]
    assert {"equal": ["account"], "calls": 9} in filtered, filtered

    # The TypeScript half, from the library built in this tree, as an installed package would be.
    package = ROOT / "typescript"
    if not (package / "dist" / "engines" / "orderbook.js").is_file():
        pytest.fail("build the TypeScript library first: (cd typescript && npm run build)")
    node = tmp_path / "node"
    (node / "node_modules" / "@smart-data-engines").mkdir(parents=True)
    (node / "node_modules" / "@smart-data-engines" / "sde").symlink_to(package)
    for name in ("trading.mjs", "model.json"):
        shutil.copyfile(EXAMPLE / name, node / name)
    for name in ("map.json", "keys.json", "engines.json"):
        shutil.copyfile(tmp_path / name, node / name)
    other = uuid4().hex
    script = ["node", "trading.mjs"]
    _run([*script, "run", *common, "--run", other, *sizes], env, node)
    seen = _run(
        [*script, "verify", *common, "--run", other, *sizes, "--depth-run", run], env, node
    )
    assert seen["mismatches"] == []
    assert seen["verified"] == {"trades": 60, "orders": 3, "fills": 6, "depth": 300}

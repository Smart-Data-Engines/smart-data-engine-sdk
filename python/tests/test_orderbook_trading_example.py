"""``examples/trading`` on all three engines, as a client runs it: provision, run, verify - from
Python, from TypeScript for orders, fills and trades, and from C++ for all of it, each library
reading what another wrote.

The map is the one the control plane issues for this model when the orderbook takes the depth:
placement map contract 6, ``DepthLevel`` on the orderbook with no write generation, ``Fill`` (with
``Order``) on PostgreSQL and ``MarketTrade`` on ClickHouse with theirs. Signed here with a key of
the test's own. Runs in the SDK's ``orderbook`` CI job, which has all three engines and Node;
locally, with ``SDE_ORDERBOOK_TCP`` and both DSNs, the TypeScript library built
(``npm run build``) and, for the C++ half, ``SDE_CPP_TRADING`` naming the built example
(``cpp/build/examples/sde_example_trading``).
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
CPP_TRADING = os.environ.get("SDE_CPP_TRADING")
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


# What a window says that neither the clock nor the rows already in a table decide: a run's shapes,
# calls, errors and filters, and the proportions computed from them.
TIMING_OR_DATA = {
    "latency_p50_ms",
    "latency_p99_ms",
    "result_cardinality_p50",
    "result_cardinality_p99",
    "rows",
    "total_bytes",
    "index_to_table_ratio",
    "write_burstiness",
}


def _behaviour(window: dict[str, Any]) -> dict[str, Any]:
    groups = {}
    for name, group in window["groups"].items():
        kept = {key: value for key, value in group.items() if key not in TIMING_OR_DATA}
        kept["shapes"] = sorted(
            (
                {key: value for key, value in shape.items() if key not in TIMING_OR_DATA}
                for shape in group["shapes"]
            ),
            key=lambda shape: str(shape["id"]),
        )
        groups[name] = kept
    return {**{key: value for key, value in window.items() if key != "groups"}, "groups": groups}


@pytest.mark.skipif(
    not CPP_TRADING,
    reason="set SDE_CPP_TRADING to the built C++ example (cpp/build/examples/sde_example_trading)",
)
def test_the_cpp_half_provisions_and_each_library_verifies_what_the_other_wrote(
    places: dict[str, str], tmp_path: Path
) -> None:
    """The C++ program is a third client of the same signed map, holding its own connections.

    It provisions the three engines, writes a run that the reference reads back row by row, every
    field, and reads back a run the reference wrote: one traffic, derived from the run id in both
    programs - name-based UUIDs, exact decimals, microsecond instants. And its telemetry window of
    that traffic is the reference's, call for call, except what the clock and the rows already in
    the tables decide.
    """
    assert CPP_TRADING
    from sde.testing.loader import model_from_neutral

    # The model is declared in C++, and `declare` prints it for the control plane: the declaration
    # model.json holds for the other two, so one model version - what lets each verify the others.
    declared = json.loads(
        subprocess.run([CPP_TRADING, "declare"], capture_output=True, text=True, check=True).stdout
    )
    assert declared == json.loads((EXAMPLE / "model.json").read_text())
    assert model_from_neutral(declared).version == model_from_neutral(
        json.loads((EXAMPLE / "model.json").read_text())
    ).version
    _signed_map(tmp_path)
    env = {
        **os.environ,
        **places,
        "TRADING_PG_ADMIN_DSN": places["TRADING_PG_DSN"],
        "TRADING_CH_ADMIN_DSN": places["TRADING_CH_DSN"],
    }
    common = ["--map", "map.json", "--keys", "keys.json", "--project", PROJECT]
    common += ["--engines", "engines.json"]
    sizes = ["--books", "2", "--updates", "30"]
    cpp = [CPP_TRADING]
    python = [sys.executable, str(EXAMPLE / "trading.py")]
    assert _run([*cpp, "provision", *common], env, tmp_path)["provisioned"] == [
        "ch-main",
        "ob-main",
        "pg-main",
    ]

    written = uuid4().hex
    workload = ["--workload", "accounts"]
    _run([*cpp, "run", *common, "--run", written, *sizes, "--window", "cpp.json", *workload],
         env, tmp_path)
    read = _run([*python, "verify", *common, "--run", written, *sizes], env, tmp_path)
    assert read["mismatches"] == []
    assert read["verified"] == {"depth": 300, "trades": 60, "orders": 3, "fills": 6}

    other = uuid4().hex
    _run([*python, "run", *common, "--run", other, *sizes, "--window", "python.json", *workload],
         env, tmp_path)
    seen = _run([*cpp, "verify", *common, "--run", other, *sizes], env, tmp_path)
    assert seen["mismatches"] == []
    assert seen["verified"] == {"depth": 300, "trades": 60, "orders": 3, "fills": 6}
    # And the C++ verify is a check that can fail: one step more than was written is a difference.
    longer = subprocess.run(
        [*cpp, "verify", *common, "--run", other, "--books", "2", "--updates", "31"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=600,
    )
    assert longer.returncode == 1, longer.stdout + longer.stderr
    assert "depth differs (150 of 155 rows)" in longer.stdout

    of_cpp = json.loads((tmp_path / "cpp.json").read_text())
    of_python = json.loads((tmp_path / "python.json").read_text())
    assert _behaviour(of_cpp) == _behaviour(of_python)
    assert {"equal": ["account"], "calls": 9} in [
        entry
        for shape in of_cpp["groups"]["Fill"]["shapes"]
        if shape["entity"] == "Order"
        for entry in shape.get("filtered_on", ())
    ]

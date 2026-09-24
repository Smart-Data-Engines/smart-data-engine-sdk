"""In-place index builds measured after they exist: the real operator against the copy path.

The premise (``../premise``) compared the copy path with the engines' own non-blocking statements.
This runs the shipped operator instead - ``LocalCutover.index`` on a signed ``sde-index``
authorization, with its snapshot, native build, qualification from the catalogue, watermark and
atomic publication - on the same table sizes and against the same copy path, including a table the
copy path cannot finish inside the 30 s cutover budget.

For each engine and size:

- copy path: the staging live test's fixture with a designed copy in the source's engine (staging
  protocol 2), N rows, ``stage`` then the matching cutover; the pause is the cutover receipt's
  ``elapsed_ms`` and its outcome - past the budget the cutover aborts and the index is not there;
- in place: the in-place live test's fixture, the same N rows, a process on the map in force
  writing a row every 5 ms on its own connection, and ``LocalCutover.index``; the result is the
  receipt, the operator's wall time, and the writer's longest gap between two successful writes
  during the build against its longest gap before it.

It imports the live-test fixtures, so it runs from ``python/tests`` with both engine DSNs set:

    cd python/tests
    ../.venv/bin/python ../../docs/qualification/in-place-index/after/measure_after.py \\
        --rows 100000 450000 --out after.json
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

sys.path.insert(0, str(Path.cwd()))

import test_index_operator_live as in_place_fixture
from test_staging_operator_live import cutover, initial

import sde
from sde.engines.clickhouse import ClickHouseEngine
from sde.engines.postgres import PostgresEngine
from sde.generation import EPOCH_COLUMN
from sde.local_cutover import CutoverRecoveryRequired, LocalCutover

CHUNK = 20000


def seed(engine: Any, table: str, first: int, count: int) -> None:
    for start in range(first, first + count, CHUNK):
        rows = [
            {"id": i, "value": i % 100000, EPOCH_COLUMN: 1}
            for i in range(start, min(first + count, start + CHUNK))
        ]
        engine.copy_in(table, rows)


def index_for(dialect: str, name: str) -> dict[str, Any]:
    if dialect == "postgres":
        return {"entity": "Event", "name": name, "columns": ["value"]}
    return {
        "entity": "Event",
        "name": name,
        "columns": ["value"],
        "method": "minmax",
        "granularity": 4,
    }


def copy_path(dialect: str, rows: int) -> dict[str, Any]:
    with (
        TemporaryDirectory() as tmp,
        initial(
            dialect, Path(tmp), target=dialect, indexes=[index_for(dialect, "sde_i_after_000001")]
        ) as (operator, stage, _old, roles, model, public, signed, _m, _s, _t),
    ):
        seed(roles[dialect].operator, "initial_events", 2, rows - 1)
        started = time.monotonic()
        staged = operator.stage(stage).as_record()
        stage_s = time.monotonic() - started
        started = time.monotonic()
        recovery: dict[str, Any] | None = None
        try:
            receipt = operator.execute(cutover(stage, signed, model, public)).as_record()
            outcome, reason, elapsed = receipt["outcome"], receipt["reason"], receipt["elapsed_ms"]
            within = receipt["within_budget"]
        except CutoverRecoveryRequired as exc:
            # The operator's watchdog ended the pause: its connections are closed and the source is
            # still frozen. Fresh connections and a resume - without a decision, recovery aborts
            # and reopens the source - are what a customer would do next; both are recorded.
            outcome, reason, elapsed, within = "recovery_required", str(exc), None, None
            wall = time.monotonic() - started
            for name, role in roles.items():
                role.operator.close()
                role.operator.connect()
                if name == "postgres":
                    role.operator._cx.execute('SET search_path TO "' + role.namespace + '"')
                role.runtime.close()
                role.runtime.connect()
            resumed_at = time.monotonic()
            recovered = (
                LocalCutover(
                    Path(tmp),
                    model=model,
                    project_id="1" * 32,
                    public_key=public,
                    operators={name: role.operator for name, role in roles.items()},
                    runtime={name: [role.runtime] for name, role in roles.items()},
                )
                .resume()
                .as_record()
            )
            recovery = {
                "interrupted_after_ms": round(wall * 1000),
                "resume_ms": round((time.monotonic() - resumed_at) * 1000),
                "resumed_outcome": recovered["outcome"],
                "resumed_reason": recovered["reason"],
            }
        return {
            "path": "copy",
            "dialect": dialect,
            "rows": rows,
            "stage_ms": round(stage_s * 1000),
            "stage_outcome": staged["outcome"],
            "cutover_wall_ms": round((time.monotonic() - started) * 1000),
            "cutover_outcome": outcome,
            "cutover_reason": reason,
            "within_budget": within,
            "pause_ms": elapsed,
            "recovery": recovery,
            "index_in_force": outcome == "success",
        }


def in_place(dialect: str, rows: int) -> dict[str, Any]:
    with TemporaryDirectory() as tmp, in_place_fixture.initial(dialect, Path(tmp)) as build:
        seed(build.role.operator, in_place_fixture.TABLE, 2, rows - 1)
        engines = {
            name: (PostgresEngine if name == "postgres" else ClickHouseEngine)(role.runtime._dsn)
            for name, role in build.roles.items()
        }
        for engine in engines.values():
            engine.connect()
        writer = sde.Session(build.model, build.plan.current, engines, project_id="1" * 32)
        gaps = {"before": 0.0, "during": 0.0}
        phase = {"name": "before"}
        stop = threading.Event()
        errors: list[str] = []

        def write() -> None:
            last = time.monotonic()
            identity = rows + 1
            while not stop.is_set():
                try:
                    writer.save("Event", {"id": identity, "value": 1})
                    now = time.monotonic()
                    gaps[phase["name"]] = max(gaps[phase["name"]], now - last)
                    last = now
                    identity += 1
                except Exception as exc:
                    errors.append(type(exc).__name__)
                time.sleep(0.005)

        thread = threading.Thread(target=write)
        thread.start()
        time.sleep(2.0)
        phase["name"] = "during"
        started = time.monotonic()
        receipt = build.operator.index(build.plan).as_record()
        wall_s = time.monotonic() - started
        time.sleep(0.5)
        stop.set()
        thread.join()
        for engine in engines.values():
            engine.close()
        return {
            "path": "in_place",
            "dialect": dialect,
            "rows": rows,
            "outcome": receipt["outcome"],
            "receipt_elapsed_ms": receipt["elapsed_ms"],
            "operator_wall_ms": round(wall_s * 1000),
            "writer_max_gap_before_ms": round(gaps["before"] * 1000, 1),
            "writer_max_gap_during_ms": round(gaps["during"] * 1000, 1),
            "writer_errors": len(errors),
            "index_in_force": receipt["outcome"] == "built",
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, nargs="+", required=True)
    parser.add_argument("--dialects", nargs="+", default=["postgres", "clickhouse"])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    results = []
    for dialect in args.dialects:
        for rows in args.rows:
            for path in (in_place, copy_path):
                result = path(dialect, rows)
                results.append(result)
                print(json.dumps(result), flush=True)
    report = {
        "machine": {
            "node": platform.node(),
            "processor": platform.processor(),
            "python": platform.python_version(),
        },
        "sde_version": sde.__version__,
        "results": results,
    }
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

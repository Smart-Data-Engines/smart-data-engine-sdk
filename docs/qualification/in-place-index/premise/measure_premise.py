"""The premise of building an index in place, measured before it is designed.

Today a model's ``indexes_added`` decision runs as a relayout: a fresh copy in the same engine with
the index, then the ordinary cutover. That cutover copies every row into the copy and compares both
copies **while the source is frozen** (``LocalCutover._execute``: freeze, repair, verify_frozen), so
its write pause should grow with the table. Building the index on the live table would move no row.

For each engine and table size this runs both, on the SDK's own code paths:

- copy path: ``initial(..., target=engine, indexes=[...])`` from the staging live test, N rows seeded
  into the source, ``LocalCutover.stage`` and then ``execute`` of the matching cutover; the pause is
  the receipt's ``elapsed_ms`` and its outcome (a 30 s budget aborts);
- in place: the same N rows in a table of the same shape, a writer inserting a row every 5 ms on its
  own connection, and the index built with the engine's own non-blocking form - PostgreSQL
  ``CREATE INDEX CONCURRENTLY``, ClickHouse ``ADD INDEX`` then ``MATERIALIZE INDEX`` waited for with
  ``mutations_sync=2``; the pause is the writer's longest gap between two successful inserts during
  the build, against the same writer's longest gap before it.

It imports the live-test fixtures, so it runs from ``python/tests`` with both engine DSNs set:

    cd python/tests
    ../.venv/bin/python ../../docs/qualification/in-place-index/premise/measure_premise.py \
        --rows 10000 --out smoke.json
    ../.venv/bin/python ../../docs/qualification/in-place-index/premise/measure_premise.py \
        --rows 100000 300000 --out premise.json
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

from test_runtime_privileges_live import runtime_roles  # noqa: E402
from test_staging_operator_live import cutover, initial  # noqa: E402

import sde  # noqa: E402
from sde.generation import EPOCH_COLUMN  # noqa: E402

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
    return {"entity": "Event", "name": name, "columns": ["value"], "method": "minmax",
            "granularity": 4}


def copy_path(dialect: str, rows: int) -> dict[str, Any]:
    with TemporaryDirectory() as tmp, initial(
        dialect, Path(tmp), target=dialect, indexes=[index_for(dialect, "sde_i_premise_000001")]
    ) as (operator, stage, _old, roles, model, public, signed, _m, _s, _t):
        seed(roles[dialect].operator, "initial_events", 2, rows - 1)
        started = time.monotonic()
        staged = operator.stage(stage).as_record()
        stage_s = time.monotonic() - started
        started = time.monotonic()
        try:
            receipt = operator.execute(cutover(stage, signed, model, public)).as_record()
            outcome, reason, elapsed = receipt["outcome"], receipt["reason"], receipt["elapsed_ms"]
        except sde.EngineError as exc:  # a recovery-required refusal is a result too
            outcome, reason, elapsed = "recovery_required", type(exc).__name__, None
        return {
            "path": "copy",
            "dialect": dialect,
            "rows": rows,
            "stage_ms": round(stage_s * 1000),
            "stage_outcome": staged["outcome"],
            "cutover_wall_ms": round((time.monotonic() - started) * 1000),
            "cutover_outcome": outcome,
            "cutover_reason": reason,
            "pause_ms": elapsed,
        }


def in_place(dialect: str, rows: int) -> dict[str, Any]:
    with runtime_roles(dialect) as roles:
        operator = roles.operator
        if dialect == "postgres":
            operator._cx.execute(
                "CREATE TABLE premise_events (id bigint PRIMARY KEY, value integer, "
                f"{EPOCH_COLUMN} bigint)"
            )
        else:
            operator._cx.command(
                "CREATE TABLE premise_events (id Int64, value Int32, "
                f"{EPOCH_COLUMN} Int64) ENGINE = ReplacingMergeTree ORDER BY id"
            )
        seed(operator, "premise_events", 1, rows)
        if dialect == "postgres":
            from psycopg.conninfo import make_conninfo

            writer_engine: Any = type(operator)(
                make_conninfo(operator._dsn, options=f"-csearch_path={roles.namespace}")
            )
        else:
            writer_engine = type(operator)(operator._dsn)  # already the namespace's database
        gaps: dict[str, float] = {"before": 0.0, "during": 0.0}
        phase = {"name": "before"}
        stop = threading.Event()
        errors: list[str] = []

        def write() -> None:
            engine = writer_engine
            assert engine is not None
            engine.connect()
            last = time.monotonic()
            next_id = rows + 1
            while not stop.is_set():
                try:
                    engine.copy_in(
                        "premise_events", [{"id": next_id, "value": 1, EPOCH_COLUMN: 1}]
                    )
                    now = time.monotonic()
                    gaps[phase["name"]] = max(gaps[phase["name"]], now - last)
                    last = now
                    next_id += 1
                except Exception as exc:  # noqa: BLE001 - a failed write is a measured result
                    errors.append(type(exc).__name__)
                time.sleep(0.005)
            engine.close()

        thread = threading.Thread(target=write)
        thread.start()
        time.sleep(2.0)
        phase["name"] = "during"
        started = time.monotonic()
        if dialect == "postgres":
            operator._cx.execute("CREATE INDEX CONCURRENTLY premise_idx ON premise_events (value)")
            valid = operator._cx.execute(
                "SELECT i.indisvalid AND i.indisready FROM pg_index i "
                "WHERE i.indexrelid = to_regclass('premise_idx')"
            ).fetchone()[0]
        else:
            operator._cx.command(
                "ALTER TABLE premise_events ADD INDEX premise_idx value TYPE minmax GRANULARITY 4"
            )
            operator._cx.command(
                "ALTER TABLE premise_events MATERIALIZE INDEX premise_idx",
                settings={"mutations_sync": 2},
            )
            pending = operator._cx.query(
                "SELECT count() FROM system.mutations WHERE database = currentDatabase() "
                "AND table = 'premise_events' AND NOT is_done"
            ).result_rows[0][0]
            valid = pending == 0
        build_s = time.monotonic() - started
        time.sleep(0.5)
        stop.set()
        thread.join()
        return {
            "path": "in_place",
            "dialect": dialect,
            "rows": rows,
            "build_ms": round(build_s * 1000),
            "index_ready": bool(valid),
            "writer_max_gap_before_ms": round(gaps["before"] * 1000, 1),
            "writer_max_gap_during_ms": round(gaps["during"] * 1000, 1),
            "writer_errors": len(errors),
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
        "machine": {"node": platform.node(), "processor": platform.processor(),
                    "python": platform.python_version()},
        "sde_version": sde.__version__,
        "results": results,
    }
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

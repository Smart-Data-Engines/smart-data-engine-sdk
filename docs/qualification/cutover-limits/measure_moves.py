"""How large a group a cutover moves between engines within its pause budget, measured.

A move - staging protocol 1, a copy of the group in the other engine, then the cutover - pauses the
source's writes while the copy is refilled from the frozen source and every value compared
(``LocalCutover._execute``: freeze, repair, verify_frozen). The pause therefore grows with the
group, and a cutover whose pause passes its budget aborts. ``../in-place-index/`` measured the
relayout (the same engine); this measures the move, PostgreSQL to ClickHouse and back, on the SDK's
own code paths: ``initial(source, ...)`` from the staging live test, N rows seeded into the source,
``LocalCutover.stage`` and then ``execute`` of the matching cutover. The pause is the receipt's
``elapsed_ms``; a budget overrun is its outcome.

It imports the live-test fixtures, so it runs from ``python/tests`` with both engine DSNs set:

    cd python/tests
    ../.venv/bin/python ../../docs/qualification/cutover-limits/measure_moves.py \
        --rows 10000 100000 300000 --out moves.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

sys.path.insert(0, str(Path.cwd()))

from test_staging_operator_live import PROJECT, cutover, initial

import sde
from sde.generation import EPOCH_COLUMN
from sde.local_cutover import CutoverRecoveryRequired, LocalCutover

CHUNK = 20000
OTHER = {"postgres": "clickhouse", "clickhouse": "postgres"}


def seed(engine: Any, table: str, first: int, count: int) -> None:
    for start in range(first, first + count, CHUNK):
        rows = [
            {"id": i, "value": i % 100000, EPOCH_COLUMN: 1}
            for i in range(start, min(first + count, start + CHUNK))
        ]
        engine.copy_in(table, rows)


def move(source: str, rows: int) -> dict[str, Any]:
    target = OTHER[source]
    with TemporaryDirectory() as tmp, initial(source, Path(tmp), target=target) as (
        operator, stage, _old, roles, model, public, signed, _m, _s, _t,
    ):
        seed(roles[source].operator, "initial_events", 2, rows - 1)
        started = time.monotonic()
        staged = operator.stage(stage).as_record()
        stage_s = time.monotonic() - started
        started = time.monotonic()
        recovery: dict[str, Any] | None = None
        try:
            receipt = operator.execute(cutover(stage, signed, model, public)).as_record()
            outcome, reason, elapsed = receipt["outcome"], receipt["reason"], receipt["elapsed_ms"]
        except CutoverRecoveryRequired as exc:
            # The operator's watchdog ended the pause at the signed budget: its connections are
            # closed and the source is still frozen. Fresh connections and a resume - without a
            # decision, recovery aborts and reopens the source - are what a customer does next.
            outcome, reason, elapsed = "recovery_required", type(exc).__name__, None
            interrupted = time.monotonic() - started
            for name, role in roles.items():
                role.operator.close()
                role.operator.connect()
                if name == "postgres":
                    role.operator._cx.execute('SET search_path TO "' + role.namespace + '"')
                role.runtime.close()
                role.runtime.connect()
            resumed_at = time.monotonic()
            recovered = LocalCutover(
                operator.store.root,
                model=model,
                project_id=PROJECT,
                public_key=public,
                operators={name: role.operator for name, role in roles.items()},
                runtime={name: [role.runtime] for name, role in roles.items()},
            ).resume().as_record()
            recovery = {
                "interrupted_after_ms": round(interrupted * 1000),
                "resume_ms": round((time.monotonic() - resumed_at) * 1000),
                "resumed_outcome": recovered["outcome"],
                "resumed_reason": recovered["reason"],
            }
        return {
            "path": "move",
            "source": source,
            "target": target,
            "rows": rows,
            "stage_ms": round(stage_s * 1000),
            "stage_outcome": staged["outcome"],
            "cutover_wall_ms": round((time.monotonic() - started) * 1000),
            "cutover_outcome": outcome,
            "cutover_reason": reason,
            "pause_ms": elapsed,
            "recovery": recovery,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, nargs="+", required=True)
    parser.add_argument("--sources", nargs="+", default=["postgres", "clickhouse"])
    parser.add_argument("--repeat", type=int, default=1, help="runs of each source and size")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    results = []
    for source in args.sources:
        for rows in args.rows:
            for run in range(1, args.repeat + 1):
                load = os.getloadavg()
                result = move(source, rows)
                result["run"] = run
                result["load_before"] = [round(value, 2) for value in load]
                results.append(result)
                print(json.dumps(result), flush=True)
    report = {
        "machine": {"node": platform.node(), "processor": platform.processor(),
                    "cpus": os.cpu_count(), "python": platform.python_version()},
        "sde_version": sde.__version__,
        "results": results,
    }
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

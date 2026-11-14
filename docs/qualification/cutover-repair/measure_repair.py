"""A move's pause when its copy is filled before it, beside the pause when it is not, measured.

``../cutover-limits/`` measured the cutover as it was: a staging copies nothing, so the pause
empties the copy, refills it from the frozen source and compares every value. This measures the
same move - the same fixtures, the same seeding, the same receipt - in the ways the operator now
has, each on the SDK's own code paths:

- ``refill``: staging, then the cutover with ``repair = "refill"``. The pause as it was.
- ``backfill``: staging, ``LocalCutover.backfill`` while the source stays open, then the cutover.
  The pause compares the copy with the frozen source and writes what differs.
- ``backfill-lost``: the same, with rows written to the source alone between the backfill and the
  cutover - ``--lost`` of them in a thousand - as a session on the earlier map writes them.
- ``backfill-refill``: a backfill and then ``repair = "refill"``: the control that the backfill
  alone, without the repair of what differs, changes nothing in the pause.

The pause is the receipt's ``elapsed_ms``. The phases are the time between the operator's own step
markers, and ``repair`` is the operator's local record of what the repair did, numbers only.

It imports the live-test fixtures, so it runs from ``python/tests`` with both engine DSNs set:

    cd python/tests
    ../.venv/bin/python ../../docs/qualification/cutover-repair/measure_repair.py \
        --sources clickhouse --rows 200000 --ways refill backfill --repeat 3 --out repair.json
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
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cutover-limits"))

from measure_moves import OTHER, seed
from test_staging_operator_live import PROJECT, cutover, initial

import sde
from sde.local_cutover import CutoverRecoveryRequired, LocalCutover

WAYS = ("refill", "backfill", "backfill-lost", "backfill-refill")


def move(source: str, rows: int, way: str, chunk_rows: int, lost: int) -> dict[str, Any]:
    target = OTHER[source]
    with TemporaryDirectory() as tmp, initial(source, Path(tmp), target=target) as (
        operator, stage, _old, roles, model, public, signed, _m, _s, _t,
    ):
        seed(roles[source].operator, "initial_events", 2, rows - 1)
        operator.stage(stage)
        operator.chunk_rows = chunk_rows
        operator.repair = "refill" if way.endswith("refill") else "diff"
        backfill_ms: int | None = None
        if way.startswith("backfill"):
            started = time.monotonic()
            (progress,) = operator.backfill()
            backfill_ms = round((time.monotonic() - started) * 1000)
            assert progress.complete and progress.rows_this_run == rows, progress.as_record()
        written_after = 0
        if way == "backfill-lost":
            # Rows the source gains after the backfill and the copy never hears of, as a session
            # on the earlier map writes them: new keys, above the seeded ones.
            written_after = max(1, rows * lost // 1000)
            seed(roles[source].operator, "initial_events", rows + 1, written_after)
        marks: list[tuple[str, float]] = []
        operator._after_step = lambda step: marks.append((step, time.monotonic()))
        plan = cutover(stage, signed, model, public)
        started = time.monotonic()
        try:
            receipt = operator.execute(plan).as_record()
        except CutoverRecoveryRequired as exc:
            # The watchdog ended the pause at the signed budget: the operator's connections are
            # closed and the source is still frozen. Fresh connections and a resume - without a
            # decision, recovery aborts and reopens the source - are what a customer does next.
            interrupted = round((time.monotonic() - started) * 1000)
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
            return {
                "source": source, "target": target, "rows": rows, "way": way,
                "chunk_rows": chunk_rows, "backfill_ms": backfill_ms,
                "outcome": "recovery_required", "reason": type(exc).__name__,
                "interrupted_after_ms": interrupted,
                "resume_ms": round((time.monotonic() - resumed_at) * 1000),
                "resumed_outcome": recovered["outcome"],
                "resumed_reason": recovered["reason"],
            }
        ended = time.monotonic()
        phases: dict[str, int] = {}
        opened: dict[str, float] = {}
        for step, at in marks:
            phase, _, edge = step.partition(":")
            if edge == "intent":
                opened[phase] = at
            elif edge == "done" and phase in opened:
                phases[phase] = round((at - opened.pop(phase)) * 1000)
        with operator.store.lock():
            repair = operator.store.read()["completed"][plan.plan_id]["repair"]
        comparison = receipt["verification"]["comparison"]
        return {
            "source": source,
            "target": target,
            "rows": rows,
            "way": way,
            "chunk_rows": chunk_rows,
            "rows_written_after_the_backfill": written_after,
            "backfill_ms": backfill_ms,
            "outcome": receipt["outcome"],
            "reason": receipt["reason"],
            "pause_ms": receipt["elapsed_ms"],
            "within_budget": receipt["within_budget"],
            "repair_ms": phases.get("repair"),
            "verify_frozen_ms": phases.get("verify_frozen"),
            "repair": repair,
            "rows_source": comparison["rows_source"],
            "rows_target": comparison["rows_target"],
            "chunks_compared": comparison["chunks_compared"],
            "wall_ms": round((ended - started) * 1000),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, nargs="+", required=True)
    parser.add_argument("--sources", nargs="+", default=["postgres", "clickhouse"])
    parser.add_argument("--ways", nargs="+", default=list(WAYS), choices=WAYS)
    parser.add_argument("--chunk-rows", type=int, nargs="+", default=[1000])
    parser.add_argument("--lost", type=int, default=1, help="rows in a thousand, for backfill-lost")
    parser.add_argument("--repeat", type=int, default=1, help="runs of each combination")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    results = []
    for source in args.sources:
        for rows in args.rows:
            for chunk_rows in args.chunk_rows:
                for run in range(1, args.repeat + 1):
                    # The ways of one run side by side, so a drift of the machine falls on each.
                    for way in args.ways:
                        load = os.getloadavg()
                        result = move(source, rows, way, chunk_rows, args.lost)
                        result["run"] = run
                        result["load_before"] = [round(value, 2) for value in load]
                        results.append(result)
                        print(json.dumps(result), flush=True)
    report = {
        "machine": {"node": platform.node(), "processor": platform.processor(),
                    "cpus": os.cpu_count(), "python": platform.python_version()},
        "sde_version": sde.__version__,
        "commit": os.environ.get("SDE_MEASURED_COMMIT"),
        "results": results,
    }
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

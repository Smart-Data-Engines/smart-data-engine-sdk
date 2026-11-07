"""Where a move's pause goes: the cutover's phases timed, for one source and size, by chunk size.

``measure_moves.py`` measured the pause as a whole. This runs the same move - the same fixtures,
the same seeding - and records the time between the operator's own step markers
(``LocalCutover._after_step``: ``<phase>:intent`` before a phase runs, ``<phase>:done`` after),
so the pause splits into the phases ``LocalCutover._execute`` runs while the source is frozen:
the grant and fence changes, ``repair`` (empty the copy, refill it from the frozen source) and
``verify_frozen`` (compare every value). ``--chunk-rows`` sets ``LocalCutover.chunk_rows``, the
rows each read and write of those two phases moves at once (1000 by default).

    cd python/tests
    ../.venv/bin/python ../../docs/qualification/cutover-limits/measure_phases.py \
        --sources clickhouse --rows 100000 --chunk-rows 1000 5000 --out phases.json
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from measure_moves import OTHER, seed
from test_staging_operator_live import cutover, initial

import sde


def move(source: str, rows: int, chunk_rows: int) -> dict[str, Any]:
    target = OTHER[source]
    with TemporaryDirectory() as tmp, initial(source, Path(tmp), target=target) as (
        operator, stage, _old, roles, model, public, signed, _m, _s, _t,
    ):
        seed(roles[source].operator, "initial_events", 2, rows - 1)
        operator.stage(stage)
        marks: list[tuple[str, float]] = []
        operator._after_step = lambda step: marks.append((step, time.monotonic()))
        operator.chunk_rows = chunk_rows
        started = time.monotonic()
        receipt = operator.execute(cutover(stage, signed, model, public)).as_record()
        ended = time.monotonic()
        phases: dict[str, int] = {}
        opened: dict[str, float] = {}
        for step, at in marks:
            phase, _, edge = step.partition(":")
            if edge == "intent":
                opened[phase] = at
            elif edge == "done" and phase in opened:
                phases[phase] = round((at - opened.pop(phase)) * 1000)
        decided = next((at for step, at in marks if step.startswith("decision:")), None)
        return {
            "source": source,
            "target": target,
            "rows": rows,
            "chunk_rows": chunk_rows,
            "outcome": receipt["outcome"],
            "reason": receipt["reason"],
            "pause_ms": receipt["elapsed_ms"],
            "phases_ms": phases,
            "after_decision_ms": None if decided is None else round((ended - decided) * 1000),
            "wall_ms": round((ended - started) * 1000),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, nargs="+", required=True)
    parser.add_argument("--sources", nargs="+", default=["postgres", "clickhouse"])
    parser.add_argument("--chunk-rows", type=int, nargs="+", default=[1000])
    parser.add_argument("--repeat", type=int, default=1, help="runs of each combination")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    results = []
    for source in args.sources:
        for rows in args.rows:
            for chunk_rows in args.chunk_rows:
                for run in range(1, args.repeat + 1):
                    load = os.getloadavg()
                    result = move(source, rows, chunk_rows)
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

"""A cutover's pause after a settle, beside the pause after a backfill alone, measured.

``../cutover-repair/`` measured the pause after a backfill: it compares both sides of the group, so
it still grows with the group. This measures the same move after a generation advance and a
settle, whose pause reads only the rows that carry the advanced generation - each way on the SDK's
own code paths, with the same fixtures and seeding as that record:

- ``backfill``: staging, ``LocalCutover.backfill``, the cutover. The pause of ``../cutover-repair/``.
- ``settled``: staging, the backfill, ``LocalCutover.advance``, ``LocalCutover.settle``, the cutover.
- ``settled-written``: the same, with rows written after the settle - ``--since`` in a thousand of
  the group - under the advanced generation: to the source and the copy, as a session and its
  fan-out write them, and one in ten of them to the source alone, as a fan-out that failed.

The pause is the receipt's ``elapsed_ms``. The phases are the time between the operator's own step
markers, and ``repair`` is the operator's local record of what the repair did, numbers only. The
advance's own pause and the settle's time are measured too: they are what a settle costs.

It imports the live-test fixtures, so it runs from ``python/tests`` with both engine DSNs set:

    cd python/tests
    ../.venv/bin/python ../../docs/qualification/cutover-settled/measure_settled.py \
        --sources clickhouse --rows 200000 --repeat 3 --out settled.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

sys.path.insert(0, str(Path.cwd()))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cutover-limits"))

from measure_moves import CHUNK, OTHER, seed
from test_staging_operator_live import PROJECT, initial

import sde
from sde.generation import EPOCH_COLUMN
from sde.local_cutover import CutoverRecoveryRequired, LocalCutover

WAYS = ("backfill", "settled", "settled-written")


def advance_of(before: dict[str, Any], signed: Any, model: Any, public: bytes) -> Any:
    """The advance of the map in force: one generation up, nothing else."""
    after = deepcopy(before)
    after["map_version"] += 1
    after["groups"]["Event"]["write_epoch"] += 1
    packet = signed(
        {
            "kind": "sde-advance",
            "protocol": 1,
            "advance_id": uuid4().hex,
            "project_id": PROJECT,
            "group": "Event",
            "pause_budget_ms": 30000,
            "before": before,
            "after": signed(after),
        }
    )
    return sde.load_advance_plan(packet, model=model, project_id=PROJECT, public_key=public)


def cutover_from(before: dict[str, Any], signed: Any, model: Any, public: bytes) -> Any:
    """The cutover from ``before`` to its copy, as the staging fixture's cutover is built."""
    source = before["groups"]["Event"]["source"]
    target = deepcopy(before["groups"]["Event"]["derived"][0])
    target.pop("lag_budget_ms")
    success, abort = deepcopy(before), deepcopy(before)
    epoch = before["groups"]["Event"]["write_epoch"]
    success["map_version"], abort["map_version"] = (
        before["map_version"] + 1,
        before["map_version"] + 2,
    )
    success["groups"]["Event"] = {"source": target, "write_epoch": epoch + 2}
    abort["groups"]["Event"] = {"source": source, "write_epoch": epoch + 1}
    request = sde.verification_request(
        sde.load_map(before, model=model, public_key=public),
        project_id=PROJECT,
        group="Event",
        request_id=uuid4().hex,
        requested_at=datetime.now(UTC).isoformat(),
    )
    return sde.load_cutover_plan(
        signed(
            {
                "kind": "sde-cutover",
                "protocol": 1,
                "plan_id": uuid4().hex,
                "project_id": PROJECT,
                "group": "Event",
                "pause_budget_ms": 30000,
                "query_impact_digest": "a" * 64,
                "before": before,
                "success": signed(success),
                "abort": signed(abort),
                "verification": request.as_record(),
            }
        ),
        model=model,
        project_id=PROJECT,
        public_key=public,
    )


def written_since(engine: Any, table: str, first: int, count: int, epoch: int) -> None:
    """New keys under ``epoch``, as a session on the advanced map writes them."""
    for start in range(first, first + count, CHUNK):
        rows = [
            {"id": i, "value": i % 100000, EPOCH_COLUMN: epoch}
            for i in range(start, min(first + count, start + CHUNK))
        ]
        engine.copy_in(table, rows)


def phases_of(marks: list[tuple[str, float]]) -> dict[str, int]:
    phases: dict[str, int] = {}
    opened: dict[str, float] = {}
    for step, at in marks:
        phase, _, edge = step.partition(":")
        if edge == "intent":
            opened[phase] = at
        elif edge == "done" and phase in opened:
            phases[phase] = round((at - opened.pop(phase)) * 1000)
    return phases


def move(source: str, rows: int, way: str, chunk_rows: int, since: int) -> dict[str, Any]:
    target = OTHER[source]
    with TemporaryDirectory() as tmp, initial(source, Path(tmp), target=target) as (
        operator, stage, _old, roles, model, public, signed, _m, _s, _t,
    ):
        seed(roles[source].operator, "initial_events", 2, rows - 1)
        operator.stage(stage)
        operator.chunk_rows = chunk_rows
        started = time.monotonic()
        (progress,) = operator.backfill()
        backfill_ms = round((time.monotonic() - started) * 1000)
        assert progress.complete and progress.rows_this_run == rows, progress.as_record()
        before = stage.as_record()["prepared"]
        advance: dict[str, Any] | None = None
        settled: dict[str, Any] | None = None
        settle_ms: int | None = None
        if way.startswith("settled"):
            plan = advance_of(before, signed, model, public)
            started = time.monotonic()
            receipt = operator.advance(plan).as_record()
            advance = {
                "outcome": receipt["outcome"],
                "pause_ms": receipt["elapsed_ms"],
                "wall_ms": round((time.monotonic() - started) * 1000),
            }
            before = plan.as_record()["after"]
            started = time.monotonic()
            (settled,) = operator.settle()
            settle_ms = round((time.monotonic() - started) * 1000)
            assert settled["settled"], settled
        written_after = 0
        if way == "settled-written":
            written_after = max(10, rows * since // 1000)
            epoch = before["groups"]["Event"]["write_epoch"]
            copy_table = before["groups"]["Event"]["derived"][0]["layout"]["tables"]["Event"]
            lost = written_after // 10
            written_since(roles[source].operator, "initial_events", rows + 1, written_after, epoch)
            written_since(roles[target].operator, copy_table, rows + 1, written_after - lost, epoch)
        marks: list[tuple[str, float]] = []
        operator._after_step = lambda step: marks.append((step, time.monotonic()))
        plan = cutover_from(before, signed, model, public)
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
            resumed = LocalCutover(
                operator.store.root,
                model=model,
                project_id=PROJECT,
                public_key=public,
                operators={name: role.operator for name, role in roles.items()},
                runtime={name: [role.runtime] for name, role in roles.items()},
            ).resume().as_record()
            return {
                "source": source, "target": target, "rows": rows, "way": way,
                "chunk_rows": chunk_rows, "backfill_ms": backfill_ms, "advance": advance,
                "settle_ms": settle_ms, "outcome": "recovery_required",
                "reason": type(exc).__name__, "interrupted_after_ms": interrupted,
                "resumed_outcome": resumed["outcome"], "resumed_reason": resumed["reason"],
                "phases_before_the_deadline": phases_of(marks),
            }
        ended = time.monotonic()
        phases = phases_of(marks)
        with operator.store.lock():
            repair = operator.store.read()["completed"][plan.plan_id]["repair"]
        comparison = receipt["verification"]["comparison"]
        return {
            "source": source,
            "target": target,
            "rows": rows,
            "way": way,
            "chunk_rows": chunk_rows,
            "rows_written_since_the_settle": written_after,
            "backfill_ms": backfill_ms,
            "advance": advance,
            "settle_ms": settle_ms,
            "settle": settled and {
                key: settled[key] for key in ("chunks", "rows", "earlier", "written", "unconfirmed")
            },
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
    parser.add_argument("--since", type=int, default=1, help="rows in a thousand, written since")
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
                        result = move(source, rows, way, chunk_rows, args.since)
                        result["run"] = run
                        result["load_before"] = [round(value, 2) for value in load]
                        results.append(result)
                        print(json.dumps(result), flush=True)
                        args.out.write_text(json.dumps({"partial": results}, indent=2) + "\n")
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

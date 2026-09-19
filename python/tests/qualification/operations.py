"""Run a finite closed-loop operation benchmark against installed artifacts and exact oracles."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import sde

from .operation_metrics import account_for_work, summarize
from .project import project
from .weather_worker import reading

SDK = Path(__file__).resolve().parents[3]


def verify(session: sde.Session, worker: int, rows: int) -> None:
    """Check all values and reject extra source rows in the measured namespace."""
    seen = 0
    after = None
    while True:
        page = session.scan(
            "WeatherReading",
            where={"station": reading(worker, 1)["station"]},
            after=after,
            limit=1000,
            fresh=True,
        )
        for actual in page.rows:
            seen += 1
            if seen > rows or actual != reading(worker, seen):
                raise AssertionError(
                    "the measured source contains missing, changed or extra values"
                )
        if page.next_after is None:
            break
        if not page.rows:
            raise AssertionError("an empty page cannot advance verification")
        after = page.next_after
    if seen != rows:
        raise AssertionError("the measured source omitted acknowledged values")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--npm", type=Path, required=True)
    parser.add_argument("--source", choices=("postgres", "clickhouse"), required=True)
    parser.add_argument("--language", choices=("python", "typescript"), required=True)
    parser.add_argument("--mode", choices=("write", "read"), required=True)
    parser.add_argument("--method", choices=("save", "save_many"), default="save_many")
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--samples", type=int, default=500)
    parser.add_argument("--telemetry", choices=("on", "off"), default="on")
    args = parser.parse_args()
    args.scratch, args.python, args.npm = (
        args.scratch.resolve(),
        args.python.absolute(),
        args.npm.resolve(),
    )
    if args.scratch == SDK or SDK in args.scratch.parents:
        parser.error("use a fresh directory outside the source repository")
    if (
        not 1 <= args.rows <= 1_000_000
        or not 1 <= args.batch_size <= 1000
        or not 1 <= args.samples <= 10000
    ):
        parser.error("bounded positive rows, batch and samples are required")
    if args.method == "save" and args.batch_size != 1:
        parser.error("save needs batch-size 1")
    args.scratch.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "protocol": 1,
        "passed": False,
        "source": args.source,
        "language": args.language,
        "mode": args.mode,
        "method": args.method,
        "rows": args.rows,
        "batch_size": args.batch_size,
        "telemetry": args.telemetry,
        "controller_handoff_verified": False,
        "source_only_map": True,
        "generation_and_verification_outside_call_timers": True,
    }
    try:
        with project(args.scratch / "project", args.source) as configured:
            operator = configured["operator"]
            active = operator.active_map().groups["WeatherReading"].source
            if args.mode == "read":
                for first in range(1, args.rows + 1, 1000):
                    configured["roles"][active.engine].operator.copy_in(
                        active.layout.tables["WeatherReading"],
                        [
                            {**reading(99, n), sde.WRITE_EPOCH_COLUMN: 1}
                            for n in range(first, min(first + 1000, args.rows + 1))
                        ],
                    )
            env = {
                name: value
                for name, value in os.environ.items()
                if name in {"PATH", "LANG", "LC_ALL", "TZ"}
            }
            env.update(TMPDIR=str(args.scratch), PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
            env.update(
                {
                    name: value
                    for name, value in configured["environment"].items()
                    if name.startswith("SDE_QUALIFY_APP_")
                }
            )
            output = args.scratch / "worker.json"
            if args.language == "python":
                command = [str(args.python), "-B", "-m", "qualification.operation_worker"]
                env["PYTHONPATH"] = str(SDK / "python/tests")
            else:
                command = [
                    "node",
                    str(SDK / "typescript/tests/qualification/operation-worker.mjs"),
                    "--sdk",
                    str(args.npm),
                ]
            command += [
                "--config",
                str(configured["config"]),
                "--project-dir",
                str(configured["directory"] / "state"),
                "--output",
                str(output),
                "--mode",
                args.mode,
                "--method",
                args.method,
                "--rows",
                str(args.rows),
                "--batch-size",
                str(args.batch_size),
                "--samples",
                str(args.samples),
                "--telemetry",
                args.telemetry,
            ]
            with (
                (args.scratch / "worker.stdout").open("w") as out,
                (args.scratch / "worker.stderr").open("w") as err,
            ):
                completed = subprocess.run(command, env=env, stdout=out, stderr=err, timeout=900)
            if completed.returncode != 0:
                raise RuntimeError("the installed worker failed; inspect its retained stderr")
            raw = json.loads(output.read_bytes())
            if (
                raw["status"] != "complete"
                or raw["language"] != args.language
                or raw["rows"] != args.rows
            ):
                raise AssertionError(
                    "the worker report does not describe the requested completed run"
                )
            if (
                raw["method"] != args.method
                or raw["batch_size"] != args.batch_size
                or raw["mode"] != args.mode
            ):
                raise AssertionError("the worker substituted another workload")
            account_for_work(
                raw["samples"],
                mode=args.mode,
                method=args.method,
                rows=args.rows,
                batch_size=args.batch_size,
                read_samples=args.samples,
            )
            if args.mode == "write" and raw["acknowledged"] != args.rows:
                raise AssertionError("the requested writes were not acknowledged")
            imported = Path(raw["sdk_module"])
            if (
                args.language == "python" and not imported.is_relative_to(args.python.parent.parent)
            ) or (args.language == "typescript" and imported != args.npm):
                raise AssertionError("the worker did not use its installed artifact")
            if args.telemetry == "on":
                window = raw["window"]
                if (
                    window["model_version"] != configured["model"].version
                    or window["complete"] is not True
                    or window["dropped_windows"] != 0
                ):
                    raise AssertionError("actual SDK telemetry is missing or incomplete")
            with sde.Session(
                configured["model"],
                operator.active_map(),
                {name: role.runtime for name, role in configured["roles"].items()},
                project_id=configured["project_id"],
            ) as session:
                started = time.monotonic_ns()
                verify(session, 1 if args.mode == "write" else 99, args.rows)
                report["oracle_ns"] = time.monotonic_ns() - started
            report.update(
                summarize(raw["samples"], elapsed_ns=raw["elapsed_ns"], expected_rows=args.rows)
            )
            report.update(max_rss_kib=raw["max_rss_kib"], exact_values_verified=args.rows)
        # A failed fixture cleanup is a failed run as well.
        report["passed"] = True
        return 0
    except BaseException as error:
        report["error"] = type(error).__name__
        raise
    finally:
        (args.scratch / "report.json").write_text(
            json.dumps(report, indent=2, allow_nan=False) + "\n"
        )


if __name__ == "__main__":
    raise SystemExit(main())

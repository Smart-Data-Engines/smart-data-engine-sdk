"""Run a fixed-rate four-process workload from installed Python and npm artifacts."""

from __future__ import annotations

import argparse
import json
import os
import select
import subprocess
import time
from pathlib import Path
from typing import Any

import sde

from .metrics import analyze
from .project import project
from .transition import Transition
from .weather_worker import reading

SDK = Path(__file__).resolve().parents[3]


def baseline(args: argparse.Namespace, source: str) -> dict[str, Any]:
    processes: list[subprocess.Popen[str]] = []
    error_logs = []
    with project(args.scratch / source, source) as configured:
        directory = configured["directory"]
        start_file = directory / "start.json"
        environment = dict(os.environ, **configured["environment"], TMPDIR=str(args.scratch))
        environment.pop("PYTHONPATH", None)
        try:
            if args.seed_rows:
                active = configured["operator"].active_map().groups["WeatherReading"].source
                table = active.layout.tables["WeatherReading"]
                for begin in range(1, args.seed_rows + 1, 1000):
                    configured["roles"][active.engine].operator.copy_in(
                        table,
                        [
                            {**reading(99, sequence), sde.WRITE_EPOCH_COLUMN: 1}
                            for sequence in range(begin, min(begin + 1000, args.seed_rows + 1))
                        ],
                    )
            for worker in range(1, 5):
                language = "python" if worker <= 2 else "typescript"
                command = (
                    [str(args.python), str(Path(__file__).with_name("weather_worker.py"))]
                    if language == "python"
                    else [
                        "node",
                        str(SDK / "typescript/tests/qualification/weather-worker.mjs"),
                        "--sdk",
                        str(args.npm),
                    ]
                )
                command.extend(
                    [
                        "--config",
                        str(configured["config"]),
                        "--project-dir",
                        str(directory / "state"),
                        "--start",
                        str(start_file),
                        "--output",
                        str(directory / f"worker-{worker}.json"),
                        "--worker",
                        str(worker),
                        "--refresh-period-ms",
                        "500" if worker in (1, 3) else "0",
                    ]
                )
                errors = (directory / f"worker-{worker}.stderr").open("w")
                error_logs.append(errors)
                processes.append(
                    subprocess.Popen(
                        command, env=environment, stdout=subprocess.PIPE, stderr=errors, text=True
                    )
                )
            for process in processes:
                assert process.stdout is not None
                if (
                    not select.select([process.stdout], [], [], 30)[0]
                    or process.stdout.readline() != "READY\n"
                ):
                    raise RuntimeError(
                        "a workload process failed before READY; inspect its local stderr"
                    )
            origin = time.monotonic_ns() + 200_000_000
            start_file.write_text(
                json.dumps(
                    {
                        "monotonic_ns": str(origin),
                        "duration_ms": args.seconds * 1000,
                        "writes_per_second_per_worker": args.rate,
                    }
                )
            )
            print(f"START {source}: 2 Python + 2 TypeScript, {args.rate * 4} writes/s", flush=True)
            transition = Transition(args, configured, origin, environment)
            deadline = origin + (args.seconds + 60) * 1_000_000_000
            while any(process.poll() is None for process in processes):
                transition.step()
                if any(process.poll() not in (None, 0) for process in processes):
                    raise RuntimeError("a workload process failed; inspect its local stderr")
                if time.monotonic_ns() >= deadline:
                    raise TimeoutError("workload processes exceeded the finite completion deadline")
                time.sleep(0.1)
            if any(process.returncode != 0 for process in processes):
                raise RuntimeError("a workload process exited without a successful report")
            if args.mode != "baseline" and not transition.finished:
                raise AssertionError("traffic stopped before the required cutover completed")
            reports = [
                json.loads((directory / f"worker-{worker}.json").read_bytes())
                for worker in range(1, 5)
            ]
            expected = args.seconds * args.rate
            session = sde.Session(
                configured["model"],
                configured["operator"].active_map(),
                {name: role.runtime for name, role in configured["roles"].items()},
                project_id=configured["project_id"],
            )
            for sequence in range(1, args.seed_rows + 1):
                row = reading(99, sequence)
                observed = session.get(
                    "WeatherReading", {name: row[name] for name in ("station", "at")}
                )
                if observed != row:
                    raise AssertionError("a preloaded synthetic value is missing or different")
            summaries = []
            for worker, report in enumerate(reports, start=1):
                if (
                    report["worker"] != worker
                    or report["acknowledged"] != expected
                    or report["scheduled_writes"] != expected
                ):
                    raise AssertionError("a workload process did not complete its fixed schedule")
                if worker <= 2:
                    if not Path(report["sdk_module"]).is_relative_to(args.python.parent.parent):
                        raise AssertionError("the Python worker did not import the installed wheel")
                elif Path(report["sdk_module"]) != args.npm:
                    raise AssertionError(
                        "the TypeScript worker did not import the selected npm artifact"
                    )
                if any(
                    window["model_version"] != configured["model"].version
                    for window in report["windows"]
                ):
                    raise AssertionError("telemetry belongs to another declared model")
                if not report["windows"] or (args.mode == "baseline" and report["errors"]):
                    raise AssertionError("baseline errors or missing actual telemetry")
                for sequence in range(1, expected + 1):
                    row = reading(worker, sequence)
                    observed = session.get(
                        "WeatherReading", {name: row[name] for name in ("station", "at")}
                    )
                    if observed != row:
                        raise AssertionError(
                            "an acknowledged synthetic value is missing or different"
                        )
                if "qualification-station-" in json.dumps(report["windows"]):
                    raise AssertionError("telemetry contains a generated row value")
                saves = [sample for sample in report["samples"] if sample["op"] == "save"]
                if sum(sample["ok"] for sample in saves) != expected:
                    raise AssertionError("samples do not account for acknowledged writes")
                summaries.append(
                    {
                        "worker": worker,
                        "language": report["language"],
                        "acknowledged": expected,
                        **analyze(
                            report, transition.events, origin, origin + args.seconds * 1_000_000_000
                        ),
                        "max_rss_kib": report["max_rss_kib"],
                        "telemetry_windows": len(report["windows"]),
                        "sdk_module": report["sdk_module"],
                    }
                )
            controller_checked = False
            if "controller_state" in configured:
                control_root = configured["controller_state"]
                for path in control_root.rglob("*"):
                    if path.is_file() and path.suffix in (".json", ".jsonl", ".txt"):
                        content = path.read_text()
                        if "qualification-station-" in content or any(
                            dsn in content for dsn in configured["environment"].values()
                        ):
                            raise AssertionError(
                                "controller state contains a row value or an engine connection"
                            )
                maps = sorted(
                    (control_root / "clients/qualification/maps").glob("*.json"),
                    key=lambda path: int(path.stem),
                )
                controlled = sde.load_map(
                    json.loads(maps[-1].read_bytes()),
                    model=configured["model"],
                    public_key=configured["public"],
                    require_signature=True,
                )
                if controlled.fingerprint != configured["operator"].active_map().fingerprint:
                    raise AssertionError(
                        "controller and local operator disagree about the active map"
                    )
                controller_checked = True
            return {
                "controller_handoff_verified": controller_checked,
                "source": source,
                "passed": True,
                "seconds": args.seconds,
                "writes_per_second": args.rate * 4,
                "mode": args.mode,
                "events": transition.events,
                "all_acknowledged_values_checked": expected * 4,
                "preloaded_values_checked": args.seed_rows,
                "workers": summaries,
            }
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                if process.stdout is not None:
                    process.stdout.close()
            for stream in error_logs:
                stream.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument(
        "--python", type=Path, required=True, help="Python interpreter with an installed wheel"
    )
    parser.add_argument(
        "--npm",
        type=Path,
        required=True,
        help="installed @smart-data-engines/sde package directory",
    )
    parser.add_argument(
        "--mode",
        choices=("baseline", "success", "before_decision", "after_decision"),
        default="baseline",
    )
    parser.add_argument("--seed-rows", type=int, default=0)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument(
        "--rate", type=int, default=10, help="scheduled writes per worker per second"
    )
    args = parser.parse_args()
    args.scratch, args.python, args.npm = (
        args.scratch.resolve(),
        # Keep the venv executable path: resolving its symlink would select system Python.
        args.python.absolute(),
        args.npm.resolve(),
    )
    if (
        args.seconds <= 0
        or args.rate <= 0
        or args.seed_rows < 0
        or args.scratch == SDK
        or SDK in args.scratch.parents
    ):
        parser.error(
            "use a positive workload and a fresh scratch directory outside the SDK repository"
        )
    if args.mode != "baseline" and args.seconds < 60:
        parser.error("cutover scenarios need at least 60 seconds of scheduled traffic")
    args.scratch.mkdir(parents=True, exist_ok=False)
    result: dict[str, Any] = {
        "profile": "weather-append-point-v1",
        "passed": False,
        "cpu_count": os.cpu_count(),
        "initial_load": os.getloadavg(),
        "cases": [],
    }
    try:
        for source in ("postgres", "clickhouse"):
            result["cases"].append(baseline(args, source))
            (args.scratch / "report.json").write_text(json.dumps(result, indent=2))
            print(f"CHECKED {source}: every acknowledged value matched", flush=True)
        result["passed"] = True
        return 0
    except Exception as exc:
        result["error"] = type(exc).__name__
        raise
    finally:
        result["final_load"] = os.getloadavg()
        (args.scratch / "report.json").write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())

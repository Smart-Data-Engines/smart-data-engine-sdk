"""Synthetic application worker for the opt-in mixed-language cutover qualification.

Run with a local operator configuration and a shared start file. Only runtime environment
variables are read. Output contains telemetry and counters; generated rows stay in the engines.
The final acknowledged sequence is a contiguous prefix, independent of migration verification.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import resource
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import sde
from sde.engines.clickhouse import ClickHouseEngine
from sde.engines.postgres import PostgresEngine
from sde.testing.loader import model_from_neutral

BASE_TIME = datetime(2026, 9, 14, 12, 0, 0, 123456, tzinfo=UTC)


def reading(worker: int, sequence: int) -> dict[str, Any]:
    return {
        "station": f"qualification-station-{worker}",
        "at": BASE_TIME + timedelta(microseconds=sequence),
        "celsius": Decimal(1525 + sequence % 1000).scaleb(-2),
        "humidity": 30 + sequence % 70,
        "id": UUID(f"{worker:08x}-0000-4000-8000-{sequence:012x}"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--start", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", type=int, required=True)
    parser.add_argument("--refresh-period-ms", type=int, default=500)
    args = parser.parse_args()
    config = json.loads(args.config.read_bytes())
    model = model_from_neutral(config["model"])
    keys = {name: base64.b64decode(value) for name, value in config["public_keys"].items()}
    recorder = sde.Recorder(model.version)
    engines: dict[str, Any] = {}
    counters: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    windows: list[dict[str, Any]] = []
    acknowledged = 0
    failures: list[dict[str, Any]] = []
    maximum_rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    try:
        for name, binding in config["engines"].items():
            kind = PostgresEngine if binding["dialect"] == "postgres" else ClickHouseEngine
            engines[name] = kind(os.environ[binding["runtime_dsn_envs"][0]])
            engines[name].connect()

        active_fingerprint = None

        def open_session(current: sde.Session | None = None) -> sde.Session:
            nonlocal active_fingerprint
            placement = sde.load_local_map(
                args.project_dir, model=model, project_id=config["project_id"], public_key=keys
            )
            if current is not None and placement.fingerprint == active_fingerprint:
                return current
            opened = sde.Session(
                model, placement, engines, recorder=recorder, project_id=config["project_id"]
            )
            active_fingerprint = placement.fingerprint
            return opened

        session = open_session()
        print("READY", flush=True)
        waiting_until = time.monotonic_ns() + 30_000_000_000
        while not args.start.exists():
            if time.monotonic_ns() >= waiting_until:
                raise TimeoutError("qualification coordinator did not supply a start time")
            time.sleep(0.01)
        start = json.loads(args.start.read_bytes())
        origin = int(start["monotonic_ns"])
        duration_ns = int(start["duration_ms"]) * 1_000_000
        rate = int(start["writes_per_second_per_worker"])
        if rate <= 0 or duration_ns <= 0 or not 1 <= args.worker <= 100:
            raise ValueError("invalid qualification workload configuration")
        period = 1_000_000_000 // rate
        deadline = origin + duration_ns
        refresh_at = roll_at = origin
        reconnect = False
        while time.monotonic_ns() < deadline:
            due = origin + acknowledged * period
            now = time.monotonic_ns()
            if now < due:
                time.sleep(min((due - now) / 1_000_000_000, 0.02))
                continue
            if now >= roll_at:
                window = recorder.roll()
                if window is not None:
                    windows.append(window.as_record(model))
                    recorder.acknowledge(1)
                roll_at = now + 5_000_000_000
                maximum_rss_kib = max(
                    maximum_rss_kib, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                )
            try:
                if reconnect:
                    for engine in engines.values():
                        engine.close()
                        engine.connect()
                if reconnect or (args.refresh_period_ms > 0 and now >= refresh_at):
                    session = open_session(None if reconnect else session)
                    reconnect = False
                    refresh_at = now + args.refresh_period_ms * 1_000_000
            except sde.SdeError as exc:
                counters["refresh_" + type(exc).__name__] += 1
                failures.append(
                    {
                        "op": "refresh",
                        "error": type(exc).__name__,
                        "time_ns": str(time.monotonic_ns()),
                    }
                )
                reconnect = True
                time.sleep(0.05)
                continue
            row = reading(args.worker, acknowledged + 1)
            began = time.monotonic_ns()
            try:
                session.save("WeatherReading", row)
            except sde.SdeError as exc:
                counters["save_" + type(exc).__name__] += 1
                failures.append(
                    {"op": "save", "error": type(exc).__name__, "time_ns": str(time.monotonic_ns())}
                )
                reconnect = True
                samples.append(
                    {
                        "op": "save",
                        "ok": False,
                        "start_ns": str(began),
                        "duration_ns": time.monotonic_ns() - began,
                        "scheduled_ns": str(due),
                    }
                )
                continue
            samples.append(
                {
                    "op": "save",
                    "ok": True,
                    "start_ns": str(began),
                    "duration_ns": time.monotonic_ns() - began,
                    "scheduled_ns": str(due),
                }
            )
            acknowledged += 1
            if acknowledged % 5 == 0:
                began = time.monotonic_ns()
                try:
                    observed = session.get(
                        "WeatherReading", {name: row[name] for name in ("station", "at")}
                    )
                except sde.SdeError as exc:
                    counters["get_" + type(exc).__name__] += 1
                    failures.append(
                        {
                            "op": "get",
                            "error": type(exc).__name__,
                            "time_ns": str(time.monotonic_ns()),
                        }
                    )
                    reconnect = True
                    samples.append(
                        {
                            "op": "get",
                            "ok": False,
                            "start_ns": str(began),
                            "duration_ns": time.monotonic_ns() - began,
                        }
                    )
                    continue
                if observed != row:
                    raise AssertionError(
                        "an acknowledged point read returned another synthetic value"
                    )
                samples.append(
                    {
                        "op": "get",
                        "ok": True,
                        "start_ns": str(began),
                        "duration_ns": time.monotonic_ns() - began,
                    }
                )
        window = recorder.roll()
        if window is not None:
            windows.append(window.as_record(model))
            recorder.acknowledge(1)
        result = {
            "protocol": 1,
            "language": "python",
            "worker": args.worker,
            "sdk_module": sde.__file__,
            "acknowledged": acknowledged,
            "scheduled_writes": duration_ns // period,
            "finished_ns": str(time.monotonic_ns()),
            "errors": dict(counters),
            "failures": failures,
            "max_rss_kib": max(maximum_rss_kib, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
            "samples": samples,
            "windows": windows,
        }
        args.output.write_text(json.dumps(result, separators=(",", ":")))
        print("DONE", flush=True)
        return 0
    finally:
        for engine in engines.values():
            engine.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as failure:
        # Driver messages can contain row values or connection details. The coordinator receives
        # the failure class and nonzero exit status, never a misleading partial success report.
        print(json.dumps({"error": type(failure).__name__}), file=sys.stderr, flush=True)
        sys.exit(1)

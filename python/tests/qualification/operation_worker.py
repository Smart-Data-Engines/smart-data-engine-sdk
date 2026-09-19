"""Measure installed SDK calls; generate and check synthetic values outside each call timer."""

from __future__ import annotations

import argparse
import base64
import json
import os
import resource
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import sde
from sde.engines.clickhouse import ClickHouseEngine
from sde.engines.postgres import PostgresEngine
from sde.testing.loader import model_from_neutral

from .weather_worker import BASE_TIME, reading


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("write", "read"), required=True)
    parser.add_argument("--method", choices=("save", "save_many"), default="save_many")
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--samples", type=int, default=500)
    parser.add_argument("--telemetry", choices=("on", "off"), default="on")
    args = parser.parse_args()
    if (
        not 1 <= args.rows <= 1_000_000
        or not 1 <= args.batch_size <= 1000
        or not 1 <= args.samples <= 10000
    ):
        parser.error("bounded positive rows, batch and samples are required")
    if args.method == "save" and args.batch_size != 1:
        parser.error("save measures exactly one row")
    config = json.loads(args.config.read_bytes())
    model = model_from_neutral(config["model"])
    keys = {name: base64.b64decode(value) for name, value in config["public_keys"].items()}
    placement = sde.load_local_map(
        args.project_dir, model=model, project_id=config["project_id"], public_key=keys
    )
    name = placement.groups["WeatherReading"].source.engine
    binding = config["engines"][name]
    kind: Any = PostgresEngine if binding["dialect"] == "postgres" else ClickHouseEngine
    recorder = sde.Recorder(model.version) if args.telemetry == "on" else None
    samples: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "protocol": 1,
        "language": "python",
        "mode": args.mode,
        "method": args.method,
        "batch_size": args.batch_size,
        "rows": args.rows,
        "sdk_module": sde.__file__,
        "status": "incomplete",
        "acknowledged": 0,
        "samples": samples,
        "telemetry_enabled": recorder is not None,
    }
    began = time.perf_counter_ns()
    try:
        with kind(os.environ[binding["runtime_dsn_envs"][0]]) as engine, sde.Session(
            model, placement, {name: engine}, recorder=recorder, project_id=config["project_id"]
        ) as session:
            if args.mode == "write":
                # Warm the same table/operation using a separate synthetic key namespace.
                for offset in range(2):
                    rows = [
                        reading(98, offset * args.batch_size + n + 1)
                        for n in range(args.batch_size)
                    ]
                    if args.method == "save":
                        session.save("WeatherReading", rows[0])
                    else:
                        session.save_many("WeatherReading", rows)
                began = time.perf_counter_ns()
                for first in range(1, args.rows + 1, args.batch_size):
                    rows = [
                        reading(1, n)
                        for n in range(first, min(first + args.batch_size, args.rows + 1))
                    ]
                    start = time.perf_counter_ns()
                    if args.method == "save":
                        session.save("WeatherReading", rows[0])
                    else:
                        session.save_many("WeatherReading", rows)
                    duration = time.perf_counter_ns() - start
                    samples.append(
                        {"operation": args.method, "duration_ns": duration, "rows": len(rows)}
                    )
                    report["acknowledged"] += len(rows)
            else:
                where = {"station": reading(99, 1)["station"]}
                cents = (
                    1525 * args.rows
                    + (args.rows // 1000) * 499500
                    + (args.rows % 1000) * (args.rows % 1000 + 1) // 2
                )
                for _ in range(20):
                    assert session.get(
                        "WeatherReading",
                        {
                            "station": where["station"],
                            "at": BASE_TIME + timedelta(microseconds=1),
                        },
                    ) == reading(99, 1)
                began = time.perf_counter_ns()
                for index in range(args.samples):
                    sequence = 1 + index * 104729 % args.rows
                    expected = reading(99, sequence)
                    result: Any
                    for operation in ("get", "scan", "count", "summarize"):
                        start = time.perf_counter_ns()
                        if operation == "get":
                            result = session.get(
                                "WeatherReading",
                                {"station": expected["station"], "at": expected["at"]},
                            )
                        elif operation == "scan":
                            result = session.scan(
                                "WeatherReading",
                                where=where,
                                bounds=sde.Range("at", low=expected["at"]),
                                limit=1000,
                            )
                        elif operation == "count":
                            result = session.count("WeatherReading", where=where)
                        else:
                            result = session.summarize("WeatherReading", "celsius", where=where)
                        duration = time.perf_counter_ns() - start
                        returned = 1
                        if operation == "get":
                            assert result == expected
                        elif operation == "scan":
                            returned = min(1000, args.rows - sequence + 1)
                            assert list(result.rows) == [
                                reading(99, sequence + n) for n in range(returned)
                            ]
                        elif operation == "count":
                            assert result == args.rows
                        else:
                            assert (
                                result.count == args.rows and result.non_null_count == args.rows
                            )
                            assert result.total == Decimal(cents).scaleb(-2)
                        samples.append(
                            {"operation": operation, "duration_ns": duration, "rows": returned}
                        )
            report["elapsed_ns"] = time.perf_counter_ns() - began
            report["status"] = "complete"
    finally:
        report["max_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if recorder:
            window = recorder.roll()
            assert window is not None
            report["window"] = window.as_record(model)
        args.output.write_text(json.dumps(report, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

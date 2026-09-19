"""Validate closed-loop SDK measurements without presenting them as scheduled-load latency."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping
from typing import Any


def summarize(
    samples: list[Mapping[str, Any]], *, elapsed_ns: int, expected_rows: int
) -> dict[str, Any]:
    if type(elapsed_ns) is not int or elapsed_ns <= 0:
        raise ValueError("elapsed time must be positive integral nanoseconds")
    if type(expected_rows) is not int or expected_rows <= 0 or not samples:
        raise ValueError("a benchmark needs actual expected rows and samples")
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for sample in samples:
        if set(sample) != {"operation", "duration_ns", "rows"}:
            raise ValueError("invalid measurement fields")
        if sample["operation"] not in {"save", "save_many", "get", "scan", "count", "summarize"}:
            raise ValueError("unknown operation")
        if type(sample["duration_ns"]) is not int or sample["duration_ns"] <= 0:
            raise ValueError("a duration must be positive integral nanoseconds")
        if type(sample["rows"]) is not int or sample["rows"] <= 0:
            raise ValueError("a measurement must represent actual rows")
        grouped.setdefault(sample["operation"], []).append(sample)
    writes = [sample for sample in samples if sample["operation"] in {"save", "save_many"}]
    if writes and sum(sample["rows"] for sample in writes) != expected_rows:
        raise ValueError("write samples do not account for every acknowledged row")
    if sum(sample["duration_ns"] for sample in samples) > elapsed_ns:
        raise ValueError("sequential measured calls exceed the enclosing interval")
    result: dict[str, Any] = {}
    for operation, values in sorted(grouped.items()):
        durations = sorted(sample["duration_ns"] / 1_000_000 for sample in values)
        result[operation] = {
            "calls": len(values),
            "rows": sum(sample["rows"] for sample in values),
            "latency_ms": {
                str(p): durations[math.ceil(len(durations) * p / 100) - 1] for p in (50, 95, 99)
            },
            "measured_call_seconds": sum(sample["duration_ns"] for sample in values) / 1e9,
        }
    return {
        "arrival_model": "closed_loop",
        "scheduled_latency_measured": False,
        "elapsed_seconds": elapsed_ns / 1e9,
        "operations": result,
    }


def account_for_work(
    samples: list[Mapping[str, Any]],
    *,
    mode: str,
    method: str,
    rows: int,
    batch_size: int,
    read_samples: int,
) -> None:
    """The requested workload, not a self-reported acknowledgement, defines measured work."""
    if mode == "write":
        expected = [min(batch_size, rows - offset) for offset in range(0, rows, batch_size)]
        if [sample["rows"] for sample in samples] != expected or any(
            sample["operation"] != method for sample in samples
        ):
            raise ValueError("write samples do not describe the requested operation and batches")
    elif mode == "read":
        if Counter(sample["operation"] for sample in samples) != {
            name: read_samples for name in ("get", "scan", "count", "summarize")
        }:
            raise ValueError("read samples do not describe every requested operation")
        if any(sample["rows"] != 1 for sample in samples if sample["operation"] != "scan"):
            raise ValueError("scalar and point operations return exactly one result")
    else:
        raise ValueError("unknown benchmark mode")

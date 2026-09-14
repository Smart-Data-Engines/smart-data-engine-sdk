"""Acceptance boundaries for the declared local workload, including scheduled request latency."""

from __future__ import annotations

import math
from typing import Any

STEADY_P99_MS = 250
MAX_RESPONSE_MS = 45000
RECOVERY_GRACE_MS = 10000
MAX_WORKER_RSS_KIB = 256 * 1024


def percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        raise ValueError("qualification has no latency samples")
    ordered = sorted(values)
    return {str(p): ordered[max(0, math.ceil(len(ordered) * p / 100) - 1)] for p in (50, 95, 99)}


def analyze(
    report: dict[str, Any], events: list[dict[str, Any]], origin: int, end: int
) -> dict[str, Any]:
    if type(report["max_rss_kib"]) is not int or report["max_rss_kib"] < 1:
        raise AssertionError("invalid process memory measurement")
    for sample in report["samples"]:
        if sample["op"] not in {"save", "get"} or type(sample["ok"]) is not bool:
            raise AssertionError("invalid operation sample")
        duration = sample["duration_ns"]
        if type(duration) is not int or duration < 0 or int(sample["start_ns"]) < origin:
            raise AssertionError("invalid or nonfinite sample timing")
        if sample["op"] == "save" and int(sample["scheduled_ns"]) < origin:
            raise AssertionError("sample predates the common schedule")
    if any(type(count) is not int or count < 1 for count in report["errors"].values()):
        raise AssertionError("invalid failure counter")
    cutovers = [event for event in events if event["action"] != "stage"]
    pauses: list[tuple[int, int]] = []
    if cutovers:
        pauses.append(
            (
                min(int(event["start_ns"]) for event in cutovers),
                max(int(event["end_ns"]) for event in cutovers),
            )
        )
    excluded = [
        (int(event["start_ns"]), int(event["end_ns"]) + 2_000_000_000)
        for event in events
        if event["action"] == "stage"
    ]
    excluded.extend((start, stop + RECOVERY_GRACE_MS * 1_000_000) for start, stop in pauses)
    for failure in report["failures"]:
        moment = int(failure["time_ns"])
        if failure["error"] not in {"EngineError", "MigrationRefused", "MapRolledBack"} or not any(
            start <= moment <= stop + 2_000_000_000 for start, stop in pauses
        ):
            raise AssertionError(
                "an application refusal occurred outside the cutover recovery window"
            )
    if sum(report["errors"].values()) != len(report["failures"]):
        raise AssertionError("failure timestamps do not account for the reported errors")
    if any(
        window["complete"] is not True or window["dropped_windows"] != 0
        for window in report["windows"]
    ):
        raise AssertionError("workload telemetry is incomplete or dropped")
    if report["max_rss_kib"] > MAX_WORKER_RSS_KIB:
        raise AssertionError("a workload process exceeded the declared memory ceiling")

    def steady(sample: dict[str, Any]) -> bool:
        start = int(sample["start_ns"])
        stop = start + sample["duration_ns"]
        return sample["ok"] and not any(
            start <= after and stop >= before for before, after in excluded
        )

    writes = [sample for sample in report["samples"] if sample["op"] == "save" and sample["ok"]]
    normal_writes = [sample for sample in writes if steady(sample)]
    normal_reads = [
        sample for sample in report["samples"] if sample["op"] == "get" and steady(sample)
    ]
    call = percentiles([sample["duration_ns"] / 1e6 for sample in normal_writes])
    read = percentiles([sample["duration_ns"] / 1e6 for sample in normal_reads])
    delay = percentiles(
        [(int(sample["start_ns"]) - int(sample["scheduled_ns"])) / 1e6 for sample in normal_writes]
    )
    if any(value["99"] > STEADY_P99_MS for value in (call, read, delay)):
        raise AssertionError(
            "steady traffic exceeded the declared p99 latency or scheduling boundary"
        )
    response = [
        (int(sample["start_ns"]) + sample["duration_ns"] - int(sample["scheduled_ns"])) / 1e6
        for sample in writes
    ]
    completions = [int(sample["start_ns"]) + sample["duration_ns"] for sample in writes]
    gaps = [
        (after - before) / 1e6
        for before, after in zip(
            [origin, *completions], [*completions, max(end, completions[-1])], strict=True
        )
    ]
    if max(response) > MAX_RESPONSE_MS or max(gaps) > MAX_RESPONSE_MS:
        raise AssertionError(
            "cutover caused a response or success gap beyond the declared boundary"
        )
    return {
        "steady_write_ms": call,
        "steady_read_ms": read,
        "steady_schedule_delay_ms": delay,
        "all_scheduled_response_ms": percentiles(response),
        "max_scheduled_response_ms": max(response),
        "max_success_gap_ms": max(gaps),
        "errors": report["errors"],
        "steady_write_samples": len(normal_writes),
        "steady_read_samples": len(normal_reads),
    }

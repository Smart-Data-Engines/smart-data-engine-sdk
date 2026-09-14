"""A workload report cannot pass by hiding latency, missing telemetry or unbound failures."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from qualification.metrics import analyze

ORIGIN = 100_000_000_000


def report(count: int = 100) -> dict[str, Any]:
    samples = []
    for number in range(count):
        due = ORIGIN + number * 100_000_000
        samples.append(
            {
                "op": "save",
                "ok": True,
                "start_ns": str(due + 1_000_000),
                "duration_ns": 10_000_000,
                "scheduled_ns": str(due),
            }
        )
        if number % 5 == 0:
            samples.append(
                {
                    "op": "get",
                    "ok": True,
                    "start_ns": str(due + 12_000_000),
                    "duration_ns": 5_000_000,
                }
            )
    return {
        "samples": samples,
        "errors": {},
        "failures": [],
        "max_rss_kib": 65536,
        "windows": [{"complete": True, "dropped_windows": 0}],
    }


def test_valid_measurements_keep_schedule_latency_separate_from_call_latency() -> None:
    result = analyze(report(), [], ORIGIN, ORIGIN + 10_000_000_000)
    assert result["steady_write_ms"]["99"] == 10
    assert result["steady_schedule_delay_ms"]["99"] == 1
    assert result["all_scheduled_response_ms"]["99"] == 11
    assert result["steady_write_samples"] == 100
    assert result["steady_read_samples"] == 20


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -1, True])
def test_nonfinite_or_invalid_durations_cannot_satisfy_latency_thresholds(invalid: Any) -> None:
    raw = report()
    raw["samples"][0]["duration_ns"] = invalid
    with pytest.raises(AssertionError, match="timing"):
        analyze(raw, [], ORIGIN, ORIGIN + 10_000_000_000)


def test_a_fixed_schedule_exposes_delayed_arrivals_even_with_fast_calls() -> None:
    raw = report()
    for sample in raw["samples"]:
        sample["start_ns"] = str(int(sample["start_ns"]) + 500_000_000)
    with pytest.raises(AssertionError, match="scheduling"):
        analyze(raw, [], ORIGIN, ORIGIN + 10_000_000_000)


@pytest.mark.parametrize("field,value", [("complete", False), ("dropped_windows", 1)])
def test_lost_or_incomplete_telemetry_refuses_acceptance(field: str, value: Any) -> None:
    raw = report()
    raw["windows"][0][field] = value
    with pytest.raises(AssertionError, match="telemetry"):
        analyze(raw, [], ORIGIN, ORIGIN + 10_000_000_000)


def test_errors_must_be_timed_and_belong_to_the_cutover_window() -> None:
    raw = report(200)
    raw["errors"] = {"save_EngineError": 1}
    raw["failures"] = [{"op": "save", "error": "EngineError", "time_ns": str(ORIGIN + 1_000_000)}]
    with pytest.raises(AssertionError, match="outside"):
        analyze(raw, [], ORIGIN, ORIGIN + 20_000_000_000)
    result = analyze(
        raw,
        [{"action": "execute", "start_ns": str(ORIGIN), "end_ns": str(ORIGIN)}],
        ORIGIN,
        ORIGIN + 20_000_000_000,
    )
    assert result["errors"] == raw["errors"]


def test_an_error_counter_without_timing_cannot_be_ignored() -> None:
    raw = report()
    raw["errors"] = {"save_EngineError": 1}
    with pytest.raises(AssertionError, match="account"):
        analyze(raw, [], ORIGIN, ORIGIN + 10_000_000_000)


def test_oversized_process_memory_refuses_acceptance() -> None:
    raw = report()
    raw["max_rss_kib"] = 300 * 1024
    with pytest.raises(AssertionError, match="memory ceiling"):
        analyze(raw, [], ORIGIN, ORIGIN + 10_000_000_000)


def test_analysis_does_not_rewrite_worker_measurements() -> None:
    raw = report()
    before = deepcopy(raw)
    analyze(raw, [], ORIGIN, ORIGIN + 10_000_000_000)
    assert raw == before

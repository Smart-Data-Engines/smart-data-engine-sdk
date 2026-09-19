"""A fast benchmark result must account for work, time and its arrival model."""

from copy import deepcopy

import pytest
from qualification.operation_metrics import account_for_work, summarize


def test_batch_rows_and_calls_are_not_interchangeable() -> None:
    samples = [
        {"operation": "save_many", "duration_ns": n * 1_000_000, "rows": 100} for n in range(1, 101)
    ]
    original = deepcopy(samples)
    result = summarize(samples, elapsed_ns=6_000_000_000, expected_rows=10000)
    assert result["operations"]["save_many"]["calls"] == 100
    assert result["operations"]["save_many"]["rows"] == 10000
    assert result["operations"]["save_many"]["latency_ms"] == {"50": 50, "95": 95, "99": 99}
    assert result["arrival_model"] == "closed_loop"
    assert result["scheduled_latency_measured"] is False
    assert samples == original


@pytest.mark.parametrize("duration", [0, -1, True, 1.0, float("nan"), float("inf")])
def test_invalid_times_never_form_a_benchmark(duration: object) -> None:
    with pytest.raises(ValueError):
        summarize(
            [{"operation": "save", "duration_ns": duration, "rows": 1}],
            elapsed_ns=100,
            expected_rows=1,
        )


@pytest.mark.parametrize("rows", [0, -1, True, 1.0])
def test_empty_or_coerced_work_is_not_fast_work(rows: object) -> None:
    with pytest.raises(ValueError):
        summarize(
            [{"operation": "save_many", "duration_ns": 10, "rows": rows}],
            elapsed_ns=100,
            expected_rows=1,
        )


def test_missing_acknowledged_rows_refuse_measurement() -> None:
    with pytest.raises(ValueError, match="acknowledged"):
        summarize(
            [{"operation": "save_many", "duration_ns": 10, "rows": 99}],
            elapsed_ns=100,
            expected_rows=100,
        )


def test_impossible_enclosing_interval_is_refused() -> None:
    with pytest.raises(ValueError, match="interval"):
        summarize(
            [{"operation": "save", "duration_ns": 101, "rows": 1}], elapsed_ns=100, expected_rows=1
        )


def test_no_samples_cannot_mean_zero_latency() -> None:
    with pytest.raises(ValueError):
        summarize([], elapsed_ns=100, expected_rows=1)


@pytest.mark.parametrize("change", ["missing", "extra", "substitute"])
def test_skipped_or_replaced_read_work_is_not_a_faster_benchmark(change: str) -> None:
    samples = [
        {"operation": name, "duration_ns": 10, "rows": 1}
        for name in ("get", "scan", "count", "summarize")
    ]
    account_for_work(
        samples, mode="read", method="save_many", rows=100, batch_size=10, read_samples=1
    )
    if change == "missing":
        samples.pop()
    elif change == "extra":
        samples.append(dict(samples[0]))
    else:
        samples[-1]["operation"] = "get"
    with pytest.raises(ValueError, match="requested"):
        account_for_work(
            samples, mode="read", method="save_many", rows=100, batch_size=10, read_samples=1
        )


@pytest.mark.parametrize("change", ["method", "batch", "missing"])
def test_matching_row_total_does_not_allow_another_write_workload(change: str) -> None:
    samples = [
        {"operation": "save_many", "duration_ns": 10, "rows": size} for size in (10, 10, 10, 1)
    ]
    account_for_work(
        samples, mode="write", method="save_many", rows=31, batch_size=10, read_samples=1
    )
    if change == "method":
        samples[0]["operation"] = "save"
    elif change == "batch":
        samples[0]["rows"], samples[-1]["rows"] = 1, 10
    else:
        samples.pop()
    with pytest.raises(ValueError, match="requested"):
        account_for_work(
            samples, mode="write", method="save_many", rows=31, batch_size=10, read_samples=1
        )

"""The soak's summary on a run made up here: each criterion it judges, and a clean run passing.

The clean run is the control. Every other test changes one thing in it and expects exactly the
breach that change is, so a criterion that stops judging fails its test and nothing else.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from qualification import soak_traffic as traffic
from qualification.soak_summary import (
    HOUR_NS,
    MINUTE_NS,
    Process,
    Run,
    drift,
    fault_outcomes,
    hourly,
    load,
    resources,
    summarise,
    suspended_ns,
)

SECOND_NS = 1_000_000_000
ZERO = 1_000 * SECOND_NS
HOURS = 4
FAULT = {"kind": "crash", "engine": "postgres", "start": ZERO + 90 * MINUTE_NS}
FAULT["end"] = FAULT["start"] + 5 * SECOND_NS


def histogram(milliseconds: float, count: int) -> dict[int, int]:
    return {traffic.bucket(int(milliseconds * 1e6)): count}


def minutes(duration_ms: float = 2.0) -> list[dict[str, Any]]:
    out = []
    for index in range(HOURS * 60):
        start = ZERO + index * MINUTE_NS
        out.append(
            {
                "t": start,
                "end": start + MINUTE_NS,
                "ops": {
                    "write": {
                        "ok": 300,
                        "errors": {},
                        "skipped": 0,
                        "unavailable": 0,
                        "duration": histogram(duration_ms, 300),
                        "delay": histogram(0.1, 300),
                    }
                },
            }
        )
    return out


def samples(rss_kib: int = 50_000, grow_kib_per_hour: int = 0, fds: int = 12) -> list[Any]:
    out = []
    for index in range(HOURS * 60 + 1):
        t = ZERO + index * MINUTE_NS
        out.append(
            {
                "t": t,
                "boottime": t + 7 * SECOND_NS,
                "processes": {
                    "python-postgres": {
                        "rss_kib": rss_kib + grow_kib_per_hour * index // 60,
                        "fds": fds,
                        "threads": 2,
                    }
                },
            }
        )
    return out


def clean_run() -> Run:
    events = [
        {"t": FAULT["start"] + 2 * SECOND_NS, "error": "EngineError", "named": True, "op": "write",
         "duration_ns": 3_000_000},
        {"t": FAULT["end"] + 3 * SECOND_NS, "recovered": "write", "after_failures": 4},
    ]  # fmt: skip
    process = Process(
        name="python-postgres",
        language="python",
        engine="postgres",
        minutes=minutes(),
        events=events,
        done={"totals": {"write": 72_000}},
        exit_code=0,
        oracle={"passed": True, "breach_count": 0},
    )
    return Run(
        zero=ZERO,
        end=ZERO + HOURS * HOUR_NS,
        hours=HOURS,
        faults=[dict(FAULT)],
        samples=samples(),
        processes=[process],
    )


def test_a_clean_run_passes() -> None:
    result = summarise(clean_run())
    assert result["passed"], result["breaches"]
    assert result["monotonic_hours"] == HOURS
    [outcome] = result["fault_outcomes"]
    assert (outcome["first_error_s"], outcome["recovery_s"]) == (2.0, 3.0)
    assert outcome["all_named"]


def test_a_fault_nothing_failed_in_needs_no_recovery() -> None:
    run = clean_run()
    run.processes[0].events = []
    [outcome] = fault_outcomes(run)
    assert (outcome["errors"], outcome["recovery_s"]) == (0, 0.0)


def test_a_process_that_never_works_again_is_a_breach() -> None:
    run = clean_run()
    run.processes[0].events = run.processes[0].events[:1]
    result = summarise(run)
    assert result["breaches"] == ["python-postgres: crash of postgres (fault 0): recovery None s"]


def test_a_return_later_than_a_minute_is_a_breach() -> None:
    run = clean_run()
    run.processes[0].events[1]["t"] = FAULT["end"] + 61 * SECOND_NS
    assert summarise(run)["breaches"] == [
        "python-postgres: crash of postgres (fault 0): recovery 61.0 s"
    ]


def test_an_error_outside_the_hierarchy_is_a_breach() -> None:
    run = clean_run()
    run.processes[0].events[0]["named"] = False
    breaches = summarise(run)["breaches"]
    assert breaches == ["python-postgres: 1 errors outside the hierarchy"]


def test_a_failed_check_and_a_failed_oracle_are_breaches() -> None:
    run = clean_run()
    run.processes[0].events.append({"t": ZERO, "check_failed": "get", "number": 3, "what": "x"})
    run.processes[0].oracle = {"passed": False, "breach_count": 1}
    assert summarise(run)["breaches"] == [
        "python-postgres: 1 failed in-run checks",
        "python-postgres: the oracle did not pass",
    ]


def test_a_process_that_did_not_finish_is_a_breach() -> None:
    run = clean_run()
    run.processes[0].done = None
    run.processes[0].exit_code = -9
    assert summarise(run)["breaches"] == ["python-postgres: did not run to the end (exit -9)"]


def test_p99_drift_beyond_twice_the_second_hour_is_a_breach() -> None:
    run = clean_run()
    slow = minutes(duration_ms=5.0)
    run.processes[0].minutes = run.processes[0].minutes[:120] + slow[120:]
    rows = hourly(run, run.processes[0])
    [found] = drift(rows)
    assert found["late_hours"] == [2, 3] and not found["passed"]
    assert summarise(run)["breaches"] == [
        f"python-postgres: p99 of write drifted x{found['ratio']}"
    ]


def test_one_slow_hour_at_the_end_is_not_a_drift() -> None:
    """Somebody else's build in the last hour: a trend would show in the hour before it too."""
    run = clean_run()
    slow = minutes(duration_ms=5.0)
    run.processes[0].minutes = run.processes[0].minutes[:180] + slow[180:]
    [found] = drift(hourly(run, run.processes[0]))
    assert found["passed"], found
    assert summarise(run)["passed"]


def test_fault_minutes_are_kept_apart_from_quiet_ones() -> None:
    run = clean_run()
    rows = hourly(run, run.processes[0])
    fault_rows = [r for r in rows if r["state"] == "fault"]
    assert [r["hour"] for r in fault_rows] == [1]
    # The fault at 90 minutes and the two after it: three minutes of three hundred writes.
    assert fault_rows[0]["ok"] == 3 * 300


def test_rss_growth_beyond_its_bound_is_a_breach() -> None:
    run = clean_run()
    run.samples = samples(grow_kib_per_hour=20_000)
    used = resources(run, run.processes[0])
    assert used["rss"]["passed"] is False
    assert summarise(run)["breaches"] == ["python-postgres: RSS grew past its bound"]


def test_rss_within_its_bound_passes() -> None:
    run = clean_run()
    run.samples = samples(grow_kib_per_hour=2_000)
    assert resources(run, run.processes[0])["rss"]["passed"] is True


def test_descriptors_that_do_not_return_after_a_fault_are_a_breach() -> None:
    run = clean_run()
    leaked = copy.deepcopy(run.samples)
    for sample in leaked:
        if sample["t"] > FAULT["start"]:
            sample["processes"]["python-postgres"]["fds"] = 13
    run.samples = leaked
    assert summarise(run)["breaches"] == [
        "python-postgres: descriptors did not return after a fault"
    ]


def test_the_time_a_machine_slept_is_boottime_ahead_of_monotonic() -> None:
    run = clean_run()
    run.samples[-1]["boottime"] += 600 * SECOND_NS
    assert suspended_ns(run.samples) == 600 * SECOND_NS
    assert suspended_ns(run.samples[:1]) == 0


def test_a_percentile_is_the_upper_bound_of_its_bucket() -> None:
    values = {traffic.bucket(1_000_000): 98, traffic.bucket(50_000_000): 2}
    assert traffic.percentile(values, 0.5) == traffic.bucket_upper_ns(traffic.bucket(1_000_000))
    assert traffic.percentile(values, 0.99) == traffic.bucket_upper_ns(traffic.bucket(50_000_000))
    assert traffic.percentile({}, 0.5) is None
    for nanoseconds in (999, 1_000, 1_500_000, 3_000_000_000):
        assert (
            nanoseconds
            <= traffic.bucket_upper_ns(traffic.bucket(nanoseconds))
            <= (nanoseconds * 2 ** (1 / traffic.SUB_BUCKETS) + 1000)
        )


def test_an_error_belongs_to_one_fault_only() -> None:
    run = clean_run()
    second = {"kind": "reset", "engine": "postgres", "start": FAULT["start"] + 60 * SECOND_NS}
    second["end"] = second["start"]
    run.faults.append(second)
    run.processes[0].events = [
        {"t": second["start"] + SECOND_NS, "error": "EngineError", "named": True, "op": "get",
         "duration_ns": 1_000_000},
        {"t": second["start"] + 2 * SECOND_NS, "recovered": "get", "after_failures": 1},
    ]  # fmt: skip
    first, later = fault_outcomes(run)
    assert (first["errors"], first["recovery_s"]) == (0, 0.0)
    assert (later["errors"], later["recovery_s"]) == (1, 2.0)


def test_the_return_is_measured_after_the_last_error_of_the_fault() -> None:
    run = clean_run()
    start = FAULT["start"]
    run.faults[0]["end"] = start + 10 * SECOND_NS
    run.processes[0].events = [
        {"t": start + SECOND_NS, "error": "EngineError", "named": True, "op": "write",
         "duration_ns": 1_000_000},
        {"t": start + 5 * SECOND_NS, "recovered": "get", "after_failures": 1},
        {"t": start + 20 * SECOND_NS, "error": "EngineError", "named": True, "op": "write",
         "duration_ns": 1_000_000},
        {"t": start + 25 * SECOND_NS, "recovered": "write", "after_failures": 1},
    ]  # fmt: skip
    [outcome] = fault_outcomes(run)
    assert (outcome["errors"], outcome["recovery_s"]) == (2, 15.0)


def test_a_run_that_ended_before_its_hours_is_a_breach() -> None:
    run = clean_run()
    run.end = ZERO + 3 * HOUR_NS
    run.stopped = "less than 2147483648 bytes free on the run's disk"
    assert summarise(run)["breaches"] == [
        "the run ended after 3.000 of 4 hours: less than 2147483648 bytes free on the run's disk"
    ]


def test_descriptors_of_requests_in_flight_are_not_a_leak() -> None:
    """A process that opens a connection a request: a sample sees one in flight or none."""
    run = clean_run()
    noisy = copy.deepcopy(run.samples)
    for index, sample in enumerate(noisy):
        sample["processes"]["python-postgres"]["fds"] = 12 + 2 * (index % 2)
    run.samples = noisy
    assert resources(run, run.processes[0])["descriptors_passed"]
    assert summarise(run)["passed"]


def written(directory: Path, run: Run, *, ended: bool) -> Path:
    """The run as the coordinator leaves its directory: ``run.json``, with the end once the run has
    one, the faults, the samples and each process's events and minutes."""
    meta: dict[str, Any] = {
        "zero": run.zero,
        "hours": run.hours,
        "processes": [
            {"name": p.name, "language": p.language, "engine": p.engine} for p in run.processes
        ],
    }
    if ended:
        meta["end"] = run.end
    (directory / "run.json").write_text(json.dumps(meta))
    (directory / "faults.jsonl").write_text("".join(json.dumps(f) + "\n" for f in run.faults))
    (directory / "samples.jsonl").write_text("".join(json.dumps(s) + "\n" for s in run.samples))
    for process in run.processes:
        out = directory / "processes" / process.name
        out.mkdir(parents=True)
        (out / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in process.events))
        (out / "minutes.jsonl").write_text("".join(json.dumps(m) + "\n" for m in process.minutes))
    return directory


def test_a_run_still_going_is_read_to_its_last_sample(tmp_path: Path) -> None:
    """The coordinator writes the end when the run ends. Until then a summary of the run reaches
    as far as its last sample and counts the errors of the faults so far: with the start taken for
    the end, every fault's window closed before it opened, and counted none."""
    run = clean_run()
    run.samples = [sample for sample in run.samples if sample["t"] <= ZERO + 2 * HOUR_NS]
    loaded = load(written(tmp_path, run, ended=False))
    assert loaded.live
    assert loaded.end == ZERO + 2 * HOUR_NS
    [outcome] = fault_outcomes(loaded)
    assert (outcome["errors"], outcome["recovery_s"]) == (1, 3.0)
    result = summarise(loaded)
    assert result["live"]
    assert "the run has not ended: 2.000 of 4.0 hours so far" in result["breaches"]


def test_a_run_that_ended_is_read_to_its_end(tmp_path: Path) -> None:
    run = clean_run()
    loaded = load(written(tmp_path, run, ended=True))
    assert not loaded.live
    assert loaded.end == run.end
    [outcome] = fault_outcomes(loaded)
    assert (outcome["errors"], outcome["recovery_s"]) == (1, 3.0)
    assert not any("has not ended" in breach for breach in summarise(loaded)["breaches"])

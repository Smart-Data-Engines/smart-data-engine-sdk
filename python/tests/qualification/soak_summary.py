"""The soak's summary: each criterion of the soak's requirements, with its numbers and its verdict.

Everything here is a function of what the run left on disk - the fault log, the samples, each
process's minutes, events and final record, and the oracle's verdicts - so a summary can be
recomputed from a run directory at any time, and tested on records made up for the purpose.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import soak_traffic as traffic

HOUR_NS = 3_600_000_000_000
MINUTE_NS = 60_000_000_000
RECOVERY_MARGIN_NS = 120_000_000_000  # a fault's window: the fault and two minutes after it
RECOVERY_BOUND_S = 60.0
DRIFT_BOUND = 2.0
RSS_GROWTH_FRACTION = 0.10
RSS_GROWTH_KIB = 16 * 1024
MIN_SAMPLES = 100  # fewer operations in an hour than this gives a p99 without resolution


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # the last line of a process killed while writing it
    return records


@dataclass
class Process:
    """One application process's records."""

    name: str  # language/engine
    language: str
    engine: str
    minutes: list[dict[str, Any]]
    events: list[dict[str, Any]]
    done: dict[str, Any] | None
    exit_code: int | None
    oracle: dict[str, Any] | None = None


@dataclass
class Run:
    zero: int
    end: int
    hours: float
    faults: list[dict[str, Any]]
    samples: list[dict[str, Any]]
    processes: list[Process] = field(default_factory=list)
    stopped: str | None = None  # why the coordinator ended the run before its hours, if it did


def in_fault_window(faults: Iterable[Mapping[str, Any]], start: int, end: int) -> bool:
    """Whether [start, end) overlaps a fault or the two minutes after it."""
    return any(
        start < fault["end"] + RECOVERY_MARGIN_NS and end > fault["start"] for fault in faults
    )


# --- faults ---------------------------------------------------------------------------------------


def fault_outcomes(run: Run) -> list[dict[str, Any]]:
    """For each fault and each process of its engine: what the operations did, and the return.

    A fault's window runs from its start to two minutes after its end, and never past the start of
    the next fault: an error belongs to one fault only. The return is the first success after the
    last error of the window, measured from the end of the fault.
    """
    outcomes = []
    for index, fault in enumerate(run.faults):
        later = [f["start"] for f in run.faults[index + 1 :]]
        horizon = min([*later, run.end])
        closes = min(fault["end"] + RECOVERY_MARGIN_NS, horizon)
        for process in run.processes:
            if process.engine != fault["engine"]:
                continue
            within = [e for e in process.events if fault["start"] <= e["t"] < closes]
            errors = [e for e in within if "error" in e]
            slow = [e["duration_ns"] for e in within if "slow" in e or "error" in e]
            stuck = [
                e for e in process.events if "stuck" in e and fault["start"] <= e["t"] < horizon
            ]
            recovery_s: float | None
            if not errors:
                recovery_s = 0.0  # nothing failed: nothing to return from
            else:
                last_error = max(e["t"] for e in errors)
                recovered = [
                    e["t"]
                    for e in process.events
                    if "recovered" in e and last_error <= e["t"] < horizon
                ]
                # None: it failed and did not work again before the next fault.
                recovery_s = max(0.0, (min(recovered) - fault["end"]) / 1e9) if recovered else None
            outcomes.append(
                {
                    "fault": index,
                    "kind": fault["kind"],
                    "engine": fault["engine"],
                    "process": process.name,
                    "language": process.language,
                    "errors": len(errors),
                    "error_classes": sorted({e["error"] for e in errors}),
                    "all_named": all(e.get("named", False) for e in errors),
                    "first_error_s": (
                        round((min(e["t"] for e in errors) - fault["start"]) / 1e9, 3)
                        if errors
                        else None
                    ),
                    "longest_operation_s": round(max(slow) / 1e9, 3) if slow else None,
                    "stuck": len(stuck),
                    "recovery_s": None if recovery_s is None else round(recovery_s, 3),
                }
            )
    return outcomes


def fault_table(outcomes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Fault kind x engine x language: the range over the repetitions, not a mean."""

    def span(values: list[float]) -> list[float] | None:
        return [min(values), max(values)] if values else None

    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for outcome in outcomes:
        key = (outcome["kind"], outcome["engine"], outcome["language"])
        groups.setdefault(key, []).append(outcome)
    table = []
    for (kind, engine, language), rows in sorted(groups.items()):
        recoveries = [r["recovery_s"] for r in rows]
        table.append(
            {
                "kind": kind,
                "engine": engine,
                "language": language,
                "repetitions": len(rows),
                "errors": span([r["errors"] for r in rows]),
                "error_classes": sorted({c for r in rows for c in r["error_classes"]}),
                "first_error_s": span([r["first_error_s"] for r in rows if r["first_error_s"]]),
                "longest_operation_s": span(
                    [r["longest_operation_s"] for r in rows if r["longest_operation_s"] is not None]
                ),
                "recovery_s": span([r for r in recoveries if r is not None]),
                "never_recovered": sum(1 for r in recoveries if r is None),
                "all_named": all(r["all_named"] for r in rows),
                "stuck": sum(r["stuck"] for r in rows),
            }
        )
    return table


# --- percentiles ----------------------------------------------------------------------------------


def hourly(run: Run, process: Process) -> list[dict[str, Any]]:
    """Per hour and operation: p50, p99, p99.9 and the maximum, quiet minutes and fault minutes
    apart, of duration and of delay behind the schedule."""
    hours: dict[tuple[int, str, str], dict[str, Any]] = {}
    for minute in process.minutes:
        hour = (minute["t"] - run.zero) // HOUR_NS
        if hour < 0:
            continue
        state = "fault" if in_fault_window(run.faults, minute["t"], minute["end"]) else "quiet"
        for kind, op in minute["ops"].items():
            entry = hours.setdefault(
                (hour, state, kind),
                {"duration": {}, "delay": {}, "ok": 0, "errors": 0, "skipped": 0,
                 "unavailable": 0},
            )  # fmt: skip
            traffic.merge(entry["duration"], op["duration"])
            traffic.merge(entry["delay"], op["delay"])
            entry["ok"] += op["ok"]
            entry["errors"] += sum(op["errors"].values())
            entry["skipped"] += op["skipped"]
            entry["unavailable"] += op["unavailable"]
    rows = []
    for (hour, state, kind), entry in sorted(hours.items()):
        row: dict[str, Any] = {"hour": hour, "state": state, "op": kind}
        for measure in ("duration", "delay"):
            histogram = entry[measure]
            for label, fraction in (("p50", 0.5), ("p99", 0.99), ("p999", 0.999), ("max", 1.0)):
                value = traffic.percentile(histogram, fraction)
                row[f"{measure}_{label}_ms"] = None if value is None else round(value / 1e6, 3)
            row[f"{measure}_samples"] = sum(histogram.values())
        row.update({name: entry[name] for name in ("ok", "errors", "skipped", "unavailable")})
        rows.append(row)
    return rows


def drift(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """p99 of each operation in the last quiet hour against the second hour (index 1)."""
    quiet = [r for r in rows if r["state"] == "quiet" and r["duration_samples"] >= MIN_SAMPLES]
    found = []
    for kind in sorted({r["op"] for r in quiet}):
        hours = sorted((r for r in quiet if r["op"] == kind), key=lambda r: r["hour"])
        second = next((r for r in hours if r["hour"] == 1), None)
        last = hours[-1] if hours else None
        if second is None or last is None or last["hour"] <= 1:
            continue
        ratio = last["duration_p99_ms"] / second["duration_p99_ms"]
        found.append(
            {
                "op": kind,
                "second_hour_p99_ms": second["duration_p99_ms"],
                "last_hour": last["hour"],
                "last_hour_p99_ms": last["duration_p99_ms"],
                "ratio": round(ratio, 3),
                "passed": ratio <= DRIFT_BOUND,
            }
        )
    return found


# --- resources ------------------------------------------------------------------------------------


def resources(run: Run, process: Process) -> dict[str, Any]:
    """RSS in the second hour and the last quiet hour; descriptors before each fault and before
    the next one."""
    # From the schedule's zero: a sample taken while the process was still starting holds the
    # descriptors of a process that has not connected yet.
    series = [
        (s["t"], s["processes"][process.name])
        for s in run.samples
        if process.name in s.get("processes", {}) and s["t"] >= run.zero
    ]
    by_hour: dict[int, list[int]] = {}
    for t, values in series:
        if not in_fault_window(run.faults, t - MINUTE_NS, t):
            by_hour.setdefault((t - run.zero) // HOUR_NS, []).append(values["rss_kib"])
    rss: dict[str, Any] = {"hours": {str(h): statistics.median(v) for h, v in by_hour.items()}}
    full = [h for h in sorted(by_hour) if h >= 1]
    if len(full) >= 2:
        second, last = statistics.median(by_hour[1]), statistics.median(by_hour[full[-1]])
        bound = max(second * (1 + RSS_GROWTH_FRACTION), second + RSS_GROWTH_KIB)
        rss.update(
            {
                "second_hour_kib": second,
                "last_hour": full[-1],
                "last_hour_kib": last,
                "bound_kib": bound,
                "passed": last <= bound,
            }
        )
    else:
        rss["passed"] = None  # a run shorter than three hours has no trend to judge
    descriptors = []
    starts = [f["start"] for f in run.faults if f["engine"] == process.engine]
    for index, start in enumerate(starts):
        horizon = starts[index + 1] if index + 1 < len(starts) else run.end
        before = [v["fds"] for t, v in series if t <= start]
        settled = [v["fds"] for t, v in series if start < t <= horizon]
        if before and settled:
            descriptors.append(
                {"fault_start": start, "before": before[-1], "settled": settled[-1],
                 "passed": settled[-1] <= before[-1]}
            )  # fmt: skip
    peak = max((v["fds"] for _, v in series), default=None)
    return {
        "rss": rss,
        "descriptors": descriptors,
        "descriptors_passed": all(d["passed"] for d in descriptors),
        "peak_descriptors": peak,
        "peak_rss_kib": max((v["rss_kib"] for _, v in series), default=None),
        "threads": sorted({v["threads"] for _, v in series}),
    }


# --- the whole ------------------------------------------------------------------------------------


def summarise(run: Run) -> dict[str, Any]:
    outcomes = fault_outcomes(run)
    processes = {}
    breaches: list[str] = []
    for process in run.processes:
        unnamed = [e for e in process.events if "error" in e and not e.get("named", False)]
        checks = [e for e in process.events if "check_failed" in e]
        stuck = [e for e in process.events if "stuck" in e]
        rows = hourly(run, process)
        drifted = drift(rows)
        used = resources(run, process)
        processes[process.name] = {
            "exit_code": process.exit_code,
            "finished": process.done is not None,
            "totals": (process.done or {}).get("totals"),
            "unnamed_errors": len(unnamed),
            "failed_checks": len(checks),
            "stuck_operations": len(stuck),
            "oracle": process.oracle,
            "hourly": rows,
            "drift": drifted,
            "resources": used,
        }
        if process.done is None or process.exit_code not in (0, None):
            breaches.append(f"{process.name}: did not run to the end (exit {process.exit_code})")
        if unnamed:
            breaches.append(f"{process.name}: {len(unnamed)} errors outside the hierarchy")
        if checks:
            breaches.append(f"{process.name}: {len(checks)} failed in-run checks")
        if process.oracle is None or not process.oracle.get("passed"):
            breaches.append(f"{process.name}: the oracle did not pass")
        if used["rss"]["passed"] is False:
            breaches.append(f"{process.name}: RSS grew past its bound")
        if not used["descriptors_passed"]:
            breaches.append(f"{process.name}: descriptors did not return after a fault")
        breaches.extend(
            f"{process.name}: p99 of {d['op']} drifted x{d['ratio']}"
            for d in drifted
            if not d["passed"]
        )
    late = [o for o in outcomes if o["recovery_s"] is None or o["recovery_s"] > RECOVERY_BOUND_S]
    breaches.extend(
        f"{o['process']}: {o['kind']} of {o['engine']} (fault {o['fault']}): "
        f"recovery {o['recovery_s']} s"
        for o in late
    )
    ran_ns = run.end - run.zero
    if ran_ns < run.hours * HOUR_NS - MINUTE_NS:
        breaches.append(
            f"the run ended after {ran_ns / HOUR_NS:.3f} of {run.hours} hours"
            + (f": {run.stopped}" if run.stopped else "")
        )
    suspended = suspended_ns(run.samples)
    return {
        "hours": run.hours,
        "monotonic_hours": round((run.end - run.zero) / HOUR_NS, 3),
        "suspended_s": round(suspended / 1e9, 3),
        "faults": len(run.faults),
        "fault_outcomes": outcomes,
        "fault_table": fault_table(outcomes),
        "processes": processes,
        "breaches": breaches,
        "passed": not breaches,
    }


def suspended_ns(samples: Sequence[Mapping[str, Any]]) -> int:
    """Time the machine slept: how far CLOCK_BOOTTIME ran ahead of CLOCK_MONOTONIC."""
    if len(samples) < 2:
        return 0
    first, last = samples[0], samples[-1]
    return max(0, int(last["boottime"] - first["boottime"]) - int(last["t"] - first["t"]))


def load(directory: Path) -> Run:
    """A run from its directory, as the coordinator left it."""
    meta = json.loads((directory / "run.json").read_text())
    run = Run(
        zero=int(meta["zero"]),
        end=int(meta.get("end", meta["zero"])),
        hours=float(meta["hours"]),
        faults=read_jsonl(directory / "faults.jsonl"),
        samples=read_jsonl(directory / "samples.jsonl"),
        stopped=meta.get("stopped"),
    )
    oracle = (
        json.loads((directory / "oracle.json").read_text())
        if (directory / "oracle.json").exists()
        else {}
    )
    for entry in meta["processes"]:
        out = directory / "processes" / entry["name"]
        done_path = out / "done.json"
        run.processes.append(
            Process(
                name=entry["name"],
                language=entry["language"],
                engine=entry["engine"],
                minutes=read_jsonl(out / "minutes.jsonl"),
                events=read_jsonl(out / "events.jsonl"),
                done=json.loads(done_path.read_text()) if done_path.exists() else None,
                exit_code=entry.get("exit_code"),
                oracle=oracle.get(entry["name"]),
            )
        )
    return run

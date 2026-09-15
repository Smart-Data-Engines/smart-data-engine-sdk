"""Verify completed local Weather runs against the exact active source, without exposing rows."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sde
from sde.generation import check_map_project

from .model import GENERATOR_ID, model, reading
from .project import DemoRefused, config, credentials, decode, engine, payload, public_keys

MAX_RUNS = 32
MAX_RUN_ROWS = 10_000
MAX_TOTAL_ROWS = 100_000
PAGE_ROWS = 1000


@dataclass(frozen=True)
class CompletedRun:
    """A validated local report descriptor; expected values are regenerated only in this process."""

    run_id: str
    language: str
    expected_rows: int
    report_sha256: str


@contextmanager
def _refusals() -> Iterator[None]:
    try:
        yield
    except DemoRefused:
        raise
    except Exception:
        # Driver/decoder messages can contain credentials or values returned by an engine.
        raise DemoRefused(
            "Local run verification did not complete; preserve the reports and inspect locally."
        ) from None


def _selected(run_ids: Sequence[str]) -> tuple[str, ...]:
    if (
        isinstance(run_ids, (str, bytes, bytearray))
        or not isinstance(run_ids, Sequence)
        or not 1 <= len(run_ids) <= MAX_RUNS
    ):
        raise DemoRefused("Select between 1 and 32 completed run IDs.")
    if any(
        not isinstance(identity, str) or re.fullmatch(r"[0-9a-f]{32}", identity) is None
        for identity in run_ids
    ):
        raise DemoRefused("Each run ID must be its original lowercase hexadecimal identifier.")
    if len(set(run_ids)) != len(run_ids):
        raise DemoRefused("Select each run ID only once.")
    return tuple(run_ids)


def _counter(value: object) -> int:
    if type(value) is not int or value < 0:
        raise DemoRefused("Completed run counters must be nonnegative integers, without coercion.")
    return value


def _load_completed(
    root: Path, run_ids: Sequence[str], *, project_id: str, model_version: str
) -> tuple[CompletedRun, ...]:
    result = []
    total = 0
    for identity in _selected(run_ids):
        data = payload(root / "runs" / identity / "report.json")
        report = decode(data)
        if (
            type(report.get("protocol")) is not int
            or report["protocol"] != 2
            or report.get("generator_id") != GENERATOR_ID
        ):
            raise DemoRefused("Verification requires report protocol 2 and its known generator ID.")
        if (
            report.get("run_id") != identity
            or report.get("project_id") != project_id
            or report.get("model_version") != model_version
            or report.get("language") not in ("python", "typescript")
        ):
            raise DemoRefused("A run report belongs to another run, project, model or language.")
        if (
            report.get("status") != "complete"
            or "pending" not in report
            or report["pending"] is not None
            or report.get("cleanup_failed", False) is not False
            or report.get("failure") is not None
        ):
            raise DemoRefused(
                "Only completed runs without pending work or cleanup failure can be verified."
            )
        acknowledged, uncertain, verified = (
            _counter(report.get(name))
            for name in ("acknowledged_rows", "verified_after_uncertain_rows", "verified_rows")
        )
        if acknowledged + uncertain != verified:
            raise DemoRefused(
                "A completed run's acknowledged and independently verified totals disagree."
            )
        if verified > MAX_RUN_ROWS:
            raise DemoRefused("Verification permits at most 10000 expected rows per run.")
        total += verified
        if total > MAX_TOTAL_ROWS:
            raise DemoRefused("Verification permits at most 100000 expected rows per invocation.")
        result.append(
            CompletedRun(identity, report["language"], verified, hashlib.sha256(data).hexdigest())
        )
    return tuple(result)


def load_completed_runs(root: Path, run_ids: Sequence[str]) -> tuple[CompletedRun, ...]:
    """Load bounded completed reports locally; query checks can sum each expected_rows value."""
    with _refusals():
        settings = config(root)
        return _load_completed(
            root, run_ids, project_id=settings["project_id"], model_version=model().version
        )


def _active_map(
    root: Path, settings: Mapping[str, Any], logical: sde.LogicalModel
) -> sde.PlacementMap:
    if (root / "reset-request.json").exists():
        raise DemoRefused("Reset was requested; run verification has stopped.")
    document = decode(payload(root / "state" / "active-map.json"))
    placement = sde.load_map(
        document,
        model=logical,
        public_key=public_keys(settings["public_keys"]),
        require_signature=True,
    )
    if placement.contract != 4:
        raise DemoRefused("Run verification requires an active generation-bearing Weather map.")
    check_map_project(placement, settings["project_id"])
    return placement


def _map_identity(placement: sde.PlacementMap) -> tuple[str | None, str, int, str | None]:
    # A trusted re-signature or JSON formatting change does not change the instructions.
    return (
        placement.project_id,
        placement.model_version,
        placement.map_version,
        placement.fingerprint,
    )


def _verify_one(client: sde.Session, completed: CompletedRun) -> None:
    station = reading(completed.run_id, 0, 1)["station"]
    seen = 0
    after: Mapping[str, Any] | None = None
    while True:
        page = client.scan(
            "WeatherReading",
            where={"station": station},
            after=after,
            limit=min(PAGE_ROWS, completed.expected_rows - seen + 1),
            fresh=True,
        )
        for actual in page.rows:
            if seen >= completed.expected_rows:
                raise DemoRefused("A completed run's namespace contains additional source rows.")
            expected = reading(completed.run_id, 0, seen + 1)
            if actual != expected:
                raise DemoRefused("A completed run's exact values do not match the active source.")
            seen += 1
        if page.next_after is None:
            break
        if not page.rows or dict(page.next_after) != {
            key: page.rows[-1][key] for key in ("station", "at")
        }:
            raise DemoRefused("The source returned an inconsistent page cursor.")
        after = page.next_after
    if seen != completed.expected_rows:
        raise DemoRefused("A completed run is missing expected source rows.")


def verify_runs(root: Path, run_ids: Sequence[str]) -> dict[str, Any]:
    """Read all expected values and reject extras in each run's namespace, using runtime rights."""
    with _refusals():
        settings, logical = config(root), model()
        completed = _load_completed(
            root, run_ids, project_id=settings["project_id"], model_version=logical.version
        )
        placement = _active_map(root, settings, logical)
        identity = _map_identity(placement)
        needed = {
            material.engine for group in placement.groups.values() for material in group.all()
        }
        if needed - settings["engines"].keys():
            raise DemoRefused("An active materialization has no local runtime binding.")
        dsns = credentials(root, "runtime", settings["engines"])
        factories = {
            name: (
                lambda dialect=settings["engines"][name]["dialect"], dsn=dsns[name]: engine(
                    dialect, dsn
                )
            )
            for name in sorted(needed)
        }
        with sde.Session.connect(
            logical, placement, factories, project_id=settings["project_id"]
        ) as client:
            for completed_run in completed:
                _verify_one(client, completed_run)
        if _map_identity(_active_map(root, settings, logical)) != identity:
            raise DemoRefused(
                "The active map changed during run verification; repeat against the current source."
            )
        for completed_run in completed:
            data = payload(root / "runs" / completed_run.run_id / "report.json")
            if hashlib.sha256(data).hexdigest() != completed_run.report_sha256:
                raise DemoRefused("A completed report changed during run verification.")
        return {
            "protocol": 1,
            "status": "verified",
            "project_id": settings["project_id"],
            "model_version": logical.version,
            "generator_id": GENERATOR_ID,
            "map_version": placement.map_version,
            "map_fingerprint": placement.fingerprint,
            "verified_rows": sum(item.expected_rows for item in completed),
            "runs": [
                {
                    "run_id": item.run_id,
                    "language": item.language,
                    "verified_rows": item.expected_rows,
                    "report_sha256": item.report_sha256,
                }
                for item in completed
            ],
        }

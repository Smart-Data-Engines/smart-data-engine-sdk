"""A bounded logical workload with explicit uncertain writes and value-free reports."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

import sde

from .model import (
    BASE_TIME,
    CELSIUS_BASE_CENTS,
    CELSIUS_MODULUS,
    GENERATOR_ID,
    HUMIDITY_BASE,
    HUMIDITY_MODULUS,
    WORKLOADS,
    model,
    reading,
)
from .project import DemoRefused, config, credentials, engine, public_keys, read, write

T = TypeVar("T")
FLEET_PAGE = 100
MAX_RUN_ROWS = 10000
"""The fleet checks one cross-station page exactly; its count and sum cover the whole window."""
ALERT_HUMIDITY = 95
"""The alert threshold. Humidity is 30 + sequence % 70, so a reading alerts when sequence % 70 is
65 or more - five readings in seventy, known exactly from the sequence number."""
ALERT_PAGE = 100


def fleet_runs(root: Path, *, project_id: str) -> dict[str, int]:
    """Every earlier run of this project, by run ID, with the rows it verified.

    Fleet analytics reads every station's rows in one time window, and every run in a directory
    writes the same timeline, so the exact answer is a sum over the runs. It is known exactly:
    generated values depend only on the sequence number, and each run's local report says how many
    rows it verified. A run that did not complete - interrupted, or with an uncertain batch - makes
    that sum unknowable, so the workload refuses instead of comparing a read with a guess.
    """
    runs: dict[str, int] = {}
    for path in sorted((root / "runs").glob("*/report.json")):
        report = read(path)
        if report.get("project_id") != project_id:
            continue
        identity = report.get("run_id")
        if (
            not isinstance(identity, str)
            or re.fullmatch(r"[0-9a-f]{32}", identity) is None
            or path.parent.name != identity
            or report.get("status") != "complete"
            or report.get("pending", False) is not None
            or report.get("generator_id") != GENERATOR_ID
            or type(report.get("verified_rows")) is not int
            or not 0 <= report["verified_rows"] <= MAX_RUN_ROWS
        ):
            raise DemoRefused(
                "Fleet analytics reads every run's rows, so every earlier run of this project must "
                "be complete; inspect the unfinished run first."
            )
        runs[identity] = report["verified_rows"]
    return runs


def fleet_expected(
    runs: Mapping[str, int], through: int, limit: int
) -> tuple[list[dict[str, Any]], int, Decimal]:
    """The first page, row count and celsius total of the window holding sequences 1..through.

    A page is in key order, station then time, and a station is its run's namespace, so the page
    walks runs in the order of their stations and each run's rows in sequence order.
    """
    page: list[dict[str, Any]] = []
    total, cents = 0, 0
    for identity in sorted(runs, key=lambda item: reading(item, 0, 1)["station"]):
        rows = min(through, runs[identity])
        total += rows
        for sequence in range(1, rows + 1):
            cents += CELSIUS_BASE_CENTS + sequence % CELSIUS_MODULUS
            if len(page) < limit:
                page.append(reading(identity, 0, sequence))
    return page, total, Decimal(f"{cents // 100}.{cents % 100:02d}")


def alert_expected(
    run_id: str, through: int, limit: int
) -> tuple[list[dict[str, Any]], int, Decimal | None]:
    """The first page, count and celsius total of this run's alerts among sequences 1..through.

    One station, so key order - station, then time - is sequence order. With no alert yet the total
    is None, as the library reports a summary of no values on every engine.
    """
    page: list[dict[str, Any]] = []
    total, cents = 0, 0
    for sequence in range(1, through + 1):
        if HUMIDITY_BASE + sequence % HUMIDITY_MODULUS < ALERT_HUMIDITY:
            continue
        total += 1
        cents += CELSIUS_BASE_CENTS + sequence % CELSIUS_MODULUS
        if len(page) < limit:
            page.append(reading(run_id, 0, sequence))
    return page, total, Decimal(f"{cents // 100}.{cents % 100:02d}") if total else None


def limits(iterations: int, batch_size: int, interval_ms: int, recovery_ms: int) -> None:
    if (
        any(type(value) is not int for value in (iterations, batch_size, interval_ms, recovery_ms))
        or not 1 <= iterations <= 1000
        or not 1 <= batch_size <= 1000
        or iterations * batch_size > MAX_RUN_ROWS
        or not 0 <= interval_ms <= 1000
        or not 0 <= recovery_ms <= 30000
    ):
        raise DemoRefused(
            "Use 1-1000 iterations/batch, at most 10000 rows, interval 0-1000 ms "
            "and recovery 0-30000 ms."
        )


def run(
    root: Path,
    *,
    iterations: int = 10,
    batch_size: int = 10,
    interval_ms: int = 100,
    recovery_ms: int = 10000,
    workload: str = "mixed",
) -> dict[str, Any]:
    limits(iterations, batch_size, interval_ms, recovery_ms)
    if workload not in WORKLOADS:
        raise DemoRefused("Choose the mixed, point, analytics, fleet or alerts workload.")
    settings = config(root)
    # Read before this run's own report exists; a run that starts later is not in the window.
    earlier = fleet_runs(root, project_id=settings["project_id"]) if workload == "fleet" else {}
    logical, keys = model(), public_keys(settings["public_keys"])
    dsns = credentials(root, "runtime", settings["engines"])
    factories = {
        name: (lambda dialect=binding["dialect"], dsn=dsns[name]: engine(dialect, dsn))
        for name, binding in settings["engines"].items()
    }
    run_id = uuid4().hex
    directory = root / "runs" / run_id
    recorder = sde.Recorder(logical.version)
    session: sde.Session | None = None
    fingerprint: str | None = None
    report: dict[str, Any] = {
        "protocol": 2,
        "generator_id": GENERATOR_ID,
        "run_id": run_id,
        "language": "python",
        "status": "running",
        "workload": workload,
        "sdk_version": sde.__version__,
        "sdk_module": sde.__file__,
        "project_id": settings["project_id"],
        "model_version": logical.version,
        "acknowledged_rows": 0,
        "verified_after_uncertain_rows": 0,
        "verified_rows": 0,
        "pending": None,
        "map_versions": [],
        "read_retries": 0,
    }

    def checkpoint() -> None:
        write(directory / "report.json", report)

    def opened(*, fresh: bool = False) -> sde.Session:
        nonlocal session, fingerprint
        if (root / "reset-request.json").exists():
            raise DemoRefused("Reset was requested; this workload has stopped.")
        placement = sde.load_local_map(
            root / "state", model=logical, project_id=settings["project_id"], public_key=keys
        )
        if fresh or session is None or placement.fingerprint != fingerprint:
            if session is not None:
                session.close()
                session = None
            required = {
                material.engine for group in placement.groups.values() for material in group.all()
            }
            if required - factories.keys():
                raise DemoRefused("An active materialization has no local binding.")
            active_factories = {name: factories[name] for name in sorted(required)}
            session = sde.Session.connect(
                logical,
                placement,
                active_factories,
                recorder=recorder,
                project_id=settings["project_id"],
            )
            fingerprint = placement.fingerprint
            if placement.map_version not in report["map_versions"]:
                report["map_versions"].append(placement.map_version)
        return session

    def read_retry(action: Callable[[sde.Session], T]) -> T:
        deadline = time.monotonic() + recovery_ms / 1000
        fresh = False
        while True:
            try:
                return action(opened(fresh=fresh))
            except (sde.EngineError, sde.MapRolledBack, sde.MigrationRefused):
                if time.monotonic() >= deadline:
                    raise
                report["read_retries"] += 1
                fresh = True
                time.sleep(0.05)

    def same(actual: Mapping[str, Any] | None, expected: dict[str, Any]) -> None:
        if actual != expected:
            raise DemoRefused("A logical read did not match this run's synthetic input.")

    def check_rows(client: sde.Session, rows: list[dict[str, Any]]) -> bool:
        for row in rows:
            actual = client.get(
                "WeatherReading", {key: row[key] for key in ("station", "at")}, fresh=True
            )
            if actual is None:
                return False
            same(actual, row)
        return True

    def point(current: sde.Session, row: dict[str, Any]) -> Any:
        return current.get("WeatherReading", {key: row[key] for key in ("station", "at")})

    def counted(
        current: sde.Session, where: dict[str, Any], bounds: sde.Range | None = None
    ) -> int:
        return current.count("WeatherReading", where=where, bounds=bounds)

    def summarized(
        current: sde.Session, where: dict[str, Any], bounds: sde.Range | None = None
    ) -> Any:
        return current.summarize("WeatherReading", "celsius", where=where, bounds=bounds)

    checkpoint()
    began = time.monotonic_ns()
    try:
        for iteration in range(iterations):
            first = iteration * batch_size + 1
            rows = [reading(run_id, 0, number) for number in range(first, first + batch_size)]
            # Opening/refresh errors happen before the write intent and may be retried safely.
            client = read_retry(lambda current: current)
            report["pending"] = {"first": first, "count": batch_size}
            checkpoint()
            try:
                client.save_many("WeatherReading", rows)
            except sde.EngineError:
                # No replay. A visible exact batch establishes the result; an absent or partial
                # batch does not prove rollback. Leave its durable range pending on refusal.
                deadline = time.monotonic() + recovery_ms / 1000
                while True:
                    try:
                        if check_rows(opened(fresh=True), rows):
                            report["verified_after_uncertain_rows"] += batch_size
                            break
                    except (sde.EngineError, sde.MapRolledBack, sde.MigrationRefused):
                        pass
                    if time.monotonic() >= deadline:
                        raise DemoRefused(
                            "Write outcome is uncertain. Inspect this run's pending "
                            "range locally; the starter did not replay it."
                        ) from None
                    time.sleep(0.05)
            else:
                report["acknowledged_rows"] += batch_size
            report["pending"] = None
            checkpoint()
            last = rows[-1]
            repeats = 20 if workload == "point" else 1
            for _ in range(repeats):
                same(read_retry(partial(point, row=last)), last)
            count = first + batch_size - 1
            where = {"station": last["station"]}
            if workload == "fleet":
                # Every station, one time window: the traffic a time-first layout is for. The
                # expected answer is exact, from this directory's run reports (``fleet_runs``).
                fleet_page, fleet_rows, fleet_celsius = fleet_expected(
                    {**earlier, run_id: count}, count, FLEET_PAGE
                )
                bounds = sde.Range(
                    "at", low=reading(run_id, 0, 1)["at"], high=reading(run_id, 0, count + 1)["at"]
                )

                def check_fleet_page(
                    current: sde.Session,
                    bounds: sde.Range = bounds,
                    expected: list[dict[str, Any]] = fleet_page,
                ) -> None:
                    page = current.scan("WeatherReading", bounds=bounds, limit=len(expected))
                    if list(page.rows) != expected:
                        raise DemoRefused("The fleet page did not match this directory's runs.")

                read_retry(check_fleet_page)
                if read_retry(partial(counted, where={}, bounds=bounds)) != fleet_rows:
                    raise DemoRefused("The fleet count did not match this directory's runs.")
                summary = read_retry(partial(summarized, where={}, bounds=bounds))
                if summary.count != fleet_rows or summary.total != fleet_celsius:
                    raise DemoRefused(
                        "The exact fleet summary did not match this directory's runs."
                    )
            elif workload == "alerts":
                # One station's readings at or above the alert threshold: a range on humidity, a
                # field outside the key, which the key cannot serve and an index can. The expected
                # answer is exact, from the generator (``alert_expected``).
                alert_page, alert_rows, alert_celsius = alert_expected(run_id, count, ALERT_PAGE)
                alert_bounds = sde.Range("humidity", low=ALERT_HUMIDITY)

                def check_alert_page(
                    current: sde.Session,
                    where: dict[str, Any] = where,
                    bounds: sde.Range = alert_bounds,
                    expected: list[dict[str, Any]] = alert_page,
                ) -> None:
                    page = current.scan(
                        "WeatherReading", where=where, bounds=bounds, limit=ALERT_PAGE
                    )
                    if list(page.rows) != expected:
                        raise DemoRefused("The alert page did not match this run.")

                read_retry(check_alert_page)
                if read_retry(partial(counted, where=where, bounds=alert_bounds)) != alert_rows:
                    raise DemoRefused("The alert count did not match this run.")
                summary = read_retry(partial(summarized, where=where, bounds=alert_bounds))
                if summary.count != alert_rows or summary.total != alert_celsius:
                    raise DemoRefused("The exact alert summary did not match this run.")
            elif workload != "point" or iteration == iterations - 1:

                def check_page(
                    current: sde.Session, where: dict[str, Any] = where, count: int = count
                ) -> None:
                    page = current.scan(
                        "WeatherReading",
                        where=where,
                        bounds=sde.Range("at", low=BASE_TIME),
                        limit=min(count, 1000),
                    )
                    expected = [reading(run_id, 0, index) for index in range(1, len(page.rows) + 1)]
                    if list(page.rows) != expected or len(page.rows) != min(count, 1000):
                        raise DemoRefused("The bounded logical page did not match this run.")

                read_retry(check_page)
                if read_retry(partial(counted, where=where)) != count:
                    raise DemoRefused("The logical count did not match this run.")
                summary = read_retry(partial(summarized, where=where))
                cents = sum(1525 + index % 1000 for index in range(1, count + 1))
                total = Decimal(f"{cents // 100}.{cents % 100:02d}")
                if summary.count != count or summary.total != total:
                    raise DemoRefused("The exact logical summary did not match this run.")
            report["verified_rows"] = count
            checkpoint()
            if interval_ms and iteration + 1 < iterations:
                time.sleep(interval_ms / 1000)
        report["status"] = "complete"
    except BaseException as exc:
        report["status"] = "incomplete"
        report["failure"] = type(exc).__name__
        raise
    finally:
        report["elapsed_ns"] = str(time.monotonic_ns() - began)
        try:
            if session is not None:
                session.close()
        except BaseException:
            report["status"] = "incomplete"
            report["cleanup_failed"] = True
            raise
        finally:
            window = recorder.roll()
            if window is not None:
                write(directory / "window.json", window.as_record(logical))
                recorder.acknowledge(1)
            checkpoint()
    return report

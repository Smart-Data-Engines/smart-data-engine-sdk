"""One soak application process: Python, one engine, on an installed artifact.

It runs the cycle of ``soak_traffic.CYCLES`` on a fixed schedule until the coordinator writes the
stop file or sends SIGTERM, and keeps what happened in two files, appended and flushed as it goes,
so a process killed in the middle leaves a readable trace:

- ``minutes.jsonl``: per minute and operation, successes, errors by class, slots skipped or
  without a connection, and histograms of duration and of delay behind the schedule; the highest
  write number acknowledged and attempted;
- ``events.jsonl``: every error, every recovery (the first success after errors), every write
  number that failed, every operation still running after 60 s, every failed in-run check.

After an error of the library's hierarchy it does what the documentation says: ``close()``, then
``connect()``, then a new session, at the next slot of the schedule. The operation that failed is
not repeated. An error outside the hierarchy is recorded as such: the soak counts it as a breach.

No row value leaves the process; only numbers, counts and error classes.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import signal
import sys
import threading
import time
import traceback
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

import sde
from qualification import soak_traffic as traffic
from sde.testing.loader import model_from_neutral

STUCK_REPORTS_S = (60, 300, 900, 3600)
# The reads look at the newest writes: 150 orders (50 an account) or 10 batches of readings.
WINDOW = {"postgres": 150, "clickhouse": 10}


class Minute:
    """The counters of one minute, per operation."""

    def __init__(self, started_ns: int) -> None:
        self.started_ns = started_ns
        self.ops: dict[str, dict[str, Any]] = {}

    def op(self, kind: str) -> dict[str, Any]:
        return self.ops.setdefault(
            kind,
            {"ok": 0, "errors": Counter(), "skipped": 0, "unavailable": 0, "duration": {},
             "delay": {}},
        )  # fmt: skip

    def record(self, kind: str, duration_ns: int, delay_ns: int, error: str | None) -> None:
        entry = self.op(kind)
        if error is None:
            entry["ok"] += 1
        else:
            entry["errors"][error] += 1
        b = traffic.bucket(duration_ns)
        entry["duration"][b] = entry["duration"].get(b, 0) + 1
        d = traffic.bucket(max(0, delay_ns))
        entry["delay"][d] = entry["delay"].get(d, 0) + 1


class Worker:
    def __init__(self, soak: dict[str, Any]) -> None:
        self.soak = soak
        self.language = "python"
        self.dialect: str = soak["dialect"]
        project = json.loads(Path(soak["project_config"]).read_bytes())
        self.project = project
        self.model = model_from_neutral(project["model"])
        self.keys = {
            name: base64.b64decode(value) for name, value in project["public_keys"].items()
        }
        binding = project["engines"][self.dialect]
        self.dsn = os.environ[binding["runtime_dsn_envs"][0]]
        self.out = Path(soak["output"])
        self.minutes: TextIO = (self.out / "minutes.jsonl").open("a", encoding="utf-8")
        self.events: TextIO = (self.out / "events.jsonl").open("a", encoding="utf-8")
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.engine: Any = None
        self.session: sde.Session | None = None
        self.connected = False
        self.cycle = traffic.CYCLES[self.dialect]
        self.period_ns = int(soak["period_ms"]) * 1_000_000
        self.attempted = -1  # the highest write number attempted
        self.acknowledged: list[int] = []  # numbers acknowledged since the last minute
        self.highest_acked = -1
        self.failed: set[int] = set()
        self.failing_since: int | None = None
        self.failures_since_success = 0
        self.running: tuple[str, int] | None = None  # (operation, started) for the watchdog
        self.reported_stuck: set[tuple[int, int]] = set()
        self.totals: Counter[str] = Counter()

    # --- output ----------------------------------------------------------------------------

    def event(self, record: dict[str, Any]) -> None:
        record = {"t": time.monotonic_ns(), "wall": datetime.now(UTC).isoformat(), **record}
        with self.lock:
            self.events.write(json.dumps(record, separators=(",", ":")) + "\n")
            self.events.flush()

    def flush_minute(self, minute: Minute) -> None:
        ops = {
            kind: {**entry, "errors": dict(entry["errors"])} for kind, entry in minute.ops.items()
        }
        record = {
            "t": minute.started_ns,
            "end": time.monotonic_ns(),
            "wall": datetime.now(UTC).isoformat(),
            "ops": ops,
            "attempted": self.attempted,
            "highest_acked": self.highest_acked,
            "connected": self.connected,
        }
        with self.lock:
            self.minutes.write(json.dumps(record, separators=(",", ":")) + "\n")
            self.minutes.flush()

    # --- the engine --------------------------------------------------------------------------

    def connect(self) -> None:
        """The documented recovery: ``close()``, then ``connect()`` of the same engine."""
        if self.engine is None:
            if self.dialect == "postgres":
                from sde.engines.postgres import PostgresEngine

                self.engine = PostgresEngine(self.dsn)
            else:
                from sde.engines.clickhouse import ClickHouseEngine

                self.engine = ClickHouseEngine(self.dsn)
        else:
            self.engine.close()
        self.engine.connect()
        placement = sde.load_local_map(
            Path(self.soak["project_dir"]),
            model=self.model,
            project_id=self.project["project_id"],
            public_key=self.keys,
        )
        self.session = sde.Session(
            self.model,
            placement,
            {self.dialect: self.engine},
            project_id=self.project["project_id"],
        )
        self.connected = True

    # --- operations --------------------------------------------------------------------------

    def write(self) -> int:
        number = self.attempted + 1
        self.attempted = number
        assert self.session is not None
        if self.dialect == "postgres":
            if traffic.is_transaction(number):
                with self.session.transaction("Order", "Fill"):
                    self.session.save("Order", traffic.order(self.language, number))
                    for fill in traffic.fills(self.language, number):
                        self.session.save("Fill", fill)
            else:
                self.session.save("Order", traffic.order(self.language, number))
        else:
            self.session.save_many("WeatherReading", traffic.batch(self.language, number))
        return number

    def pick(self, slot: int) -> int | None:
        """The write number a read asks for: the newest acknowledged, or an earlier one."""
        if self.highest_acked < 0:
            return None
        candidate = (
            self.highest_acked if slot % 2 == 0 else (slot * 7919) % (self.highest_acked + 1)
        )
        while candidate >= 0 and candidate in self.failed:
            candidate -= 1
        return candidate if candidate >= 0 else None

    def get(self, slot: int) -> None:
        number = self.pick(slot)
        if number is None:
            return
        assert self.session is not None
        if self.dialect == "postgres":
            expected = traffic.order(self.language, number)
            found = self.session.get(
                "Order", {"account": expected["account"], "placed_at": expected["placed_at"]}
            )
        else:
            expected = traffic.reading(self.language, number * traffic.BATCH)
            found = self.session.get(
                "WeatherReading", {"station": expected["station"], "at": expected["at"]}
            )
        if found != expected:
            self.check_failed("get", number, "the row read is not the row written")

    def window(self) -> tuple[int, int] | None:
        """The newest write numbers, as the range scan and count read them."""
        if self.highest_acked < 0:
            return None
        return max(0, self.highest_acked - WINDOW[self.dialect] + 1), self.highest_acked

    def scan(self, slot: int) -> None:
        bounds = self.window()
        if bounds is None:
            return
        low, high = bounds
        assert self.session is not None
        if self.dialect == "postgres":
            name = traffic.account(self.language, slot)
            page = self.session.scan(
                "Order",
                where={"account": name},
                bounds=sde.Range(
                    "placed_at",
                    traffic.order(self.language, low)["placed_at"],
                    traffic.order(self.language, high)["placed_at"] + timedelta(microseconds=1),
                ),
                limit=200,
            )
            seen = set()
            for row in page.rows:
                number = traffic.order_number(row)
                if row != traffic.order(self.language, number):
                    self.check_failed("scan", number, "a scanned row is not the row written")
                seen.add(number)
            expected = {
                n
                for n in range(low, high + 1)
                if n not in self.failed and traffic.account(self.language, n) == name
            }
        else:
            first, last = low * traffic.BATCH, high * traffic.BATCH + traffic.BATCH - 1
            page = self.session.scan(
                "WeatherReading",
                where={"station": traffic.station(self.language)},
                bounds=sde.Range(
                    "at",
                    traffic.reading(self.language, first)["at"],
                    traffic.reading(self.language, last)["at"] + timedelta(microseconds=1),
                ),
                limit=traffic.BATCH * WINDOW["clickhouse"],
            )
            seen = set()
            for row in page.rows:
                number = traffic.reading_number(row)
                if row != traffic.reading(self.language, number):
                    self.check_failed("scan", number, "a scanned row is not the row written")
                seen.add(number // traffic.BATCH)
            expected = {n for n in range(low, high + 1) if n not in self.failed}
        missing = expected - seen
        if missing:
            self.check_failed("scan", min(missing), f"{len(missing)} acknowledged writes missing")

    def count(self, slot: int) -> None:
        bounds = self.window()
        if bounds is None:
            return
        low, high = bounds
        assert self.session is not None
        attempted_high = self.attempted
        if self.dialect == "postgres":
            name = traffic.account(self.language, slot)
            counted = self.session.count(
                "Order",
                where={"account": name},
                bounds=sde.Range(
                    "placed_at",
                    traffic.order(self.language, low)["placed_at"],
                    traffic.order(self.language, high)["placed_at"] + timedelta(microseconds=1),
                ),
            )
            numbers = [n for n in range(low, high + 1) if traffic.account(self.language, n) == name]
            least = sum(1 for n in numbers if n not in self.failed)
            most = len([n for n in numbers if n <= attempted_high])
        else:
            first, last = low * traffic.BATCH, high * traffic.BATCH + traffic.BATCH - 1
            counted = self.session.count(
                "WeatherReading",
                where={"station": traffic.station(self.language)},
                bounds=sde.Range(
                    "at",
                    traffic.reading(self.language, first)["at"],
                    traffic.reading(self.language, last)["at"] + timedelta(microseconds=1),
                ),
            )
            numbers = list(range(low, high + 1))
            least = traffic.BATCH * sum(1 for n in numbers if n not in self.failed)
            most = traffic.BATCH * len(numbers)
        if not least <= counted <= most:
            self.check_failed("count", low, f"counted {counted}, expected {least} to {most}")

    def check_failed(self, kind: str, number: int, what: str) -> None:
        self.totals["check_failed"] += 1
        self.event({"check_failed": kind, "number": number, "what": what})

    # --- the loop ----------------------------------------------------------------------------

    def watchdog(self) -> None:
        while not self.stop.wait(1.0):
            running = self.running
            if running is None:
                continue
            kind, started = running
            elapsed_s = (time.monotonic_ns() - started) / 1e9
            for mark in STUCK_REPORTS_S:
                if elapsed_s >= mark and (started, mark) not in self.reported_stuck:
                    self.reported_stuck.add((started, mark))
                    self.event({"stuck": kind, "running_s": mark, "started": started})

    def run(self) -> int:
        start = json.loads(Path(self.soak["start_file"]).read_bytes())
        zero = int(start["monotonic_ns"])
        stop_file = Path(self.soak["stop_file"])
        threading.Thread(target=self.watchdog, daemon=True).start()
        try:
            self.connect()
        except sde.SdeError as failure:
            self.connected = False
            self.event({"connect_failed": type(failure).__name__, "message": str(failure)[:300]})
        minute = Minute(time.monotonic_ns())
        slot = max(0, (time.monotonic_ns() - zero) // self.period_ns)
        while not self.stop.is_set() and not stop_file.exists():
            now = time.monotonic_ns()
            if now - minute.started_ns >= 60_000_000_000:
                self.flush_minute(minute)
                minute = Minute(now)
            due = zero + slot * self.period_ns
            if now < due:
                time.sleep(min((due - now) / 1e9, 0.05))
                continue
            kind = self.cycle[slot % len(self.cycle)]
            if now - due > self.period_ns:
                minute.op(kind)["skipped"] += 1
                slot += 1
                continue
            if not self.connected:
                began = time.monotonic_ns()
                try:
                    self.running = ("connect", began)
                    self.connect()
                except sde.SdeError as failure:
                    self.event(
                        {
                            "connect_failed": type(failure).__name__,
                            "duration_ns": time.monotonic_ns() - began,
                            "message": str(failure)[:300],
                        }
                    )
                    minute.op(kind)["unavailable"] += 1
                    slot += 1
                    continue
                finally:
                    self.running = None
                self.event({"connected": True, "duration_ns": time.monotonic_ns() - began})
            self.operate(minute, kind, slot, due)
            slot += 1
        self.flush_minute(minute)
        return 0

    def operate(self, minute: Minute, kind: str, slot: int, due: int) -> None:
        began = time.monotonic_ns()
        self.running = (kind, began)
        number: int | None = None
        error: str | None = None
        named = True
        message = ""
        try:
            if kind == "write":
                number = self.attempted + 1
                self.write()
                self.highest_acked = max(self.highest_acked, number)
            elif kind == "get":
                self.get(slot)
            elif kind == "scan":
                self.scan(slot)
            else:
                self.count(slot)
        except sde.SdeError as failure:
            error, message = type(failure).__name__, str(failure)[:300]
        except Exception as failure:  # outside the hierarchy: what requirement 3 forbids
            error, named, message = type(failure).__name__, False, str(failure)[:300]
            self.event({"traceback": traceback.format_exc()[-2000:]})
        finally:
            self.running = None
        ended = time.monotonic_ns()
        minute.record(kind, ended - began, began - due, error)
        if error is None:
            self.totals[kind] += 1
            if self.failures_since_success:
                self.event(
                    {
                        "recovered": kind,
                        "after_failures": self.failures_since_success,
                        "since_first_failure_ns": ended - (self.failing_since or ended),
                    }
                )
            self.failures_since_success, self.failing_since = 0, None
            return
        self.totals["error_" + error] += 1
        if number is not None:
            self.failed.add(number)
        if self.failures_since_success == 0:
            self.failing_since = began
        self.failures_since_success += 1
        self.event(
            {
                "error": error,
                "named": named,
                "op": kind,
                "number": number,
                "duration_ns": ended - began,
                "message": message,
            }
        )
        self.connected = False

    def close(self) -> None:
        if self.engine is not None:
            with contextlib.suppress(sde.SdeError):
                self.engine.close()
        done = {
            "language": self.language,
            "dialect": self.dialect,
            "attempted": self.attempted,
            "highest_acked": self.highest_acked,
            "failed": sorted(self.failed),
            "totals": dict(self.totals),
            "sdk_module": sde.__file__,
        }
        (self.out / "done.json").write_text(json.dumps(done))
        self.minutes.close()
        self.events.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--soak", type=Path, required=True)
    args = parser.parse_args()
    worker = Worker(json.loads(args.soak.read_bytes()))
    signal.signal(signal.SIGTERM, lambda *_: worker.stop.set())
    print("READY", flush=True)
    try:
        return worker.run()
    finally:
        worker.close()


if __name__ == "__main__":
    sys.exit(main())

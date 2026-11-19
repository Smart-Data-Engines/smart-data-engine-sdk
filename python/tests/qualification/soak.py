"""The soak: three libraries, two engines, a day, and the faults of ``soak_engines``/``soak_proxy``.

    PYTHONPATH=python/tests python/.venv/bin/python -m qualification.soak \\
        --run /absolute/new-directory --hours 24 \\
        --python /venv-with-the-wheel/bin/python \\
        [--npm /consumer/node_modules/@smart-data-engines/sde] \\
        [--cpp-worker /prefix/bin/sde_soak_worker] \\
        --pg-bin /usr/lib/postgresql/16/bin --clickhouse /usr/bin/clickhouse

``--short`` instead of ``--hours``: each fault once, two minutes apart. The run directory holds
everything the summary reads: ``run.json``, ``faults.jsonl``, ``samples.jsonl``, each process's
``minutes.jsonl``, ``events.jsonl`` and ``done.json``, ``oracle.json`` and ``summary.json``. A file
named ``STOP`` in it, or SIGTERM, ends the run early and still judges it.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from . import soak_oracle as oracle
from . import soak_summary as summary
from . import soak_traffic as traffic
from .project import project
from .soak_engines import PrivateClickHouse, PrivateEngine, PrivatePostgres
from .soak_proxy import Proxy

SDK = Path(__file__).resolve().parents[3]
KINDS = ("restart", "crash", "freeze", "reset", "refuse", "partition")
ENGINES = ("postgres", "clickhouse")
SAMPLE_EVERY_S = 60.0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def with_port(dsn: str, port: int) -> str:
    parts = urlsplit(dsn)
    host = parts.netloc.rsplit("@", 1)
    credentials = host[0] + "@" if len(host) == 2 else ""
    hostname = parts.hostname or "127.0.0.1"
    return urlunsplit(
        (parts.scheme, f"{credentials}{hostname}:{port}", parts.path, parts.query, parts.fragment)
    )


def schedule(hours: float, every_s: float) -> list[tuple[float, str, str]]:
    """(seconds after zero, kind, engine): every kind on every engine in turn, one per period."""
    order = [(kind, engine) for kind in KINDS for engine in ENGINES]
    faults = []
    at = every_s
    index = 0
    while at + every_s <= hours * 3600 + 1e-9:
        kind, engine = order[index % len(order)]
        faults.append((at, kind, engine))
        index += 1
        at += every_s
    return faults


def _lines(command: list[str]) -> list[str]:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    except OSError as failure:
        return [f"unavailable: {failure}"]
    return [line.strip() for line in (done.stdout or done.stderr).splitlines() if line.strip()]


def _first_line(command: list[str]) -> str:
    lines = _lines(command)
    return lines[0] if lines else ""


def machine() -> dict[str, Any]:
    cpu = next(
        (
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        ),
        "unknown",
    )
    memory = next(
        line.split(":", 1)[1].strip()
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith("MemTotal")
    )
    release = next(
        (
            line.split("=", 1)[1].strip().strip('"')
            for line in Path("/etc/os-release").read_text().splitlines()
            if line.startswith("PRETTY_NAME=")
        ),
        "unknown",
    )
    return {
        "host": platform.node(),
        "cpu": cpu,
        "cpus": os.cpu_count(),
        "memory": memory,
        "system": release,
        "kernel": platform.release(),
        "disks": _lines(["lsblk", "-dno", "NAME,MODEL,SIZE,ROTA"]),
    }


class Soak:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.run_dir: Path = args.run
        self.stop = threading.Event()
        self.engines: dict[str, PrivateEngine] = {}
        self.proxies: dict[str, Proxy] = {}
        self.processes: list[dict[str, Any]] = []
        self.meta: dict[str, Any] = {}
        self.faults_log = self.run_dir / "faults.jsonl"
        self.samples_log = self.run_dir / "samples.jsonl"

    # --- setup -----------------------------------------------------------------------------

    def languages(self) -> list[str]:
        chosen = ["python"]
        if self.args.npm:
            chosen.append("typescript")
        if self.args.cpp_worker:
            chosen.append("cpp")
        return chosen

    def start_engines(self) -> None:
        password = secrets.token_hex(16)
        postgres = PrivatePostgres(
            self.run_dir / "engines" / "postgres", self.args.pg_bin, free_port(), password
        )
        clickhouse = PrivateClickHouse(
            self.run_dir / "engines" / "clickhouse",
            self.args.clickhouse,
            free_port(),
            free_port(),
            password,
        )
        for engine in (postgres, clickhouse):
            engine.initialise()
            engine.start()
            self.engines[engine.name] = engine
            proxy = Proxy("127.0.0.1", engine.port)
            proxy.start()
            self.proxies[engine.name] = proxy
        os.environ["SDE_POSTGRES_DSN"] = postgres.admin_dsn
        os.environ["SDE_CLICKHOUSE_DSN"] = clickhouse.admin_dsn

    def launch(self, configured: dict[str, dict[str, Any]], zero: int) -> None:
        start_file = self.run_dir / "start.json"
        start_file.write_text(json.dumps({"monotonic_ns": zero}))
        for language in self.languages():
            for engine in ENGINES:
                project_of = configured[engine]
                name = f"{language}-{engine}"
                out = self.run_dir / "processes" / name
                out.mkdir(parents=True)
                soak_config = {
                    "language": language,
                    "dialect": engine,
                    "project_config": str(project_of["config"]),
                    "project_dir": str(project_of["directory"] / "state"),
                    "output": str(out),
                    "start_file": str(start_file),
                    "stop_file": str(self.run_dir / "STOP-PROCESSES"),
                    "period_ms": self.args.period_ms,
                    "batch": traffic.BATCH,
                }
                config_path = out / "soak.json"
                config_path.write_text(json.dumps(soak_config))
                environment = dict(os.environ, **project_of["environment"])
                environment.pop("PYTHONPATH", None)
                variable = f"SDE_QUALIFY_APP_{engine.upper()}"
                environment[variable] = with_port(environment[variable], self.proxies[engine].port)
                # The application holds its runtime login and nothing else.
                for admin in (
                    "SDE_POSTGRES_DSN",
                    "SDE_CLICKHOUSE_DSN",
                    "SDE_QUALIFY_OPERATOR_POSTGRES",
                    "SDE_QUALIFY_OPERATOR_CLICKHOUSE",
                ):
                    environment.pop(admin, None)
                if language == "python":
                    environment["PYTHONPATH"] = str(SDK / "python" / "tests")
                    command = [str(self.args.python), "-m", "qualification.soak_worker"]
                elif language == "typescript":
                    command = [
                        "node",
                        str(SDK / "typescript/tests/qualification/soak-worker.mjs"),
                        "--sdk",
                        str(self.args.npm),
                    ]
                else:
                    command = [str(self.args.cpp_worker)]
                command += ["--soak", str(config_path)]
                log = (out / "stderr.log").open("ab")
                try:
                    child = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        env=environment,
                        cwd=str(out),
                        start_new_session=True,
                    )
                finally:
                    log.close()
                self.processes.append(
                    {"name": name, "language": language, "engine": engine, "child": child}
                )

    # --- while it runs -----------------------------------------------------------------------

    def sample(self) -> None:
        processes: dict[str, dict[str, int] | None] = {}
        for entry in self.processes:
            pid = entry["child"].pid
            try:
                status = Path(f"/proc/{pid}/status").read_text().splitlines()
                fields = {line.split(":")[0]: line.split(":", 1)[1].strip() for line in status}
                processes[entry["name"]] = {
                    "rss_kib": int(fields["VmRSS"].split()[0]),
                    "threads": int(fields["Threads"]),
                    "fds": len(os.listdir(f"/proc/{pid}/fd")),
                }
            except (OSError, KeyError, ValueError):
                processes[entry["name"]] = None
        engines = {}
        for name, engine in self.engines.items():
            total = 0
            for pid in self._safe_pids(engine):
                with contextlib.suppress(OSError, ValueError, IndexError):
                    text = Path(f"/proc/{pid}/status").read_text()
                    total += int(text.split("VmRSS:")[1].split()[0])
            engines[name] = {"rss_kib": total}
        record = {
            "t": time.monotonic_ns(),
            "boottime": time.clock_gettime_ns(time.CLOCK_BOOTTIME),
            "wall": datetime.now(UTC).isoformat(),
            "load": Path("/proc/loadavg").read_text().split()[:3],
            "processes": {k: v for k, v in processes.items() if v is not None},
            "gone": sorted(k for k, v in processes.items() if v is None),
            "engines": engines,
        }
        with self.samples_log.open("a") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")

    @staticmethod
    def _safe_pids(engine: PrivateEngine) -> list[int]:
        try:
            return engine.pids()
        except Exception:  # the engine is between processes during a fault
            return []

    def sampler(self) -> None:
        while not self.stop.wait(SAMPLE_EVERY_S):
            self.sample()

    def inject(self, kind: str, engine_name: str) -> None:
        engine, proxy = self.engines[engine_name], self.proxies[engine_name]
        hold = self.args.fault_seconds
        started = time.monotonic_ns()
        wall = datetime.now(UTC).isoformat()
        detail: dict[str, Any] = {}
        if kind == "restart":
            engine.restart()
        elif kind == "crash":
            engine.crash()
        elif kind == "freeze":
            frozen = engine.freeze()
            detail["processes"] = len(frozen)
            try:
                self.stop.wait(hold)
            finally:
                engine.thaw(frozen)
        elif kind == "reset":
            detail["connections"] = proxy.reset()
        elif kind == "refuse":
            proxy.set_mode("refuse")
            try:
                self.stop.wait(hold)
            finally:
                proxy.set_mode("pass")
        elif kind == "partition":
            proxy.set_mode("stall")
            try:
                self.stop.wait(hold)
            finally:
                detail["connections"] = proxy.reset()
                proxy.set_mode("pass")
        else:
            raise ValueError(kind)
        record = {
            "kind": kind,
            "engine": engine_name,
            "start": started,
            "end": time.monotonic_ns(),
            "wall_start": wall,
            "wall_end": datetime.now(UTC).isoformat(),
            **detail,
        }
        with self.faults_log.open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    def wait_until(self, deadline_ns: int) -> bool:
        """Sleep until ``deadline_ns`` on the monotonic clock; False when the run is stopped."""
        while True:
            if self.stop.is_set() or (self.run_dir / "STOP").exists():
                self.stop.set()
                return False
            left = (deadline_ns - time.monotonic_ns()) / 1e9
            if left <= 0:
                return True
            self.stop.wait(min(left, 5.0))

    # --- the end -----------------------------------------------------------------------------

    def stop_processes(self) -> None:
        (self.run_dir / "STOP-PROCESSES").write_text("stop\n")
        deadline = time.monotonic() + 120
        for entry in self.processes:
            child = entry["child"]
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=max(1.0, deadline - time.monotonic()))
        for entry in self.processes:
            child = entry["child"]
            if child.poll() is None:
                entry["escalated"] = "SIGTERM"
                child.send_signal(signal.SIGTERM)
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    entry["escalated"] = "SIGKILL"
                    child.kill()
                    child.wait(timeout=30)
            entry["exit_code"] = child.returncode

    def record(self, name: str) -> oracle.Record:
        out = self.run_dir / "processes" / name
        done = out / "done.json"
        if done.exists():
            final = json.loads(done.read_text())
            return oracle.Record(
                attempted=int(final["attempted"]),
                highest_acked=int(final["highest_acked"]),
                failed={int(n) for n in final["failed"]},
            )
        minutes = summary.read_jsonl(out / "minutes.jsonl")
        events = summary.read_jsonl(out / "events.jsonl")
        failed = {int(e["number"]) for e in events if "error" in e and e.get("number") is not None}
        last = minutes[-1] if minutes else {"attempted": -1, "highest_acked": -1}
        attempted = max([int(last["attempted"]), *failed]) + 1  # one may have been in flight
        return oracle.Record(
            attempted=attempted, highest_acked=int(last["highest_acked"]), failed=failed
        )

    def judge(self, configured: dict[str, dict[str, Any]]) -> dict[str, Any]:
        verdicts = {}
        for entry in self.processes:
            name, language, engine = entry["name"], entry["language"], entry["engine"]
            record = self.record(name)
            namespace = configured[engine]["roles"][engine].namespace
            if engine == "postgres":
                orders, fills = oracle.read_postgres(
                    self.engines["postgres"].admin_dsn, namespace, language
                )
                verdict = oracle.judge_orders(language, record, orders, fills)
            else:
                rows, physical = oracle.read_clickhouse(
                    self.engines["clickhouse"].admin_dsn, namespace, language
                )
                verdict = oracle.judge_readings(language, record, rows, physical)
            verdicts[name] = verdict.as_dict()
        return verdicts

    def enter_project(
        self, stack: contextlib.ExitStack[Any], source: str, declaration: dict[str, Any]
    ) -> dict[str, Any]:
        """A project whose clean-up may fail without failing the run.

        The fixture holds its administrator's connections for the whole run, and the faults end
        them: a restart terminates every session. Dropping the namespace then fails on a closed
        connection - and is not needed, since the engines are the run's own and go with it.
        """
        manager = project(self.run_dir / f"project-{source}", source, declaration=declaration)
        configured = manager.__enter__()

        def leave() -> None:
            try:
                manager.__exit__(None, None, None)
            except Exception as failure:
                self.meta.setdefault("teardown", []).append(
                    f"{source} project clean-up: {type(failure).__name__}"
                )

        stack.callback(leave)
        return configured

    def write_meta(self, **more: Any) -> None:
        self.meta.update(more)
        self.meta["processes"] = [
            {key: value for key, value in entry.items() if key != "child"}
            | {"pid": entry["child"].pid}
            for entry in self.processes
        ]
        (self.run_dir / "run.json").write_text(json.dumps(self.meta, indent=1) + "\n")

    def run(self) -> int:
        args = self.args
        self.run_dir.mkdir(parents=True, exist_ok=False)
        hours = 0.0 if args.short else float(args.hours)
        every = min(120.0, args.fault_every_s) if args.short else float(args.fault_every_s)
        planned = (
            [(every * (i + 1), kind, engine) for i, (kind, engine) in
             enumerate((k, e) for k in KINDS for e in ENGINES)]
            if args.short
            else schedule(hours, every)
        )  # fmt: skip
        if args.short:
            hours = (planned[-1][0] + every) / 3600
        self.meta = {
            "protocol": 1,
            "hours": hours,
            "fault_every_s": every,
            "fault_seconds": args.fault_seconds,
            "period_ms": args.period_ms,
            "languages": self.languages(),
            "machine": machine(),
            "runtimes": {
                "python": _first_line([str(args.python), "--version"]),
                "node": _first_line(["node", "--version"]) if args.npm else None,
                "cpp_worker": str(args.cpp_worker) if args.cpp_worker else None,
            },
            "artifacts": json.loads(args.artifacts.read_text()) if args.artifacts else None,
            "planned_faults": [list(p) for p in planned],
            "started_wall": datetime.now(UTC).isoformat(),
        }
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        sampler = threading.Thread(target=self.sampler, daemon=True)
        try:
            self.start_engines()
            self.meta["engines"] = {
                name: {"version": engine.version(), "port": engine.port}
                for name, engine in self.engines.items()
            }
            with contextlib.ExitStack() as stack:
                configured = {
                    "postgres": self.enter_project(stack, "postgres", traffic.POSTGRES_DECLARATION),
                    "clickhouse": self.enter_project(
                        stack, "clickhouse", traffic.CLICKHOUSE_DECLARATION
                    ),
                }
                zero = time.monotonic_ns() + 10_000_000_000
                self.meta["zero"] = zero
                self.launch(configured, zero)
                self.write_meta()
                self.sample()
                sampler.start()
                for at, kind, engine in planned:
                    if not self.wait_until(zero + int(at * 1e9)):
                        break
                    self.sample()  # the descriptors and memory just before the fault
                    self.inject(kind, engine)
                self.wait_until(zero + int(hours * 3600 * 1e9))
                self.stop.set()
                self.meta["end"] = time.monotonic_ns()
                self.meta["ended_wall"] = datetime.now(UTC).isoformat()
                self.stop_processes()
                self.sample()
                self.write_meta()
                verdicts = self.judge(configured)
                (self.run_dir / "oracle.json").write_text(json.dumps(verdicts, indent=1) + "\n")
            result = summary.summarise(summary.load(self.run_dir))
            (self.run_dir / "summary.json").write_text(json.dumps(result, indent=1) + "\n")
            print(
                json.dumps({"passed": result["passed"], "breaches": result["breaches"]}, indent=1)
            )
            return 0 if result["passed"] else 1
        finally:
            self.stop.set()
            for entry in self.processes:
                if entry["child"].poll() is None:
                    entry["child"].kill()
            for proxy in self.proxies.values():
                proxy.close()
            for private in self.engines.values():
                with contextlib.suppress(Exception):
                    private.stop()
            if not args.keep_engines:
                for name in self.engines:
                    shutil.rmtree(self.run_dir / "engines" / name / "data", ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    length = parser.add_mutually_exclusive_group(required=True)
    length.add_argument("--hours", type=float)
    length.add_argument("--short", action="store_true")
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--npm", type=Path)
    parser.add_argument("--cpp-worker", type=Path)
    parser.add_argument("--pg-bin", type=Path, default=Path("/usr/lib/postgresql/16/bin"))
    parser.add_argument("--clickhouse", type=Path, default=Path("/usr/bin/clickhouse"))
    parser.add_argument("--period-ms", type=int, default=100)
    parser.add_argument("--fault-every-s", type=float, default=1200.0)
    parser.add_argument("--fault-seconds", type=float, default=45.0)
    parser.add_argument("--artifacts", type=Path, help="JSON of the artifacts' SHA-256")
    parser.add_argument("--keep-engines", action="store_true")
    args = parser.parse_args()
    if not args.run.is_absolute():
        parser.error("--run must be an absolute path")
    return Soak(args).run()


if __name__ == "__main__":
    sys.exit(main())

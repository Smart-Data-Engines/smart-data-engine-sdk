"""PostgreSQL and ClickHouse of the soak's own: processes, data directories and ports of a run.

The faults the soak injects into an engine - a restart, a crash, a freeze - are done to these and to
nothing else, so another session's test engines and CI never see them, and no step needs root.

PostgreSQL's processes each call ``setsid()``, so a signal to the postmaster's process group does
not reach them: a freeze or a crash is sent to the postmaster and to each of its children, read from
``/proc`` at that moment. ClickHouse runs without its watchdog, which would otherwise restart a
killed server by itself and take the crash away from the soak.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import signal
import subprocess
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

READY_SECONDS = 180.0


class EngineDown(RuntimeError):
    """The engine did not start, or did not stop, within its bound."""


def children(parent: int, proc: Path = Path("/proc")) -> list[int]:
    """The processes whose parent is ``parent``, read from ``/proc/<pid>/stat``."""
    found = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue  # gone while we looked
        # The command name is in parentheses and may hold spaces or parentheses itself.
        fields = stat[stat.rindex(")") + 2 :].split()
        if int(fields[1]) == parent:
            found.append(int(entry.name))
    return sorted(found)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie answers kill(0) until it is reaped: read its state.
    try:
        state = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return state[state.rindex(")") + 2] != "Z"


class PrivateEngine(Protocol):
    name: str
    dialect: str
    port: int

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def restart(self) -> None: ...
    def crash(self) -> None: ...
    def freeze(self) -> list[int]: ...
    def thaw(self, frozen: list[int]) -> None: ...
    def pids(self) -> list[int]: ...
    @property
    def admin_dsn(self) -> str: ...
    def version(self) -> str: ...


class _Process:
    """One server process, started in a session of its own, its output in a log file."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.process: subprocess.Popen[bytes] | None = None

    def launch(self, command: list[str], environment: dict[str, str] | None = None) -> None:
        log = (self.directory / "server.out").open("ab")
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=environment,
            )
        finally:
            log.close()

    @property
    def pid(self) -> int:
        if self.process is None:
            raise EngineDown("the engine has not been started")
        return self.process.pid

    def wait(self, seconds: float) -> None:
        if self.process is None:
            return
        try:
            self.process.wait(timeout=seconds)
        except subprocess.TimeoutExpired as late:
            raise EngineDown(f"the engine did not exit within {seconds} s") from late


def _ready(check: Callable[[], bool], what: str, seconds: float = READY_SECONDS) -> None:
    deadline = time.monotonic() + seconds
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except Exception as failure:  # any failure means "not yet"
            last = failure
        time.sleep(0.25)
    raise EngineDown(f"{what} was not ready within {seconds} s: {last!r}")


class PrivatePostgres:
    """``initdb`` once, then ``postgres -D`` as a child of the soak."""

    name = "postgres"
    dialect = "postgres"

    def __init__(self, directory: Path, binaries: Path, port: int, password: str) -> None:
        self.directory, self.binaries = directory, binaries
        self.port, self.password = port, password
        self.data = directory / "data"
        self.server = _Process(directory)

    @property
    def admin_dsn(self) -> str:
        return (
            f"postgresql://postgres:{quote(self.password, safe='')}@127.0.0.1:{self.port}/postgres"
        )

    def initialise(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=False)
        secret = self.directory / "password"
        secret.write_text(self.password + "\n")
        secret.chmod(0o600)
        subprocess.run(
            [
                str(self.binaries / "initdb"),
                "-D",
                str(self.data),
                "-U",
                "postgres",
                "--auth=scram-sha-256",
                f"--pwfile={secret}",
                "-E",
                "UTF8",
                "--locale=C.UTF-8",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=300,
        )
        secret.unlink()
        sockets = self.directory / "sockets"
        sockets.mkdir()
        with (self.data / "postgresql.conf").open("a") as conf:
            conf.write(
                f"\n# The soak's private engine\nport = {self.port}\n"
                "listen_addresses = '127.0.0.1'\n"
                f"unix_socket_directories = '{sockets}'\n"
                "log_min_messages = warning\nlog_connections = off\nlog_disconnections = off\n"
            )

    def start(self) -> None:
        self.server.launch([str(self.binaries / "postgres"), "-D", str(self.data)])
        _ready(self._answers, "PostgreSQL")

    def _answers(self) -> bool:
        import psycopg

        with psycopg.connect(self.admin_dsn, connect_timeout=5) as connection:
            return connection.execute("SELECT 1").fetchone() == (1,)

    def version(self) -> str:
        import psycopg

        with psycopg.connect(self.admin_dsn, connect_timeout=5) as connection:
            row = connection.execute("SHOW server_version").fetchone()
        assert row is not None
        return str(row[0])

    def pids(self) -> list[int]:
        return [self.server.pid, *children(self.server.pid)]

    def stop(self) -> None:
        # SIGINT is PostgreSQL's fast shutdown: sessions are ended, nothing waits for clients.
        if self.server.process is not None and self.server.process.poll() is None:
            os.kill(self.server.pid, signal.SIGINT)
            self.server.wait(120)

    def restart(self) -> None:
        self.stop()
        self.start()

    def crash(self) -> None:
        """SIGKILL to the postmaster and every child at once; the next start replays the WAL.

        The next postmaster refuses to start while a process of the last one still holds its
        shared memory, so every one of them is waited for, not only the postmaster.
        """
        victims = self.pids()
        for pid in victims:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        self.server.wait(30)
        deadline = time.monotonic() + 30
        while any(_alive(pid) for pid in victims):
            if time.monotonic() > deadline:
                raise EngineDown("a PostgreSQL process outlived SIGKILL by 30 s")
            time.sleep(0.1)
        self.start()

    def freeze(self) -> list[int]:
        frozen = self.pids()
        for pid in frozen:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGSTOP)
        return frozen

    def thaw(self, frozen: list[int]) -> None:
        for pid in frozen:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGCONT)


CLICKHOUSE_CONFIG = """<clickhouse>
  <logger>
    <level>warning</level>
    <log>{d}/server.log</log>
    <errorlog>{d}/error.log</errorlog>
    <size>50M</size>
    <count>2</count>
  </logger>
  <listen_host>127.0.0.1</listen_host>
  <http_port>{port}</http_port>
  <tcp_port>{tcp}</tcp_port>
  <path>{d}/data/</path>
  <tmp_path>{d}/tmp/</tmp_path>
  <user_files_path>{d}/user_files/</user_files_path>
  <format_schema_path>{d}/format_schemas/</format_schema_path>
  <user_directories>
    <users_xml><path>{d}/users.xml</path></users_xml>
    <local_directory><path>{d}/access/</path></local_directory>
  </user_directories>
  <mark_cache_size>268435456</mark_cache_size>
  <max_server_memory_usage_to_ram_ratio>0.3</max_server_memory_usage_to_ram_ratio>
</clickhouse>
"""

CLICKHOUSE_USERS = """<clickhouse>
  <profiles><default/></profiles>
  <quotas><default/></quotas>
  <users>
    <default>
      <password_sha256_hex>{digest}</password_sha256_hex>
      <networks><ip>127.0.0.1</ip></networks>
      <profile>default</profile>
      <quota>default</quota>
      <access_management>1</access_management>
    </default>
  </users>
</clickhouse>
"""


class PrivateClickHouse:
    """``clickhouse server`` with a configuration written into the run's directory.

    No query log or other system log tables: over a day of steady traffic they grow to gigabytes,
    and nothing in the soak reads them.
    """

    name = "clickhouse"
    dialect = "clickhouse"

    def __init__(
        self, directory: Path, binary: Path, port: int, tcp_port: int, password: str
    ) -> None:
        self.directory, self.binary, self.port = directory, binary, port
        self.tcp_port, self.password = tcp_port, password
        self.server = _Process(directory)

    @property
    def admin_dsn(self) -> str:
        return f"clickhouse://default:{quote(self.password, safe='')}@127.0.0.1:{self.port}/default"

    def initialise(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=False)
        d = str(self.directory)
        (self.directory / "config.xml").write_text(
            CLICKHOUSE_CONFIG.format(d=d, port=self.port, tcp=self.tcp_port)
        )
        users = self.directory / "users.xml"
        users.write_text(
            CLICKHOUSE_USERS.format(digest=hashlib.sha256(self.password.encode()).hexdigest())
        )
        users.chmod(0o600)

    def start(self) -> None:
        self.server.launch(
            [str(self.binary), "server", f"--config-file={self.directory / 'config.xml'}"],
            dict(os.environ, CLICKHOUSE_WATCHDOG_ENABLE="0"),
        )
        _ready(self._answers, "ClickHouse")

    def _answers(self) -> bool:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/ping", timeout=5) as reply:
            return bool(reply.read() == b"Ok.\n")

    def version(self) -> str:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/",
            data=b"SELECT version()",
            headers={"X-ClickHouse-User": "default", "X-ClickHouse-Key": self.password},
        )
        with urllib.request.urlopen(request, timeout=10) as reply:
            return str(reply.read().decode().strip())

    def pids(self) -> list[int]:
        return [self.server.pid]

    def stop(self) -> None:
        if self.server.process is not None and self.server.process.poll() is None:
            os.kill(self.server.pid, signal.SIGTERM)
            self.server.wait(120)

    def restart(self) -> None:
        self.stop()
        self.start()

    def crash(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(self.server.pid, signal.SIGKILL)
        self.server.wait(30)
        self.start()

    def freeze(self) -> list[int]:
        frozen = self.pids()
        for pid in frozen:
            os.kill(pid, signal.SIGSTOP)
        return frozen

    def thaw(self, frozen: list[int]) -> None:
        for pid in frozen:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGCONT)

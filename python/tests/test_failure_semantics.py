"""Requirement 6.6: the semantics of every operation under every kind of failure, before the sale.

The requirement names four kinds - the engine unavailable, a timeout, a connection cut mid
operation, and a migration in progress - and says they belong in documentation the client reads
before buying rather than in something they discover on production. `docs/failure-semantics.md` is
that document. This file is what stops it becoming prose: the first half measures the three
connection failures against real engines, and the second pairs every claim on the page with the
mechanism that makes it true.

**Two of the three named failures were defects rather than documentation gaps, and writing this
found them.** Neither adapter bounded opening a connection: measured against a socket that accepts
the TCP connection and then says nothing, the call did not return for as long as the test would wait
- 45 seconds, and there was no reason to believe it would ever return. libpq has no default
`connect_timeout`, and for ClickHouse the TCP connect succeeds, so what was left was the driver's
`send_receive_timeout` default of 300 seconds. That is a hang inside the caller's request path, in
the ordinary case of a firewall that accepts, a load balancer with no healthy backend, or a server
mid-restart. Both are bounded now, both bounds are defaults a DSN overrides, and the numbers in the
document come from this file rather than from a guess.

The third was a diagnostic. After a connection is cut, the first failing call reports what the
server said, which is right, and **every call after it reports "the connection is closed"** - true
and useless at the moment the reader most needs to be told that this library does not reopen a
connection it was handed.
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import threading
import time
import urllib.parse
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

import sde
from sde.engines.clickhouse import CONNECT_TIMEOUT_SECONDS as CH_CONNECT
from sde.engines.clickhouse import HANDSHAKE_TIMEOUT_SECONDS as CH_HANDSHAKE
from sde.engines.clickhouse import ClickHouseEngine
from sde.engines.postgres import CONNECT_TIMEOUT_SECONDS as PG_CONNECT
from sde.engines.postgres import PostgresEngine
from sde.errors import EngineError

PG_DSN = os.environ.get("SDE_POSTGRES_DSN")
CH_DSN = os.environ.get("SDE_CLICKHOUSE_DSN")
DOC = "docs/failure-semantics.md"


@pytest.fixture(autouse=True)
def _isolate() -> None:
    sde.clear_registry()


def _document() -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parents[2] / DOC).read_text(encoding="utf-8")


def _flat() -> str:
    """The document with its line breaks collapsed.

    Markdown wraps at a hundred columns, so a sentence to assert on is split across lines about half
    the time. Asserting on the raw text makes these checks depend on where the wrap fell, which is
    the kind of test that passes until somebody reflows a paragraph.
    """
    return " ".join(_document().split())


def _with_port(dsn: str, port: int) -> str:
    """The same DSN, pointed at another port.

    Not a string replacement of the port this machine happens to use. The first version of this
    file did `dsn.replace(":55432", ":55499")`, which is a no-op against CI's `:5432` - so four
    tests connected to the *real* engine, found it healthy, and failed on "DID NOT RAISE". A test
    whose setup silently does nothing is worse than a missing test: it reports on something else.
    """
    parsed = urllib.parse.urlsplit(dsn)
    host = parsed.hostname or "127.0.0.1"
    credentials = ""
    if parsed.username:
        credentials = parsed.username + (f":{parsed.password}" if parsed.password else "") + "@"
    return urllib.parse.urlunsplit(
        (parsed.scheme, f"{credentials}{host}:{port}", parsed.path, parsed.query, parsed.fragment)
    )


def _free_port() -> int:
    """A port with nothing on it. Bound and released, so the number is known to be unused."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])

@contextlib.contextmanager
def _within(seconds: int) -> Iterator[None]:
    """Fail if the block has not finished in time, rather than waiting for it.

    The bound being tested is a bound on *waiting*, so a test that loses it would hang instead of
    failing - and a hung CI job reads as an infrastructure problem rather than as the defect it is.
    Verified by removing the default: with this alarm the test fails in seconds; without it, the
    run does not end.
    """
    def ring(*_: object) -> None:
        raise TimeoutError(f"still waiting after {seconds}s, so nothing bounded this call")

    previous = signal.signal(signal.SIGALRM, ring)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _model() -> sde.LogicalModel:
    @sde.entity
    class Reading:
        id: uuid.UUID
        sensor: str

    return sde.build_model(Reading)


def _map(model: sde.LogicalModel) -> sde.PlacementMap:
    raw: dict[str, Any] = {
        "contract": sde.CONTRACT,
        "model_version": model.version,
        "map_version": 1,
        "groups": {
            group.name: {
                "source": {"id": f"{group.name}@pg", "engine": "pg", "layout": {"auto": True}}
            }
            for group in sde.colocation_groups(model)
        },
    }
    return sde.load_map(raw, model=model)


@pytest.fixture
def silent_port() -> Iterator[int]:
    """A socket that completes the TCP handshake and then says nothing, ever.

    The failure this represents is not exotic. It is what a firewall configured to accept looks
    like, and a load balancer with no healthy backend, and a server that is starting up. A refused
    connection is easy; this is the one that used to hang.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    accepted: list[Any] = []

    def accept_and_ignore() -> None:
        while True:
            try:
                accepted.append(listener.accept()[0])
            except OSError:
                return

    threading.Thread(target=accept_and_ignore, daemon=True).start()
    try:
        yield int(listener.getsockname()[1])
    finally:
        listener.close()
        for connection in accepted:
            connection.close()


# --- the three connection failures, measured ---------------------------------------------------


def test_an_engine_that_is_not_listening_fails_immediately_and_says_why() -> None:
    if PG_DSN is None:
        pytest.skip("set SDE_POSTGRES_DSN")
    closed = _with_port(PG_DSN, _free_port())
    # The setup has to have done something. This is the assertion the first version of this file
    # was missing, and without it four tests connected to a healthy engine and reported on it.
    assert closed != PG_DSN
    started = time.monotonic()
    with pytest.raises(EngineError, match="could not connect to PostgreSQL") as raised:
        PostgresEngine(closed).connect()
    elapsed = time.monotonic() - started

    # The driver's own reason, not ours: it names the host and the port it tried.
    assert "connection" in str(raised.value)
    # Fast, and the assertion is loose on purpose - what matters is that it does not sit on the
    # connect timeout, which would mean the refusal was being treated as silence.
    assert elapsed < PG_CONNECT, f"a refused connection took {elapsed:.1f}s"


def test_an_engine_that_accepts_and_stays_silent_is_bounded(silent_port: int) -> None:
    """The measurement that turned a documentation task into a fix.

    Before the bound: the call did not return within 45 seconds. The assertion is two-sided,
    because the interesting failure is in both directions - unbounded means a hang in a request
    path, and a bound far below the default would mean this library deciding that a slow network is
    a broken one.
    """
    if PG_DSN is None:
        pytest.skip("set SDE_POSTGRES_DSN")
    hung = _with_port(PG_DSN, silent_port)
    assert hung != PG_DSN
    started = time.monotonic()
    refused = pytest.raises(EngineError, match="could not connect to PostgreSQL")
    with _within(PG_CONNECT + 5), refused:
        PostgresEngine(hung).connect()
    elapsed = time.monotonic() - started
    assert PG_CONNECT - 1 <= elapsed <= PG_CONNECT + 5, f"gave up after {elapsed:.1f}s"


def test_a_timeout_the_caller_chose_wins_over_ours(silent_port: int) -> None:
    """The bound is a default, not a rule. A `connect_timeout` in the DSN is the caller's decision
    about their own network, and a library that overrode it would be the wrong kind of helpful."""
    if PG_DSN is None:
        pytest.skip("set SDE_POSTGRES_DSN")
    hung = _with_port(PG_DSN, silent_port) + "?connect_timeout=2"
    started = time.monotonic()
    with pytest.raises(EngineError):
        PostgresEngine(hung).connect()
    elapsed = time.monotonic() - started
    assert elapsed < PG_CONNECT - 3, f"the caller asked for 2s and waited {elapsed:.1f}s"


def test_clickhouse_bounds_the_handshake_rather_than_the_query(silent_port: int) -> None:
    """Here the TCP connect succeeds, so `connect_timeout` never fires and the driver's 300 second
    read timeout is what is left. The handshake is bounded; a query deliberately is not."""
    if CH_DSN is None:
        pytest.skip("set SDE_CLICKHOUSE_DSN")
    hung = _with_port(CH_DSN, silent_port)
    assert hung != CH_DSN
    started = time.monotonic()
    refused = pytest.raises(EngineError, match="could not connect to ClickHouse")
    with _within(CH_HANDSHAKE + 5), refused:
        ClickHouseEngine(hung).connect()
    elapsed = time.monotonic() - started
    assert CH_HANDSHAKE - 1 <= elapsed <= CH_HANDSHAKE + 5, f"gave up after {elapsed:.1f}s"
    assert CH_CONNECT < CH_HANDSHAKE, "opening is bounded more tightly than the handshake"


def test_a_connection_cut_under_an_operation_reaches_the_caller_and_retries_nothing() -> None:
    """The third named failure, done by actually cutting the connection.

    Three claims in one test because they are one sequence: the failing call reports what the
    server said, nothing is retried, and every later call says the connection is gone *and what to
    do about it*. The last one is why the sequence is asserted rather than the first failure alone.
    """
    if PG_DSN is None:
        pytest.skip("set SDE_POSTGRES_DSN")
    import psycopg

    model = _model()
    engine = PostgresEngine(PG_DSN)
    engine.connect()
    session = sde.Session(model=model, placement=_map(model), engines={"pg": engine})
    session.ensure_schema()

    before = uuid.uuid4()
    session.save("Reading", {"id": before, "sensor": "before"})
    assert session.get("Reading", {"id": before})["sensor"] == "before"

    with psycopg.connect(PG_DSN, autocommit=True) as killer, killer.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        )
        assert cur.fetchall(), "nothing was terminated, so this test proves nothing"

    lost = uuid.uuid4()
    with pytest.raises(EngineError, match="insert into") as first:
        session.save("Reading", {"id": lost, "sensor": "during"})
    # The server's own words. A wrapper that replaced them with "write failed" would be hiding the
    # one piece of information that says whether this was the engine, the network or us.
    assert "terminating connection" in str(first.value)

    with pytest.raises(EngineError) as second:
        session.save("Reading", {"id": uuid.uuid4(), "sensor": "after"})
    assert "does not reopen one it was handed" in str(second.value)
    assert "Nothing was retried" in str(second.value)

    with pytest.raises(EngineError, match="select from"):
        session.get("Reading", {"id": before})

    # Recovery is the caller's, it is two calls, and it works.
    engine.close()
    engine.connect()
    assert session.get("Reading", {"id": before})["sensor"] == "before"
    # And the write that failed did not land. "Nothing was retried" is only worth saying if the
    # row's absence is checked rather than assumed.
    assert session.get("Reading", {"id": lost}) is None


# --- the document, paired with its mechanisms --------------------------------------------------


def test_the_document_states_the_measured_bounds_and_they_are_the_constants() -> None:
    """A page that names a duration goes stale the moment the constant moves, and a client sizing
    their own request timeout on it would be sizing on last month."""
    text = _document()
    assert f"**{PG_CONNECT} s** on PostgreSQL" in text
    assert f"**{CH_HANDSHAKE} s** on ClickHouse" in text


def test_the_three_rules_are_on_the_page_and_each_one_holds() -> None:
    """Every claim, next to the thing that makes it true, so that lifting either fails this."""
    assert "We are not in your data path" in _flat()
    # The mechanism: an engine has nowhere to put an address of ours, and the library imports no
    # network module. Both are their own tests; named here so that dropping one fails the page too.
    # A sibling module, imported the way pytest makes it available: `tests/` is not a package,
    # so its directory is on sys.path and `tests.test_no_account` only resolved locally, as an
    # implicit namespace package. CI said ModuleNotFoundError.
    from test_no_account import MAY_IMPORT

    assert not {"http", "urllib", "socket", "requests"} & set(MAY_IMPORT)

    flat = _flat()
    assert "comes back to your code as an error" in flat
    assert "never retried into a different engine" in flat
    # The mechanism: routing resolves to exactly one materialisation, and a write resolves to the
    # source before anything else is consulted, so there is no second engine to fall back to.
    model = _model()
    placement = _map(model)
    write = next(s for s in sde.enumerate_shapes(model) if s.kind == "write")
    assert sde.resolve(placement, write).id.endswith("@pg")

    assert "only where the operation is known to be idempotent" in flat


def test_the_page_admits_what_it_cannot_promise_about_a_driver() -> None:
    """Our own silence is pinned by a test over our source. The drivers are not ours, and one of
    them writes to standard error on a failed connection - measured while writing this file. A page
    claiming silence for the whole stack would be claiming something we do not control."""
    text = _document()
    assert "Unexpected Http Driver Exception" in text
    assert "that is a driver configuration" in _flat()


def test_the_page_says_the_durations_are_not_a_service_level() -> None:
    assert "not a service level" in _flat()


def test_every_test_the_page_names_exists() -> None:
    """The last column is the reason to believe the row. A citation pointing at nothing is worse
    than no citation, because it reads as evidence."""
    import re
    from pathlib import Path

    here = Path(__file__).parent
    named = set(re.findall(r"`(test_[a-z_]+(?:\*)?\.py)`", _document()))
    assert named, "the document cites no tests"
    for name in sorted(named):
        if name.endswith("*.py"):
            stem = name[: -len("*.py")]
            assert list(here.glob(f"{stem}*.py")), name
        else:
            assert (here / name).is_file(), name


def test_the_page_is_reachable_from_the_readme() -> None:
    """A document a client cannot find is a document that gets asked for in a call instead."""
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")
    assert "docs/failure-semantics.md" in readme

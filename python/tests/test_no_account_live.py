"""Requirement 12.6 against live engines: nothing is attempted, and nothing is said twice.

The structural half is in ``test_no_account.py``. This is the half that needs a running engine,
for two different reasons.

**The stream comparison needs real operations.** The claim is that the account-free mode emits no
event the account mode does not, and the only way to be sure is to run the same work under a real
signed map and a real hand-written one and compare what came out. A fake engine would agree with
whatever this library believes, which here includes the belief under test.

**The absence of an outbound connection needs an instrument that can be shown to see one.** An
audit hook is that instrument, and it is only worth installing if it fires. Measured: psycopg
connects through libpq in C and raises no audit event at all, while clickhouse-connect goes over
``http.client`` and raises fourteen. So the assertion is deliberately not "the list is empty" - an
empty list is what a blind instrument produces. It is **the list is not empty and every address in
it is the client's own engine**, with a connection this test makes itself as the control.

The hook runs in a subprocess because ``sys.addaudithook`` cannot be undone, and because a fresh
process is the only way to cover import time - a library that phones home does it on import.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import json
import logging
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import sde
from sde.canonical import canonical_bytes
from sde.engines.postgres import PostgresEngine
from sde.watermark import WATERMARK_TABLE

PG_DSN = os.environ.get("SDE_POSTGRES_DSN")
CH_DSN = os.environ.get("SDE_CLICKHOUSE_DSN")


# --- the two documents, both real ----------------------------------------------------------------
#
# The signed one is signed with a key generated here rather than by substituting the flag, which
# other tests do quite reasonably. Here it matters: `load_map` has to take the same path a client's
# process takes, verification included, or the streams being compared are not the two modes.


def _model() -> sde.LogicalModel:
    sde.clear_registry()

    @sde.entity
    class Event:
        id: uuid.UUID
        name: str
        at: dt.datetime

    return sde.build_model(Event)


def _raw(model: sde.LogicalModel) -> dict[str, Any]:
    return {
        "contract": sde.CONTRACT,
        "model_version": model.version,
        "map_version": 1,
        "groups": {
            group.name: {
                "source": {
                    "id": f"{group.name}@pg",
                    "engine": "pg-test",
                    "layout": {"auto": True},
                }
            }
            for group in sde.colocation_groups(model)
        },
    }


def _sign(raw: dict[str, Any]) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    private = Ed25519PrivateKey.generate()
    raw["signature"] = {
        "alg": "ed25519",
        "key_id": "k1",
        "value": base64.b64encode(private.sign(canonical_bytes(raw))).decode(),
    }
    return private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.events.append(str(record.msg))


@contextlib.contextmanager
def _capturing() -> Iterator[list[str]]:
    logger = logging.getLogger("sde")
    handler = _Collect()
    previous = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        yield handler.events
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


# --- the stream comparison -----------------------------------------------------------------------


pg_only = pytest.mark.skipif(not PG_DSN, reason="set SDE_POSTGRES_DSN")


@pytest.fixture()
def engine() -> Iterator[PostgresEngine]:
    assert PG_DSN
    with PostgresEngine(PG_DSN) as eng:
        with eng._cx.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS "event" CASCADE')  # type: ignore[arg-type]
            # The bookkeeping too. Dropping it is the deliberate act the refusal's own message
            # names, and without it a second run of this file meets its own watermark.
            cur.execute(f'DROP TABLE IF EXISTS "{WATERMARK_TABLE}" CASCADE')  # type: ignore[arg-type]
        yield eng


def _operate(session: sde.Session) -> None:
    """The full set an application has: schema, write, read, transaction, fresh read."""
    session.ensure_schema()
    key = uuid.uuid4()
    session.save("Event", {"id": key, "name": "a", "at": dt.datetime.now(dt.UTC)})
    session.get("Event", {"id": key})
    with session.transaction("Event") as tx:
        tx.save(
            "Event",
            {"id": uuid.uuid4(), "name": "b", "at": dt.datetime.now(dt.UTC)},
        )
    session.get("Event", {"id": key}, fresh=True)


@pg_only
def test_the_account_free_mode_emits_no_event_the_account_mode_does_not(
    engine: PostgresEngine,
) -> None:
    """The whole of 12.6's second half, as a property of the stream rather than of any wording.

    A nag is a line the free mode emits and the paying mode does not, whatever it says. So is a
    line that fires once per operation instead of once per process. Neither can survive this
    comparison, and it keeps holding for complaints nobody has thought of yet.

    The rule is deliberately directional. The account mode emitting *more* is fine and expected -
    a signed map buys the forward-only check, and that check has a line. The assertion below shows
    that difference explicitly, which is also what keeps the first assertion from being vacuous:
    the two sides are not simply equal.
    """
    model = _model()

    unsigned_raw = _raw(model)
    signed_raw = _raw(model)
    public = _sign(signed_raw)

    with _capturing() as free_load:
        free_map = sde.load_map(unsigned_raw, model=model)
        free_session = sde.Session(model, free_map, {"pg-test": engine})
    with _capturing() as free_ops:
        _operate(free_session)

    with _capturing() as paid_load:
        paid_map = sde.load_map(signed_raw, model=model, public_key=public)
        paid_session = sde.Session(model, paid_map, {"pg-test": engine})
    with _capturing() as paid_ops:
        _operate(paid_session)

    assert free_map.signed is False
    assert paid_map.signed is True

    # 1. Nothing at load is said only to the client without an account.
    assert set(free_load) - set(paid_load) == set(), sorted(set(free_load) - set(paid_load))

    # 2. The difference runs the other way, and is exactly the check a signature buys.
    assert set(paid_load) - set(free_load) == {"sde.map.forward_only"}

    # 3. Per operation the two streams are identical, sequence and all. This is the assertion a
    #    once-per-call complaint would fail, and it is stronger than a set comparison: a line
    #    emitted twice as often in one mode would pass a set comparison unnoticed.
    assert free_ops == paid_ops
    assert free_ops, "the comparison is worthless if no operation logged anything"


def test_the_two_lines_a_map_load_produces_actually_fire() -> None:
    """The complement to the static vocabulary check, and it took a surviving mutation to notice.

    The static test reads ``log()`` call sites, so it proves the vocabulary and the code agree
    about which names exist. It cannot prove a call site is reachable: putting ``if False:`` in
    front of one leaves the site where the parser finds it and emits nothing, and that mutation
    survived. Reachability is a behavioural question, so it is asked behaviourally, of the two
    lines an operator needs most - the model version everything is keyed on, and the map that was
    accepted.
    """
    with _capturing() as events:
        model = _model()
    assert events.count("sde.model.built") == 1

    with _capturing() as events:
        placement = sde.load_map(_raw(model), model=model)
    assert events.count("sde.map.loaded") == 1
    assert placement.map_version == 1

    with _capturing() as events, pytest.raises(Exception, match="model version"):
        broken = _raw(model)
        broken["model_version"] = "0" * 16
        sde.load_map(broken, model=model)
    assert events.count("sde.map.rejected") == 1


@pg_only
def test_the_line_that_names_the_mode_is_once_per_process_not_once_per_operation(
    engine: PostgresEngine,
) -> None:
    """"Does not go on about it" is a rate, so it is measured as one.

    Fifty operations. A per-operation line is the failure requirement 12.6 describes literally -
    "warns about it in every log line" - and it is also the one a reviewer reading a diff cannot
    see, because the line looks identical either way; only its position in the file differs.
    """
    model = _model()
    with _capturing() as events:
        placement = sde.load_map(_raw(model), model=model)
        session = sde.Session(model, placement, {"pg-test": engine})
        session.ensure_schema()
        for _ in range(50):
            key = uuid.uuid4()
            session.save(
                "Event", {"id": key, "name": "x", "at": dt.datetime.now(dt.UTC)}
            )
            session.get("Event", {"id": key})

    assert events.count("sde.map.loaded") == 1

    # What makes "once" a property rather than an artefact of a quiet library: the stream does
    # scale with the workload, one line per resolved read. Measured, a write logs nothing on the
    # routing path - it returns the source without resolving - so fifty gets are fifty lines and
    # fifty saves are none.
    assert events.count("sde.route.fallback") == 50
    assert len(events) >= 51


# --- nothing is attempted ------------------------------------------------------------------------


_PROBE = '''
"""Everything this library does, with an audit hook watching the socket layer from before import.

Printed as JSON on the last line. The hook is installed first so that import time is covered: a
library that phones home does it on import, and a test that installs the hook afterwards would be
the one thing unable to see it.
"""
import json, os, sys

FORBIDDEN_PREFIXES = ("urllib.", "smtplib.", "ftplib.", "telnetlib.", "imaplib.", "nntplib.")

attempts, forbidden = [], []


def destination(event, args):
    """Host and port, structured. Reported as a pair rather than as a repr so that the caller can
    compare it to an address instead of searching a string for one - a substring test would let
    127.0.0.1:9999 through on the strength of the host."""
    if event == "socket.connect":
        address = args[1]
        if isinstance(address, tuple) and len(address) >= 2:
            return [str(address[0]), int(address[1])]
        return [repr(address), -1]
    if event in ("socket.getaddrinfo", "socket.gethostbyname"):
        return [str(args[0]), int(args[1]) if len(args) > 1 and isinstance(args[1], int) else -1]
    if event == "http.client.connect":
        return [str(args[1]), int(args[2])]
    return [repr(args), -1]


def hook(event, args):
    if event in ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
                 "http.client.connect"):
        attempts.append([event] + destination(event, args))
    elif event.startswith(FORBIDDEN_PREFIXES):
        forbidden.append(event)


sys.addaudithook(hook)

# The control. If this does not turn up in `attempts`, nothing else in this file means anything.
import socket

host, port = "127.0.0.1", int(os.environ["CH_PORT"])
probe = socket.socket()
try:
    probe.connect((host, port))
finally:
    probe.close()
control = len(attempts)

import base64, datetime as dt, uuid
import sde
import sde.layout
from sde.canonical import canonical_bytes
from sde.engines.clickhouse import ClickHouseEngine
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


@sde.entity
class Event:
    id: uuid.UUID
    name: str
    at: dt.datetime


model = sde.build_model(Event)


def layout_for(group):
    """An explicit layout, because {"auto": true} derives a PostgreSQL one and always will.

    A layout carries no dialect and the map names an engine by name rather than by dialect, so
    there is nothing in a hand-written map from which the right one could be known. Measured, the
    PostgreSQL layout does not reach ClickHouse's index handling: it fails on the first type name,
    `timestamptz`. Fails closed either way.
    """
    derived = sde.layout.default_layout(model, group, dialect="clickhouse")
    return {
        "tables": dict(derived.tables),
        "columns": {k: dict(v) for k, v in derived.columns.items()},
    }


raw = {
    "contract": sde.CONTRACT,
    "model_version": model.version,
    "map_version": 1,
    "groups": {
        g.name: {"source": {"id": g.name + "@ch", "engine": "ch-test", "layout": layout_for(g)}}
        for g in sde.colocation_groups(model)
    },
}

# Both documents, and the signed one really signed: verifying a signature is the only place this
# library calls into compiled code, so it is the only place the hook could be evaded.
private = Ed25519PrivateKey.generate()
signed = dict(raw)
signed["signature"] = {
    "alg": "ed25519",
    "key_id": "k1",
    "value": base64.b64encode(private.sign(canonical_bytes(raw))).decode(),
}
public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

sde.load_map(signed, model=model, public_key=public)
placement = sde.load_map(raw, model=model)

# Telemetry is the one thing here that would have somewhere to go, so it is on the path under
# the hook: a window is recorded and then rolled, which is where a library that shipped its
# measurements would ship them.
recorder = sde.Recorder(model.version)
with ClickHouseEngine(os.environ["SDE_CLICKHOUSE_DSN"]) as engine:
    session = sde.Session(model, placement, {"ch-test": engine}, recorder=recorder)
    session.ensure_schema()
    key = uuid.uuid4()
    session.save("Event", {"id": key, "name": "a", "at": dt.datetime.now(dt.UTC)})
    session.get("Event", {"id": key})
    window = recorder.roll()
    assert window is not None and window.shapes, "the telemetry path has to be exercised"
    recorder.acknowledge(1)

print(json.dumps({"control": control, "attempts": attempts, "forbidden": forbidden}))
'''


LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def is_the_clients_engine(attempt: list[Any], *, port: int) -> bool:
    """One recorded attempt, judged against the one address this process is entitled to reach."""
    _event, host, seen_port = attempt
    return str(host) in LOOPBACK and int(seen_port) == port


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [
        (["socket.connect", "127.0.0.1", 58123], True),
        # The case a substring test over the repr let through, which is why the pair is compared.
        (["socket.connect", "127.0.0.1", 9999], False),
        (["http.client.connect", "telemetry.smartdataengines.com", 443], False),
        (["socket.getaddrinfo", "127.0.0.1", -1], False),
    ],
)
def test_the_address_predicate_tells_the_engine_from_everything_else(
    attempt: list[Any], expected: bool
) -> None:
    """The other half of a test that passes by finding nothing."""
    assert is_the_clients_engine(attempt, port=58123) is expected


@pytest.mark.skipif(not CH_DSN, reason="set SDE_CLICKHOUSE_DSN")
def test_not_one_connection_is_attempted_to_anything_but_the_clients_own_engine(
    tmp_path: Path,
) -> None:
    """The first half of 12.6, measured rather than argued.

    "Blocked traffic and nothing broke" is the test 12.5 already has, and it cannot tell a library
    that attempts nothing from one that attempts and swallows the error. This one watches the
    attempts instead of the outcome.

    ClickHouse is the engine here on purpose: it speaks HTTP through ``http.client``, so its
    connections are visible to the hook. That turns the assertion from "empty" into "every address
    is the client's own", and an empty list would now fail it.
    """
    assert CH_DSN
    port = int(CH_DSN.rsplit(":", 1)[-1].split("/")[0])
    script = tmp_path / "probe.py"
    script.write_text(_PROBE, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env={**os.environ, "CH_PORT": str(port)},
        check=False,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])

    # The instrument works. Without this the rest is satisfied by a hook that sees nothing.
    assert report["control"] == 1, report

    # Nothing went anywhere else. Every attempt, import time included, names the client's engine -
    # host and port both, compared as a pair.
    stray = [item for item in report["attempts"] if not is_the_clients_engine(item, port=port)]
    assert stray == [], stray

    # And there were attempts to check, which is the difference between this test and a blind one.
    assert len(report["attempts"]) > report["control"], report["attempts"]
    assert report["forbidden"] == [], report["forbidden"]

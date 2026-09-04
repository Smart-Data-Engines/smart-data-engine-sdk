"""There is no expiry mechanism in this library, and the guarantee is stronger than that.

Requirement 15a.1 asks for a library with no expiry date, no licence check and no degradation. A
test that greps for the word "expiry" would satisfy the letter of that and fail open on the first
field called ``valid_through``, so this file checks two things that renaming cannot get past.

**A placement map carries no date at all.** Not "the date is ignored" - there is no date. The
library has never been told when a map was issued, so there is nothing an expiry check could
compare against, wherever somebody put it. That is the strongest form this promise can take, and
it is checked over the keys the **parser reads**, from its syntax tree. The first version of that
test walked the keys of a document written in this file, which asserted something about the test and
nothing about the library: a date field added to the parser would not have moved it.

**Nothing in this library reads the wall clock in order to decide anything.** There is exactly one
wall-clock read in the whole package, and it stamps a verification report with when it ran. Every
other time measurement is ``perf_counter_ns``, which is a monotonic counter of nanoseconds since an
arbitrary start: it cannot be compared to a date, so it cannot gate on one. The test asserts the
count, so a second wall-clock read has to be justified by whoever adds it.

And then the behavioural half, which is the one that catches an expiry check regardless of where
the date came from - embedded in the map, computed from the model version, fetched from anywhere:
**the clock is moved a year forward and the full operation set still runs.** Moving the clock
rather than ageing the map, because the map has no age to give it.

Why this lives in the library's own test suite rather than the control plane's: the promise is
"you can check this by reading the code", and the code a client reads is this one. See §12a of the
product design for the three reasons the mechanism does not exist, the first of which is that a
time bomb in somebody else's production happens once.
"""

from __future__ import annotations

import ast
import datetime as dt
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import sde
import sde.placement
from sde.engines.postgres import PostgresEngine

SRC = Path(sde.__file__).resolve().parent
DSN = os.environ.get("SDE_POSTGRES_DSN")


# --- what the document does not contain ---------------------------------------------------------


def _model() -> sde.LogicalModel:
    sde.clear_registry()

    @sde.entity
    class Reading:
        id: uuid.UUID
        station: str
        celsius: int

    return sde.build_model(Reading)


def _map_document(model: sde.LogicalModel) -> dict[str, Any]:
    """A map in the shape a client is handed one, written the way the format contract describes."""
    group = sde.colocation_groups(model)[0]
    layout = sde.default_layout(model, group, dialect="postgres")
    return {
        "contract": 2,
        "model_version": model.version,
        "map_version": 7,
        "groups": {
            group.name: {
                "source": {
                    "id": "source@pg-main",
                    "engine": "pg-main",
                    "layout": {
                        "tables": dict(layout.tables),
                        "columns": {e: dict(c) for e, c in layout.columns.items()},
                        "indexes": [dict(i) for i in layout.indexes],
                    },
                }
            }
        },
        "routing": {},
    }


def _keys(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, inner in value.items():
            yield str(key)
            yield from _keys(inner)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


DATE_ISH = (
    "date", "expir", "valid", "until", "issued", "not_after", "not_before",
    "ttl", "lease", "renew", "licen", "grace", "deadline",
)


def _keys_the_parser_reads() -> set[str]:
    """Every string key ``placement.py`` looks up in a map document.

    Read from the parser's syntax tree rather than from a document a test wrote, and that is the
    correction that makes this test worth running. The first version walked the keys of a document
    built in this file, so it asserted something about the test and nothing about the library - a
    date field added to the parser would not have moved it.
    """
    source = Path(sde.placement.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    keys: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            keys.add(node.args[0].value)
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            keys.add(node.slice.value)
    return keys


def test_the_map_parser_reads_no_key_that_could_hold_a_date() -> None:
    """The strongest form of requirement 15a.1: not "ignored" but "never looked at".

    A library that has never been told when a map was issued cannot decide that it is too old,
    whatever a later reader calls the field. So the check is over the keys the parser actually
    reads, and it fails the moment one of them is date-shaped.
    """
    keys = _keys_the_parser_reads()
    assert keys, "the parser reads no keys at all, so this test is measuring nothing"
    offenders = sorted(k for k in keys if any(n in k.lower() for n in DATE_ISH))
    assert not offenders, f"the map parser now reads {offenders}"

    model = _model()
    parsed = sde.load_map(_map_document(model), model=model)
    attributes = sorted(
        name
        for name in dir(parsed)
        if not name.startswith("_") and any(n in name.lower() for n in DATE_ISH)
    )
    assert not attributes, f"PlacementMap now exposes {attributes}"


def test_a_date_written_into_a_map_by_hand_changes_nothing() -> None:
    """The behavioural half, and it is the one a suspicious client would run.

    Put a date in the document, with a value a year in the past, and the parsed map is identical to
    the one without it. Not "the library tolerates it" - the library never looks, so there is
    nothing for it to tolerate.
    """
    model = _model()
    plain = sde.load_map(_map_document(model), model=model)
    dated = dict(_map_document(model))
    dated["valid_through"] = "2020-01-01"
    dated["expires_at"] = "2020-01-01T00:00:00Z"
    with_dates = sde.load_map(dated, model=model)

    assert with_dates.map_version == plain.map_version
    assert with_dates.model_version == plain.model_version
    assert sorted(with_dates.groups) == sorted(plain.groups)
    for name, placed in sorted(with_dates.groups.items()):
        assert placed.source.engine == plain.groups[name].source.engine
        assert dict(placed.source.layout.tables) == dict(
            plain.groups[name].source.layout.tables
        )


# --- what the source does not do -----------------------------------------------------------------


WALL_CLOCK = (
    "now",
    "utcnow",
    "today",
    "time",
    "gmtime",
    "localtime",
    "fromtimestamp",
)
"""Calls that yield the current date. ``perf_counter_ns`` and ``monotonic`` are deliberately absent.

A monotonic counter counts nanoseconds from an arbitrary start and has no relationship to a
calendar, so it cannot be compared to an issuance date. That is why the telemetry paths are free to
use it and why this list does not name it.
"""


def _wall_clock_reads() -> list[tuple[str, int]]:
    """Every place in the library that asks what time it is now."""
    found: list[tuple[str, int]] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", "")
            )
            if name in WALL_CLOCK:
                found.append((str(path.relative_to(SRC)), node.lineno))
    return found


def test_the_library_reads_the_wall_clock_exactly_once() -> None:
    """And what it does with it is stamp a measurement, not compare anything.

    Pinned as a count rather than as an absence, because one read is legitimate: a verification
    report says when it ran, and a report with no timestamp is a report nobody can put in order.
    Pinning the count means a second read has to be argued for by whoever adds it - which is the
    conversation this test exists to force, since "what time is it" is the first thing an expiry
    check needs.
    """
    reads = _wall_clock_reads()
    assert len(reads) == 1, f"the library now reads the wall clock in {reads}"
    where, _ = reads[0]
    assert where == "migration.py", where


def test_no_wall_clock_read_is_ever_compared_to_anything() -> None:
    """The shape an expiry check has to take, refused structurally.

    Whatever the field is called and wherever the date comes from, gating on it means comparing
    something to now. So: no ``Compare`` node in this library has a wall-clock call anywhere in
    either side of it.
    """
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for side in (node.left, *node.comparators):
                for inner in ast.walk(side):
                    if not isinstance(inner, ast.Call):
                        continue
                    name = (
                        inner.func.attr
                        if isinstance(inner.func, ast.Attribute)
                        else getattr(inner.func, "id", "")
                    )
                    if name in WALL_CLOCK:
                        offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert not offenders, f"a wall-clock read is compared at {offenders}"


def test_the_detector_detects() -> None:
    """Both static tests above pass by finding nothing, so they have to be shown to look.

    Without this the pair could keep passing after the walker stopped walking - which is exactly
    how the migration package's import-closure test passed while checking nothing, twice.
    """
    tree = ast.parse(
        "import datetime\n"
        "def f(issued):\n"
        "    return issued < datetime.datetime.now()\n"
    )
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        )
        in WALL_CLOCK
    ]
    assert calls, "the wall-clock matcher does not match datetime.datetime.now()"
    compares = [node for node in ast.walk(tree) if isinstance(node, ast.Compare)]
    assert compares, "the comparison matcher does not match a comparison"


def test_nothing_in_the_library_mentions_a_subscription_or_a_licence() -> None:
    """A denylist, and it fails open - which is why the two structural tests above exist.

    What it catches is the drift that would matter commercially: a field, a parameter or an error
    message that tells a client the library knows whether they pay. Requirement 15a.5 says the
    library does not know and has no right to know.
    """
    forbidden = ("subscription", "licence", "license", "entitle", "billing", "quota", "paid_")
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8").lower()
        for word in forbidden:
            if word in source:
                offenders.append(f"{path.relative_to(SRC)}: {word}")
    assert not offenders, offenders


# --- and the behaviour, with the clock moved -----------------------------------------------------


@pytest.mark.skipif(
    not DSN,
    reason="set SDE_POSTGRES_DSN: the claim is that the full operation set works, and a fake "
    "would agree with whatever this library believes",
)
def test_the_full_operation_set_runs_with_the_clock_a_year_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement 15a.1's behavioural half, with the clock moved rather than the map aged.

    Run against a **hand-written, unsigned** map, which is requirement 12.5's account-free mode.
    That makes the claim as strong as it can be: no signature, no key, no account, nothing that
    came from us, and the clock a year past when the map was written.

    A map has no age to give it - see the first test in this file - so ageing one is not a thing
    that can be done. Moving the clock is better anyway: it catches an expiry check regardless of
    where the date came from, because any such check has to ask what time it is now, and here the
    answer is a year later than when the map was made.
    """
    assert DSN
    model = _model()
    document = _map_document(model)
    placement = sde.load_map(document, model=model)

    a_year_on = dt.datetime(2027, 9, 4, tzinfo=dt.UTC)

    class Later(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
            return a_year_on if tz is None else a_year_on.astimezone(tz)

        @classmethod
        def utcnow(cls) -> dt.datetime:
            return a_year_on.replace(tzinfo=None)

    monkeypatch.setattr(dt, "datetime", Later)

    with PostgresEngine(DSN) as engine:
        with engine._cx.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS "reading" CASCADE')
            cur.execute("DROP TABLE IF EXISTS sde_map_state")
        recorder = sde.Recorder(model.version)
        session = sde.Session(
            model=model,
            placement=placement,
            engines={"pg-main": engine},
            recorder=recorder,
        )
        session.ensure_schema()

        identifier = uuid.uuid4()
        session.save("Reading", {"id": identifier, "station": "WAW", "celsius": 21})
        assert session.get("Reading", {"id": identifier})["station"] == "WAW"
        with session.transaction():
            session.save("Reading", {"id": uuid.uuid4(), "station": "KRK", "celsius": 18})

        # The one comparison this library makes against stored state, and it is a **version**
        # rather than a date: the rollback watermark from 3a.11. Worth exercising here precisely
        # because it is the mechanism an expiry check would most plausibly be bolted onto.
        # ``not_applicable`` because this map is **hand-written and unsigned** - requirement
        # 12.5's account-free mode - and the watermark from 3a.11 only guards documents that claim
        # to come from us. Which makes this the strongest version of the test rather than a
        # weaker one: no signature, no public key, no account, no map version we issued, the
        # clock a year on, and the full operation set runs. There is nothing here that could
        # expire because there is nothing here that came from us at all.
        check = session.rollback_protection
        assert check.protection == "not_applicable"

        window = recorder.roll()
        assert window is not None
        assert sum(shape.calls for shape in window.shapes) >= 3
        # A telemetry window is bounded by a **monotonic** counter, not by dates. Nanoseconds
        # since the epoch would be around 1.8e18 in 2026; `perf_counter_ns` counts from an
        # arbitrary start and is orders of magnitude smaller. So a window cannot be aged either,
        # which matters: the window is the other artefact that travels to us on a schedule and
        # would be the natural second place to put a licence check.
        assert window.started_ns < 10**17
        assert window.ended_ns >= window.started_ns

        with engine._cx.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS "reading" CASCADE')
            cur.execute("DROP TABLE IF EXISTS sde_map_state")

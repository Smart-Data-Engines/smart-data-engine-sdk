"""Our bugs must not become the client's outage - and must not be invisible either.

This library goes into somebody else's process. The promise in the README is that an internal defect
of ours is swallowed and logged rather than propagated, and the promise has a sharp edge: swallow
one thing too many and a write that never happened gets reported as success.

So these tests come in pairs. For each thing that must be swallowed there is a matching assertion
that something adjacent is *not*, because a guarantee stated only in the permissive direction is how
a library ends up quietly losing data.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest

import sde
from sde.internal import guard, internal_failures, reset_internal_failures


@pytest.fixture(autouse=True)
def _isolate() -> None:
    sde.clear_registry()
    reset_internal_failures()


def test_a_guarded_operation_that_fails_returns_none_and_is_counted() -> None:
    def boom() -> int:
        raise RuntimeError("a bug in our telemetry")

    assert guard("telemetry.aggregate", boom) is None
    assert internal_failures()["telemetry.aggregate"] == 1


def test_a_guarded_operation_that_succeeds_returns_its_value() -> None:
    assert guard("whatever", lambda: 7) == 7
    assert internal_failures() == {}


def test_failures_are_counted_per_operation_and_visible() -> None:
    # Visible on purpose. A library that swallows silently is indistinguishable from one that works,
    # so a client has to be able to see that our code is misbehaving inside their process even when
    # we cannot see it ourselves.
    for _ in range(3):
        guard("a", lambda: (_ for _ in ()).throw(ValueError()))
    guard("b", lambda: (_ for _ in ()).throw(ValueError()))
    assert internal_failures() == {"a": 3, "b": 1}


def test_keyboard_interrupt_and_system_exit_are_not_swallowed() -> None:
    # Swallowing these would make an application impossible to stop, which is a different way of
    # breaking somebody's production than the one this module prevents.
    with pytest.raises(KeyboardInterrupt):
        guard("x", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(SystemExit):
        guard("x", lambda: (_ for _ in ()).throw(SystemExit()))
    assert internal_failures() == {}


class ExplodingHandler(logging.Handler):
    """A logging handler with a bug in it, which is a thing that exists in real applications."""

    def emit(self, record: logging.LogRecord) -> None:
        raise OSError("the socket this handler writes to just closed")


class ExplodingFilter(logging.Filter):
    """A filter with a bug. Filters are the other way an exception escapes `logger.info`."""

    def filter(self, record: logging.LogRecord) -> bool:
        raise RuntimeError("a filter that raises")


def test_a_broken_logging_handler_does_not_break_an_operation() -> None:
    # The realistic version: a client ships a custom formatter, or logs to a socket that goes away.
    # A library that fails a request because its own log line could not be written has no business
    # being in someone else's process.
    #
    # Note the setLevel. Without it this test passes for the wrong reason - the sde logger inherits
    # WARNING from root, logger.info becomes a no-op, the handler is never called and nothing is
    # counted. The first version of this test did exactly that and looked like it proved something.
    logger = logging.getLogger("sde")
    handler = ExplodingHandler()
    previous = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        sde.logging.log("sde.model.built", model_version="abc")  # type: ignore[attr-defined]
        assert internal_failures()["logging.emit"] == 1
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def test_a_broken_logging_filter_does_not_break_an_operation() -> None:
    # A second route, and worth its own test because the standard library treats the two differently
    # in the surrounding code: a filter raises out of Logger.filter with nothing catching it.
    logger = logging.getLogger("sde")
    log_filter = ExplodingFilter()
    previous = logger.level
    logger.setLevel(logging.INFO)
    logger.addFilter(log_filter)
    try:
        sde.logging.log("sde.model.built", model_version="abc")  # type: ignore[attr-defined]
        assert internal_failures()["logging.emit"] == 1
    finally:
        logger.removeFilter(log_filter)
        logger.setLevel(previous)


def test_an_unknown_event_name_is_counted_rather_than_raised() -> None:
    """This used to raise, and the argument for raising was falsified by measurement.

    The argument was that an unknown name is our programming error and the test suite is where it
    should surface. It did not surface there: the names that go unnoticed live on rare paths, and a
    rare path is where no test goes. ``sde.explain.no_query_tree`` shipped missing from the
    vocabulary behind a ``pragma: no cover``, and a client planning a query on a ClickHouse without
    the new analyzer got our note to ourselves - "add it to EVENTS" - instead of a plan.

    The guard is now the static test in test_no_account.py, which reads every call site in the
    package and agrees the vocabulary in both directions. That is total, and it fails in CI rather
    than in somebody's request. Here the name is counted, so it is visible without being fatal, and
    nothing is emitted, so the vocabulary a client alerts on stays closed.
    """
    before = internal_failures().get("logging.unknown_event", 0)
    sde.logging.log("sde.made.this.up")  # type: ignore[attr-defined]
    assert internal_failures()["logging.unknown_event"] == before + 1


def test_an_unknown_event_name_reaches_no_handler() -> None:
    """The half that makes the vocabulary closed rather than merely documented."""
    logger = logging.getLogger("sde")
    seen: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record.getMessage())

    handler = Collect()
    previous = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        sde.logging.log("sde.made.this.up")  # type: ignore[attr-defined]
        sde.logging.log("sde.model.built", model_version="abc")  # type: ignore[attr-defined]
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    assert seen == ["sde.model.built"]


# --- the other direction: what must NOT be swallowed -------------------------------------------


def _model() -> sde.LogicalModel:
    @sde.entity
    class Thing:
        id: uuid.UUID
        name: str

    return sde.build_model(Thing)


def _placement(model: sde.LogicalModel) -> sde.PlacementMap:
    raw: dict[str, Any] = {
        "contract": sde.CONTRACT,
        "model_version": model.version,
        "map_version": 1,
        "groups": {
            g.name: {
                "source": {"id": f"{g.name}@e", "engine": "e", "layout": {"auto": True}}
            }
            for g in sde.colocation_groups(model)
        },
    }
    return sde.load_map(raw, model=model)


class FailingEngine:
    dialect = "failing"

    def ensure_schema(self, layout: Any, *, keys: Any) -> None:
        return None

    def insert(self, table: str, values: Any) -> None:
        raise sde.EngineError("the database refused this write")

    def get(self, table: str, key: Any) -> None:
        return None

    def transaction(self) -> Any:  # pragma: no cover - not reached in this test
        raise NotImplementedError


def test_a_failed_write_is_not_swallowed() -> None:
    # The most important assertion in this file. Everything above is about being harmless; this is
    # about not being dishonest. A write that did not happen must never look like one that did.
    model = _model()
    session = sde.Session(model, _placement(model), {"e": FailingEngine()})
    with pytest.raises(sde.EngineError, match="refused this write"):
        session.save("Thing", {"id": uuid.uuid4(), "name": "x"})
    # And it is not counted as an internal failure either: it is the client's problem to handle, not
    # ours to absorb and tally.
    assert internal_failures() == {}


def test_a_bad_map_is_not_swallowed() -> None:
    # The map decides where data is written, so a defect in it is never internal.
    model = _model()
    with pytest.raises(sde.MapError):
        sde.load_map({"contract": 99}, model=model)
    assert internal_failures() == {}


def test_a_declaration_error_is_not_swallowed() -> None:
    @sde.entity
    class NoKey:
        name: str

    with pytest.raises(sde.DeclarationError):
        sde.build_model(NoKey)
    assert internal_failures() == {}

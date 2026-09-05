"""Structured logging, with one rule: every event has a name from a closed vocabulary.

We never have access to a client's machine. When something goes wrong there, the log is the only
diagnostic we will ever see, so it has to be greppable and stable rather than prose that changes
with each refactor. Hence ``sde.``-prefixed event names, structured fields, and no interpolated
values in the event name itself.

No dependency on a logging framework, and no configuration required. The library emits through the
standard :mod:`logging` module under the ``sde`` logger and stays silent unless the application
configures a handler - a library that prints on import is a library people vendor and patch.
"""

from __future__ import annotations

import logging
from typing import Any, Final

__all__ = ["EVENTS", "log", "logger"]

logger: Final = logging.getLogger("sde")

EVENTS: Final[frozenset[str]] = frozenset(
    {
        # Once per process, and the single most useful line here: it names the model_version every
        # other artefact keys on. "Why was my map refused for model version X" is answerable from
        # this line and unanswerable without it.
        "sde.model.built",
        # A map parsed and was accepted. `signed` says which kind of document it was and
        # `forward_only` whether the rollback check applies - fields rather than two event names,
        # so that the account-free mode emits nothing the account mode does not.
        "sde.map.loaded",
        # A map was refused, with the error class and the structural reason. The exception goes to
        # the application; this goes to whoever is looking at why it will not start.
        "sde.map.rejected",
        # A signed map was accepted and is not older than any already applied here. Emitted once
        # per session, so the fields are the ones an operator wants when a start is refused later.
        "sde.map.forward_only",
        # No engine in this map can keep the bookkeeping, so a map that goes backwards cannot be
        # recognised. Not a failure - our own orderbook engine has a fixed schema and nowhere to
        # put it - but it is the one state where a documented protection is genuinely absent.
        "sde.map.rollback_unprotected",
        "sde.route.resolved",
        "sde.route.fallback",
        "sde.schema.applied",
        # extra_columns fires when a table the map describes has columns the map does not
        # name. Allowed rather than refused - a client may have added one outside SDE and
        # writes are unaffected - but logged, because the alternative to refusing is saying
        # nothing, and a schema that has quietly diverged is worth one line.
        "sde.schema.extra_columns",
        "sde.write.failed",
        # A dual-write fan-out did not reach a copy. Not an application failure: the row is in the
        # source, which is authoritative, and VERIFY is the gate that refuses to switch reads while
        # any divergence remains. This is the only place in the library that swallows a write error.
        "sde.migration.divergence",
        # One backfill chunk landed in a copy and the marker moved. Emitted per chunk rather than
        # per run, because a backfill is the one operation here that can take hours and an operator
        # watching it needs to see it move. The marker itself is a row count and never a key value:
        # a log line is the last place a client's own data should turn up.
        "sde.migration.backfill_progress",
        # The orderbook engine only. A write there is invisible to a query until flush(), and
        # flush() in local mode tears the engine down and reopens it - 3.4 ms measured. Reads flush
        # lazily, so this line is where the cost shows up and how many rows it bought.
        "sde.orderbook.flushed",
        # A ClickHouse server whose analyzer does not answer EXPLAIN QUERY TREE, so the table
        # names behind a plan come from EXPLAIN ESTIMATE instead. Not a failure - the plan is
        # returned - but the ReplacingMergeTree finding is a property of a table, so an operator
        # who was expecting one and did not get it should be able to see why.
        "sde.explain.no_query_tree",
        "sde.internal.error",
        # Telemetry. `window_closed` fires when a period ends and its aggregate is buffered for
        # the application to collect; it was called `window_sent` until somebody read the log to
        # check whether this library phones home and found a line saying it had. Nothing here
        # sends anything - there is no channel, no address and no dependency that could open one -
        # so the old name described an event that cannot happen, and a closed vocabulary exists so
        # that an alert built on a name keeps working, not so that a wrong name outlives the
        # reading of it. Renamed while the cost of renaming is zero.
        "sde.telemetry.window_closed",
        # dropped fires when the buffer is full and the oldest window is discarded - telemetry is
        # the thing that gets lost when we run out of room, never an operation.
        "sde.telemetry.dropped",
    }
)


def log(event: str, /, **fields: Any) -> None:
    """Emit one structured event.

    Nothing here can raise. The handler belongs to the client's application: it might write to a
    socket that just closed, or be a custom formatter with a bug in it. A library that fails a
    request because its own log line could not be written has no business being in somebody else's
    process.

    An unknown event name used to raise, on the argument that it is our programming error and the
    test suite is where it should surface. **Measured, it did not.** The names that go unnoticed
    are the ones on rare paths, and a rare path is precisely where no test goes:
    ``sde.explain.no_query_tree`` shipped absent from the vocabulary behind a ``pragma: no cover``,
    and a client on a ClickHouse without the new analyzer got ``ValueError: ... is not a known
    event. Add it to EVENTS ...`` - a note addressed to us, delivered to them, instead of the query
    plan, on the one path written to degrade gracefully.

    So the guard moved to where it can be total: a static test reads every ``log()`` call site in
    the package and requires the vocabulary and the code to agree in both directions. That fails in
    CI on every path rather than in production on one. Here an unknown name is counted through the
    same registry as a broken handler and nothing is emitted - so the vocabulary stays closed, a
    client can still see that it happened, and it costs them no call.
    """
    if event not in EVENTS:
        _count("logging.unknown_event")
        return
    # Checked before anything is built. Routing logs on every operation and an unconfigured logger
    # is
    # the normal case in production, so the cost of a log line nobody consumes has to be one
    # comparison rather than a dictionary construction.
    if not logger.isEnabledFor(logging.INFO):
        return
    try:
        logger.info(event, extra={"sde_event": event, "sde_fields": fields})
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        _count("logging.emit")


def _count(what: str) -> None:
    """Record one of our own failures without going through :func:`~sde.internal.guard`.

    guard() reports through log(), so calling it from here would be a loop. Same registry, by hand.
    """
    from .internal import _failures, _lock

    with _lock:
        _failures[what] = _failures.get(what, 0) + 1

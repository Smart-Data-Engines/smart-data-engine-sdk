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
        "sde.model.built",
        "sde.map.loaded",
        "sde.map.unsigned",
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
        # The orderbook engine only. A write there is invisible to a query until flush(), and
        # flush() in local mode tears the engine down and reopens it - 3.4 ms measured. Reads flush
        # lazily, so this line is where the cost shows up and how many rows it bought.
        "sde.orderbook.flushed",
        "sde.internal.error",
        # Telemetry. dropped fires when the buffer is full and the oldest window is discarded -
        # telemetry is the thing that gets lost when we run out of room, never an operation.
        "sde.telemetry.window_sent",
        "sde.telemetry.dropped",
    }
)


def log(event: str, /, **fields: Any) -> None:
    """Emit one structured event.

    An unknown event name raises, because the whole value of a closed vocabulary is that a client
    can build an alert on a name and have it keep working. That is a programming error on our side
    and the test suite is where it should surface.

    The *emission* is a different matter and is deliberately incapable of raising. The handler
    belongs to the client's application: it might write to a socket that just closed, or be a custom
    formatter with a bug in it. A library that fails a request because its own log line could not be
    written has no business being in somebody else's process.
    """
    if event not in EVENTS:
        raise ValueError(
            f"{event!r} is not a known event. Add it to EVENTS with a comment explaining when it "
            "fires - the vocabulary is closed so that alerts built on these names keep working."
        )
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
        # No guard() here, and no logging of the logging failure: guard() reports through log(), so
        # calling it would be a loop. Counted instead, through the same registry, by hand.
        from .internal import _failures, _lock

        with _lock:
            _failures["logging.emit"] = _failures.get("logging.emit", 0) + 1

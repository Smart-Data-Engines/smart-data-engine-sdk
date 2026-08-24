"""The boundary between our bugs and the client's uptime.

This library runs inside somebody else's application. A defect in our telemetry, our logging or our
diagnostics must not take down their request, and the honest way to guarantee that is to route every
purely-internal side effect through one function that cannot raise.

The hard part is not the try/except. It is deciding what counts as internal, and getting that wrong
in either direction is bad:

- Swallow too little and a bug in a counter becomes a customer's outage.
- Swallow too much and a write that never happened is reported as success, which is the worst thing
  this library could do.

So the rule is narrow and stated once here: **internal means it cannot change whether the client's
operation was performed correctly.** Emitting a log line is internal. Aggregating a telemetry window
is internal. Choosing which materialisation a read goes to is *not* - a wrong route returns wrong
data. Writing a row is not. Verifying a placement map's signature is not, because the map decides
where data is written.

When a guarded operation does fail, it is logged as ``sde.internal.error`` and the failure is
counted. A library that swallows silently is indistinguishable from one that works, so the counter
is the thing that makes this honest: it is readable, and a client who wants to alert on "the
vendor's library is quietly failing" has something to alert on.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TypeVar

__all__ = ["guard", "internal_failures", "reset_internal_failures"]

T = TypeVar("T")

_lock = threading.Lock()
_failures: dict[str, int] = {}


def internal_failures() -> dict[str, int]:
    """How many times each guarded operation has failed, by name.

    Exposed rather than hidden. Swallowing a failure and leaving no trace of it would make this
    library indistinguishable from one that works, and a client should be able to see that our code
    is misbehaving inside their process even when we cannot.
    """
    with _lock:
        return dict(_failures)


def reset_internal_failures() -> None:
    """For tests."""
    with _lock:
        _failures.clear()


def guard(what: str, operation: Callable[[], T]) -> T | None:
    """Run a purely-internal operation. Never raises.

    ``what`` names the operation and becomes the key in :func:`internal_failures`, so it should be
    stable across releases - a client may be alerting on it.
    """
    try:
        return operation()
    except BaseException as exc:
        # BaseException rather than Exception on purpose. A generator misbehaving, a recursion limit
        # or a bad __del__ inside a telemetry aggregator would otherwise escape a narrower clause
        # and reach the client's code, which is exactly what this exists to prevent.
        # KeyboardInterrupt and SystemExit are re-raised below, because swallowing those would make
        # an application impossible to stop.
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        with _lock:
            _failures[what] = _failures.get(what, 0) + 1
        _report(what, exc)
        return None


def _report(what: str, exc: BaseException) -> None:
    """Log the failure, and do not let logging failures escape either.

    Nested guarding sounds paranoid until you consider what a broken logging handler in a client's
    application does to a library that logs from inside an exception handler.
    """
    try:
        from .logging import log

        log("sde.internal.error", operation=what, error=type(exc).__name__)
    except BaseException:
        pass

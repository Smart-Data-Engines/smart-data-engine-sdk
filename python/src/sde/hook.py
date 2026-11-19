"""What a session tells the application about each operation, when the application asks.

A session given a hook calls it once for every operation it completes - with what the operation
was, where it went, how long it took, how many rows it moved and how it ended - and with nothing
the operation carried: no key, no row, no filter, no statement, no connection string and no error
message, since a driver's message can quote a key (``docs/observability.md``). The hook runs after
the operation, in the thread that ran it; a hook that raises is counted in
:func:`sde.internal_failures` and changes nothing about the operation's result.

The library depends on no tracing or metrics package. Turning a record into a span or a histogram
is a few lines of the application's own code, shown in ``docs/observability.md``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

from .errors import SdeError

# The methods a record can name; ``transaction`` is a whole transaction, from open to its end.
OPERATIONS = (
    "book_at",
    "book_stream",
    "count",
    "get",
    "save",
    "save_many",
    "scan",
    "search",
    "summarize",
    "transaction",
)


class OperationRecord(NamedTuple):
    """One completed operation, in the order TypeScript and C++ keep its fields.

    A named tuple rather than a frozen dataclass, and measured: built once per operation, the
    dataclass cost 2.6 to 4.5 µs on the reference laptop and the tuple 1.0 to 1.3, against a
    PostgreSQL round trip of 300 to 600 µs and a budget of 1 % (``tests/test_overhead.py``).
    """

    operation: str
    """The session method: one of :data:`OPERATIONS`."""
    entity: str
    """The entity, in the application's own names even when the model is hashed."""
    group: str
    """The colocation group, also in the application's names."""
    shape: str
    """The operation shape's id, as telemetry names it; empty for a transaction."""
    engine: str
    """The engine's name in the placement map - never its address."""
    dialect: str
    """``postgres``, ``clickhouse``, ``elasticsearch`` or ``orderbook``."""
    materialization: str
    """The id of the materialization that served the operation."""
    duration_ns: int
    """How long it took, on the monotonic clock. A span's start is the time it ended minus this."""
    rows: int
    """Rows written or returned; 1 for a count or a summary."""
    outcome: str
    """``ok``, the class of an error of the library's hierarchy, or ``unexpected``."""


OperationHook = Callable[[OperationRecord], None]

FIELDS: tuple[str, ...] = OperationRecord._fields


def outcome_of(error: BaseException | None) -> str:
    """``ok``, the class name of an error the library raises, or ``unexpected`` for anything else -
    a class name is all that leaves, because a message can hold a value."""
    if error is None:
        return "ok"
    if isinstance(error, SdeError):
        return type(error).__name__
    return "unexpected"

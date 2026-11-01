"""The days and instants an engine holds, read from the engine (format contract section 8b).

The contract admits a date and a timestamp from year 0001 to 9999 in UTC. ClickHouse 24.8 holds
``Date32`` and ``DateTime64`` from 1900-01-01 to 2299-12-31 only, and moves a value outside to the
nearer end without an error: measured on 1 November 2026, 1800-01-01 was stored as 1900-01-01 and
2500-06-01 12:00 as 2299-12-31 12:00, through ``VALUES`` and ``JSONEachRow`` alike, and the server's
``date_time_overflow_behavior='throw'`` did not apply to an insert. ClickHouse 26.9 holds the
contract's whole range. So the range is the server's, and it is read from the server: a conversion
moves a value as an insert does, so converting the contract's ends gives the server's
(:data:`RANGE_SQL`).

A session refuses a row with a value outside the range of an engine its write goes to, before any
engine is called, and a migration refuses to copy one. An engine that holds the contract's whole
range has no range here (``None``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Final

__all__ = ["RANGE_SQL", "TEMPORAL_KINDS", "TemporalRange", "from_engine_text", "kind_noun"]

RANGE_SQL: Final = (
    "SELECT toString(toDate32('0001-01-01')), toString(toDate32('9999-12-31')), "
    "toString(toDateTime64('0001-01-01 00:00:00', 6, 'UTC')), "
    "toString(toDateTime64('9999-12-31 23:59:59.999999', 6, 'UTC'))"
)
"""ClickHouse's own ends: the contract's, moved as an insert would move them."""

TEMPORAL_KINDS: Final = frozenset({"date", "timestamp", "timestamptz"})

_FIRST_DAY: Final = date(1, 1, 1)
_LAST_DAY: Final = date(9999, 12, 31)
_FIRST_INSTANT: Final = datetime(1, 1, 1)
_LAST_INSTANT: Final = datetime(9999, 12, 31, 23, 59, 59, 999999)


@dataclass(frozen=True)
class TemporalRange:
    """The first and last day and instant an engine holds. Instants are naive, in UTC."""

    first_day: date
    last_day: date
    first_instant: datetime
    last_instant: datetime

    def holds(self, kind: str, value: Any) -> bool:
        """Whether a value admitted for a field of ``kind`` is inside the range.

        A value reaches here in the form a filter gets (section 8b): a date as ``date``, a
        ``timestamp`` as a naive ``datetime`` in UTC wall time, a ``timestamptz`` as an aware one
        in UTC.
        """
        if kind == "date":
            return bool(self.first_day <= value <= self.last_day)
        instant = value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value
        return bool(self.first_instant <= instant <= self.last_instant)

    def describe(self) -> str:
        """The range as a refusal says it: by its days, a timestamp's as well."""
        return f"from {self.first_day.isoformat()} to {self.last_day.isoformat()}"


def kind_noun(kind: str) -> str:
    return "a date" if kind == "date" else "a timestamp"


def from_engine_text(first_day: str, last_day: str, first: str, last: str) -> TemporalRange | None:
    """The range from :data:`RANGE_SQL`'s four answers; ``None`` for the contract's whole one."""
    span = TemporalRange(
        date.fromisoformat(first_day),
        date.fromisoformat(last_day),
        datetime.fromisoformat(first),
        datetime.fromisoformat(last),
    )
    ends = (span.first_day, span.last_day, span.first_instant, span.last_instant)
    return None if ends == (_FIRST_DAY, _LAST_DAY, _FIRST_INSTANT, _LAST_INSTANT) else span

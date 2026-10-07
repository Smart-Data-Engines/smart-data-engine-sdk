"""What a row may give a field of each neutral type, and the form the value reaches an engine in.

Format contract section 8b, point 4. Until 7 October 2026 a row's values went to the driver as the
application gave them, and each driver and each engine converted them in its own way. Measured
through ``Session.save`` on PostgreSQL 15 and ClickHouse 24.8, in this library and in TypeScript:
- a decimal with more fractional digits than its column was rounded by PostgreSQL and truncated by
  ClickHouse;
- a decimal past its precision was stored by ClickHouse from here, digits changed;
- an integer outside int32 wrapped to the opposite sign in ClickHouse from TypeScript;
- a date that does not exist moved two days;
- a number became a timestamp.

The same model meant something different per engine, and moving a group changed what.

A value is admitted in the forms a filter of the same type takes (:func:`sde.query.query_value`),
so a row and a read agree about what a value is. It is passed on in the form a filter gets, so
every adapter receives one representation per type. A refusal says what the type does not hold
and never the value: a refusal is logged, and the value is the client's.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Final

from .query import QueryRefused, ReadColumn, exact_decimal, query_value

Check = Callable[[Any], Any]
"""Returns the value as engines receive it, or raises :class:`Misfit`."""


class Misfit(Exception):
    """What a field's type does not hold about a value, in words that never include it."""


_NOUNS: Final = {
    "bool": "a boolean",
    "string": "text",
    "uuid": "a UUID",
    "date": "a date",
    "timestamp": "a timestamp",
    "timestamptz": "a timestamp",
    "bytes": "bytes",
}
"""The types whose rule is a filter value's rule exactly, and what a refusal calls them."""


def _as_filter(kind: str) -> Check:
    column = ReadColumn("", kind)
    noun = _NOUNS[kind]

    def check(value: Any) -> Any:
        try:
            return query_value(column, value)
        except QueryRefused:
            raise Misfit(f"a value that is not {noun}") from None

    return check


def _integer(kind: str) -> Check:
    bound = 2**31 if kind == "int32" else 2**63

    def check(value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int):
            raise Misfit("a value that is not an integer")
        if not -bound <= value < bound:
            raise Misfit(f"an integer outside {kind}")
        return int(value)

    return check


def _float(kind: str) -> Check:
    single = kind == "float32"

    def check(value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise Misfit("a value that is not a number")
        try:
            number = float(value)
        except OverflowError:
            raise Misfit("a number outside float64") from None
        # NaN and the infinities are values both float types hold, and both engines store them.
        # What float32 cannot hold is a finite number it rounds to an infinity, or one that is not
        # zero and rounds to zero: PostgreSQL refuses both for `real`, and ClickHouse stores the
        # infinity or the zero.
        if single and number != 0.0 and math.isfinite(number):
            try:
                (rounded,) = struct.unpack("<f", struct.pack("<f", number))
            except OverflowError:
                rounded = math.inf
            if rounded == 0.0 or math.isinf(rounded):
                raise Misfit("a number outside float32")
        return number

    return check


def _decimal(kind: str) -> Check:
    precision, scale = (int(part) for part in kind[len("decimal(") : -1].split(","))
    whole = precision - scale

    def check(value: Any) -> Any:
        try:
            number = exact_decimal(value)
        except QueryRefused:
            raise Misfit("a value that is not an exact decimal") from None
        sign, digits, exponent = number.as_tuple()
        assert isinstance(exponent, int)  # finite, by exact_decimal
        if any(digits):
            # Trailing zeros say nothing about the value: 1.230 fits two fractional digits.
            length = len(digits)
            while digits[length - 1] == 0:
                length -= 1
                exponent += 1
            if -exponent > scale:
                raise Misfit(f"more than {scale} fractional digits")
            if length + exponent > whole:
                raise Misfit(f"more than {whole} integer digits")
            # Only now, with at most `precision` digits left: text this long is refused above
            # before it could cost anything.
            coefficient = 0
            for digit in digits[:length]:
                coefficient = coefficient * 10 + digit
        else:
            sign, coefficient, exponent = 0, 0, 0
        if isinstance(value, int):
            return value
        return Decimal(at_scale(sign == 1, coefficient, exponent, scale))

    return check


def at_scale(negative: bool, coefficient: int, exponent: int, scale: int) -> str:
    """``coefficient * 10**exponent`` in plain notation with exactly ``scale`` fractional digits.

    The text a decimal reaches an engine in, the same in every library: ``1.230`` and ``1.23`` and
    ``123E-2`` all become ``1.23`` in a ``decimal(12,2)``, and ``1E+3`` becomes ``1000.00``. The
    caller has checked that the value fits, so no digit is dropped and the text has at most the
    field's precision in digits.
    """
    shifted = coefficient * 10 ** (exponent + scale)
    text = str(shifted).rjust(scale + 1, "0")
    if scale:
        text = text[:-scale] + "." + text[-scale:]
    return "-" + text if negative else text


def check_for(kind: str) -> Check | None:
    """The check for a field of this neutral type, or ``None`` where every value is admitted."""
    if kind in _NOUNS:
        return _as_filter(kind)
    if kind in ("int32", "int64"):
        return _integer(kind)
    if kind in ("float32", "float64"):
        return _float(kind)
    if kind.startswith("decimal("):
        return _decimal(kind)
    # json: whatever the adapter can serialise. The section 8b rule does not reach it yet.
    return None

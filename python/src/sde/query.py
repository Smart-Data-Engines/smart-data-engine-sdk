"""Bounded logical read plans. Values stay in the application process, outside shape identities."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Protocol, cast
from uuid import UUID

from .errors import ModelPlanningError
from .schema import QUOTE

MAX_PAGE_ROWS: Final = 1000
MAX_ORDER_FIELDS: Final = 32


class QueryRefused(ModelPlanningError):
    """A logical query cannot be executed as asked; no query I/O has started."""


@dataclass(frozen=True)
class Range:
    field: str
    low: Any = None
    high: Any = None


@dataclass(frozen=True)
class ReadColumn:
    name: str
    type: str


@dataclass(frozen=True)
class ReadFilter:
    column: ReadColumn
    operation: str
    value: Any


@dataclass(frozen=True)
class ReadPlan:
    columns: tuple[ReadColumn, ...]
    filters: tuple[ReadFilter, ...]
    order: tuple[ReadColumn, ...]
    descending: bool
    after: tuple[Any, ...] | None
    limit: int


@dataclass(frozen=True)
class ScanPage:
    rows: tuple[dict[str, Any], ...]
    next_after: Mapping[str, Any] | None


class Queryable(Protocol):
    def select_rows(self, table: str, plan: ReadPlan) -> list[dict[str, Any]]: ...
    def count_rows(self, table: str, plan: ReadPlan) -> int: ...


def query_engine(engine: object) -> Queryable:
    if not all(callable(getattr(engine, name, None)) for name in ("select_rows", "count_rows")):
        raise QueryRefused("this adapter does not support logical reads (select_rows/count_rows)")
    return cast(Queryable, engine)


def _text(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise QueryRefused("query strings must contain Unicode scalar values") from None
    return value


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise QueryRefused("decimal query values require Decimal, integer or decimal text")
    if (
        isinstance(value, str)
        and re.fullmatch(
            r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value.strip()
        )
        is None
    ):
        raise QueryRefused("invalid decimal query value")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise QueryRefused("invalid decimal query value") from None
    if not number.is_finite():
        raise QueryRefused("decimal query values must be finite")
    # Check before formatting: a compact exponent must not allocate an unbounded parameter.
    exponent = number.as_tuple().exponent
    assert isinstance(exponent, int)
    scale = max(0, -exponent)
    digits = max(0, number.adjusted() + 1) + scale
    if digits > 76 or scale > 76:
        raise QueryRefused("decimal query values may use at most 76 digits")
    return number


def query_value(column: ReadColumn, value: Any) -> Any:
    """Snapshot one typed predicate/cursor value without a driver or network call."""
    if value is None:
        return None
    kind = column.type
    if kind.startswith("decimal("):
        return _decimal(value)
    if kind == "bool":
        if not isinstance(value, bool):
            raise QueryRefused("a boolean query value must be a bool")
        return value
    if kind in ("int32", "int64"):
        if isinstance(value, bool) or not isinstance(value, int):
            raise QueryRefused("an integer query value must be an integer")
        bits = 32 if kind == "int32" else 64
        if not -(2 ** (bits - 1)) <= value < 2 ** (bits - 1):
            raise QueryRefused(f"integer query value is outside {kind}")
        return value
    if kind in ("float32", "float64"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise QueryRefused("a float query value must be a finite number")
        try:
            number = float(value)
        except OverflowError:
            raise QueryRefused("float query value is outside float64") from None
        if not math.isfinite(number):
            raise QueryRefused("float query bounds must be finite")
        return number
    if kind == "string":
        if not isinstance(value, str):
            raise QueryRefused("a string query value must be text")
        return _text(value)
    if kind == "uuid":
        if isinstance(value, UUID):
            return value
        if (
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value) is None
        ):
            raise QueryRefused("a UUID query value must be UUID or canonical UUID text")
        return UUID(value)
    if kind == "date":
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            try:
                return date.fromisoformat(value)
            except ValueError:
                pass
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        raise QueryRefused("a date query value must be a date or YYYY-MM-DD text")
    if kind in ("timestamp", "timestamptz"):
        if isinstance(value, str):
            if (
                re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
                    r"(?:Z|[+-]\d{2}:\d{2}(?::\d{2})?)?",
                    value,
                )
                is None
            ):
                raise QueryRefused("timestamp query text needs at most six fractional digits")
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                raise QueryRefused("invalid timestamp query value") from None
        if not isinstance(value, datetime):
            raise QueryRefused("a timestamp query value must be datetime or ISO text")
        try:
            instant = (
                value.replace(tzinfo=UTC) if value.utcoffset() is None else value.astimezone(UTC)
            )
        except (ValueError, OverflowError):
            raise QueryRefused(
                "timestamp query value is outside UTC years 0001 through 9999"
            ) from None
        return instant.replace(tzinfo=None) if kind == "timestamp" else instant
    if kind == "bytes" and isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    raise QueryRefused(f"query predicates are not supported for {kind}")


def plan_read(
    columns: Sequence[ReadColumn],
    key: Sequence[str],
    *,
    where: Mapping[str, Any] | None = None,
    bounds: Range | None = None,
    order_by: str | None = None,
    descending: bool = False,
    after: Mapping[str, Any] | None = None,
    limit: int = 100,
    paginate: bool = True,
) -> ReadPlan:
    by_name = {column.name: column for column in columns}

    def field(name: str) -> ReadColumn:
        if not isinstance(name, str) or name not in by_name:
            raise QueryRefused("query refers to a field this entity does not declare")
        return by_name[name]

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_ROWS:
        raise QueryRefused(f"page limit must be an integer between 1 and {MAX_PAGE_ROWS}")
    if not isinstance(descending, bool):
        raise QueryRefused("descending must be a bool")
    if paginate and not key:
        raise QueryRefused("a paginated read requires an entity key")
    order_names = ([order_by] if order_by is not None else []) + [
        name for name in key if name != order_by
    ]
    if paginate and len(order_names) > MAX_ORDER_FIELDS:
        raise QueryRefused(f"a read may order by at most {MAX_ORDER_FIELDS} fields")
    order = tuple(field(name) for name in order_names) if paginate else ()
    if any(column.type in ("json", "float32", "float64") for column in order):
        raise QueryRefused("JSON and floating-point ordering are not supported by this read API")
    if any(
        column.type.startswith("decimal(")
        and int(column.type[len("decimal(") : -1].split(",")[0]) > 76
        for column in order
    ):
        raise QueryRefused("decimal ordering supports at most 76 digits")
    filters: list[ReadFilter] = []
    if where is not None:
        if not isinstance(where, Mapping) or any(not isinstance(name, str) for name in where):
            raise QueryRefused("where must map logical field names to scalar values")
        for name in sorted(where):
            column = field(name)
            if column.type == "json":
                raise QueryRefused("JSON predicates are not supported by this read API")
            filters.append(ReadFilter(column, "eq", query_value(column, where[name])))
    if bounds is not None:
        if not isinstance(bounds, Range):
            raise QueryRefused("bounds must be a Range")
        column = field(bounds.field)
        if not column.type.startswith(
            ("int32", "int64", "float32", "float64", "decimal(", "date", "timestamp")
        ):
            raise QueryRefused("this field has no range-read shape")
        if bounds.low is None and bounds.high is None:
            raise QueryRefused("a range needs at least one bound")
        for operation, value in (("ge", bounds.low), ("lt", bounds.high)):
            if value is not None:
                filters.append(ReadFilter(column, operation, query_value(column, value)))
    position = None
    if after is not None:
        if not paginate:
            raise QueryRefused("an aggregate has no pagination position")
        if not isinstance(after, Mapping) or set(after) != set(order_names):
            raise QueryRefused("after must contain exactly the complete ordering key")
        position = tuple(query_value(column, after[column.name]) for column in order)
    return ReadPlan(tuple(columns), tuple(filters), order, descending, position, limit)


def _expression(column: ReadColumn, dialect: str) -> str:
    expression = QUOTE[dialect](column.name)
    if column.type == "string" and dialect == "postgres":
        return f'({expression} COLLATE "C")'
    if column.type == "uuid" and dialect == "clickhouse":
        return f"toString({expression})"
    if column.type in ("float32", "float64"):
        return (
            f"CAST({expression} AS double precision)"
            if dialect == "postgres"
            else f"toFloat64({expression})"
        )
    return expression


def _comparison(
    column: ReadColumn,
    operator: str,
    value: Any,
    dialect: str,
    parameter: Callable[[Any], str],
) -> str:
    expression = _expression(column, dialect)
    if value is None:
        return f"{expression} IS NULL"
    if column.type.startswith("decimal("):
        _, scale_text = column.type[len("decimal(") : -1].split(",")
        scale = max(int(scale_text), max(0, -value.as_tuple().exponent))
        integer_digits = int(column.type[len("decimal(") : -1].split(",")[0]) - int(scale_text)
        if integer_digits + scale > 76:
            raise QueryRefused("decimal comparison requires more than 76 digits")
        target = (
            f"numeric(76,{scale})" if dialect == "postgres" else f"Nullable(Decimal(76,{scale}))"
        )
        expression = f"CAST({expression} AS {target})"
        bound = f"CAST({parameter(format(value, 'f'))} AS {target})"
    else:
        bound = parameter(
            str(value) if column.type == "uuid" and dialect == "clickhouse" else value
        )
    comparison = f"{expression} {operator} {bound}"
    if column.type in ("float32", "float64") and operator != "=":
        not_nan = (
            f"{expression} <> 'NaN'::double precision"
            if dialect == "postgres"
            else f"NOT isNaN({expression})"
        )
        comparison = f"({not_nan} AND {comparison})"
    return comparison


def read_sql(
    table: str,
    plan: ReadPlan,
    *,
    dialect: str,
    parameter: Callable[[Any], str],
    count: bool = False,
) -> str:
    """One native query. The parameter callback owns the driver's value representation."""
    clauses = [
        _comparison(
            predicate.column,
            {"eq": "=", "ge": ">=", "lt": "<"}[predicate.operation],
            predicate.value,
            dialect,
            parameter,
        )
        for predicate in plan.filters
    ]
    if not count and plan.after is not None:
        alternatives: list[str] = []
        operator = "<" if plan.descending else ">"
        for index, (column, value) in enumerate(zip(plan.order, plan.after, strict=True)):
            if value is not None:
                # Bind each occurrence in SQL order. Reusing a prefix would reuse a named
                # ClickHouse parameter but leave PostgreSQL's positional parameters misaligned.
                prefix = [
                    _comparison(previous, "=", plan.after[position], dialect, parameter)
                    for position, previous in enumerate(plan.order[:index])
                ]
                expression = _expression(column, dialect)
                later = _comparison(column, operator, value, dialect, parameter)
                alternatives.append(
                    "(" + " AND ".join([*prefix, f"({expression} IS NULL OR {later})"]) + ")"
                )
        clauses.append("(" + " OR ".join(alternatives) + ")" if alternatives else "FALSE")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    quote = QUOTE[dialect]
    final = " FINAL" if dialect == "clickhouse" else ""
    if count:
        expression = "CAST(count(*) AS text)" if dialect == "postgres" else "toString(count())"
        return f"SELECT {expression} AS sde_count FROM {quote(table)}{final}{where}"
    projection = ", ".join(
        f"toTimeZone({quote(column.name)}, 'UTC') AS {quote(column.name)}"
        if dialect == "clickhouse" and column.type in ("timestamp", "timestamptz")
        else quote(column.name)
        for column in plan.columns
    )
    direction = "DESC" if plan.descending else "ASC"
    order = ", ".join(
        f"{_expression(column, dialect)} {direction} NULLS LAST" for column in plan.order
    )
    return (
        f"SELECT {projection} FROM {quote(table)}{final}{where} ORDER BY {order} "
        f"LIMIT {parameter(plan.limit + 1)}"
    )


@dataclass(frozen=True)
class NumericSummary:
    count: int
    non_null_count: int
    minimum: int | Decimal | None
    maximum: int | Decimal | None
    total: int | Decimal | None
    mean: Decimal | None


class Summarizable(Protocol):
    def summarize_rows(
        self,
        table: str,
        plan: ReadPlan,
        column: ReadColumn,
    ) -> Mapping[str, Any]: ...


def summary_engine(engine: object) -> Summarizable:
    if not callable(getattr(engine, "summarize_rows", None)):
        raise QueryRefused("this adapter does not support numeric summaries (summarize_rows)")
    return cast(Summarizable, engine)


def summary_scale(column: ReadColumn, mean_scale: int) -> int:
    if isinstance(mean_scale, bool) or not isinstance(mean_scale, int) or not 0 <= mean_scale <= 38:
        raise QueryRefused("mean_scale must be an integer between 0 and 38")
    if column.type in ("int32", "int64"):
        return 0
    if column.type.startswith("decimal("):
        precision, scale = map(int, column.type[len("decimal(") : -1].split(","))
        if precision <= 56:
            return scale
    raise QueryRefused("summaries require int32, int64 or decimal precision up to 56")


def summary_sql(
    table: str,
    plan: ReadPlan,
    column: ReadColumn,
    *,
    dialect: str,
    parameter: Callable[[Any], str],
) -> str:
    scale = summary_scale(column, 0)
    quoted = QUOTE[dialect](column.name)
    target = f"numeric(76,{scale})" if dialect == "postgres" else f"Nullable(Decimal(76,{scale}))"

    def text(expression: str) -> str:
        return f"CAST({expression} AS text)" if dialect == "postgres" else f"toString({expression})"

    fields = [
        text("count(*)") + " AS sde_count",
        text(f"count({quoted})") + " AS sde_present",
        text(f"min({quoted})") + " AS sde_min",
        text(f"max({quoted})") + " AS sde_max",
        text(f"sum(CAST({quoted} AS {target}))") + " AS sde_total",
    ]
    clauses = [
        _comparison(
            item.column,
            {"eq": "=", "ge": ">=", "lt": "<"}[item.operation],
            item.value,
            dialect,
            parameter,
        )
        for item in plan.filters
    ]
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    final = " FINAL" if dialect == "clickhouse" else ""
    return f"SELECT {', '.join(fields)} FROM {QUOTE[dialect](table)}{final}{where}"


def _decimal_integer(number: Decimal, scale: int) -> int:
    sign, digits, exponent = number.as_tuple()
    assert isinstance(exponent, int)
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    shift = exponent + scale
    if shift < 0:
        divisor = int(10**-shift)
        if coefficient % divisor:
            raise ValueError("summary result has fractional digits outside the stored scale")
        coefficient //= divisor
    else:
        coefficient *= int(10**shift)
    return -coefficient if sign else coefficient


def numeric_summary(
    record: Mapping[str, Any],
    column: ReadColumn,
    mean_scale: int,
) -> NumericSummary:
    """Decode exact server text and round a rational mean independently of decimal context."""
    scale = summary_scale(column, mean_scale)
    count, present = int(record["sde_count"]), int(record["sde_present"])
    if not 0 <= present <= count:
        raise ValueError("summary counts are inconsistent")
    if present == 0:
        return NumericSummary(count, 0, None, None, None, None)
    minimum, maximum, total = (_decimal(record[key]) for key in ("sde_min", "sde_max", "sde_total"))
    unscaled = _decimal_integer(total, scale)
    numerator, denominator = abs(unscaled) * 10**mean_scale, present * 10**scale
    rounded, remainder = divmod(numerator, denominator)
    if remainder * 2 > denominator or (remainder * 2 == denominator and rounded % 2):
        rounded += 1
    mean = Decimal((int(unscaled < 0), tuple(int(digit) for digit in str(rounded)), -mean_scale))
    if column.type in ("int32", "int64"):
        return NumericSummary(
            count,
            present,
            _decimal_integer(minimum, 0),
            _decimal_integer(maximum, 0),
            _decimal_integer(total, 0),
            mean,
        )

    def scaled(value: Decimal) -> Decimal:
        coefficient = _decimal_integer(value, scale)
        return Decimal(
            (
                int(coefficient < 0),
                tuple(int(digit) for digit in str(abs(coefficient))),
                -scale,
            )
        )

    return NumericSummary(count, present, scaled(minimum), scaled(maximum), scaled(total), mean)

"""Bounded application batches, independent of migration's idempotent copy protocol."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final, Protocol, cast
from uuid import UUID

from .errors import BulkWriteRefused

MAX_BATCH_ROWS: Final = 1000
MAX_BATCH_VALUES: Final = 60_000


class BulkWritable(Protocol):
    """One native application insert, with no conflict suppression, splitting or retry."""

    def insert_many(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None: ...


def bulk_writer(engine: object) -> BulkWritable:
    if not callable(getattr(engine, "insert_many", None)):
        raise BulkWriteRefused("this adapter does not support bulk writes (insert_many)")
    return cast(BulkWritable, engine)


def batch_columns(rows: object, *, extra_columns: int = 0) -> list[str]:
    """Check the entire batch before an adapter can start work. Values are never in errors."""
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        raise BulkWriteRefused("a batch must be a sequence of row mappings")
    if len(rows) > MAX_BATCH_ROWS:
        raise BulkWriteRefused(f"a batch may contain at most {MAX_BATCH_ROWS} rows")
    if not rows:
        return []
    columns: list[str] | None = None
    for row in rows:
        if not isinstance(row, Mapping) or not row or any(not isinstance(c, str) for c in row):
            raise BulkWriteRefused("each batch row must be a nonempty mapping with string fields")
        here = sorted(row)
        if columns is None:
            columns = here
            if len(rows) * (len(columns) + extra_columns) > MAX_BATCH_VALUES:
                raise BulkWriteRefused(
                    f"a batch may contain at most {MAX_BATCH_VALUES} values, including generation"
                )
        elif here != columns:
            raise BulkWriteRefused("all batch rows must have the same fields")
    assert columns is not None
    return columns


def _snapshot(value: Any, active: set[int]) -> Any:
    if value is None or isinstance(
        value, (str, bool, int, float, Decimal, UUID, date, datetime, bytes)
    ):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    identity = id(value)
    if identity in active:
        raise BulkWriteRefused("batch values must not contain cycles")
    active.add(identity)
    try:
        if isinstance(value, Mapping) and all(isinstance(key, str) for key in value):
            return {key: _snapshot(item, active) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [_snapshot(item, active) for item in value]
        raise BulkWriteRefused("batch values must use the SDK scalar types or JSON containers")
    finally:
        active.remove(identity)


def snapshot_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    try:
        return tuple({key: _snapshot(value, set()) for key, value in row.items()} for row in rows)
    except RecursionError:
        raise BulkWriteRefused("batch values are nested too deeply") from None

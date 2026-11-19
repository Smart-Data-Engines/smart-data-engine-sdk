"""What the soak's applications write, derived from numbers, and how their timings are kept.

Every value a soak process writes follows from (language, write number), so the oracle regenerates
each row from the number alone and no value has to leave the engines. The TypeScript and C++
processes derive the same values by the same rules (``soak-worker.mjs``, ``soak_worker.cpp``).

PostgreSQL holds ``Order`` and ``Fill`` in one atomic group. Write number ``s`` is an order; when
``s % 3 == 0`` it is written in one transaction with its two fills, otherwise alone. ClickHouse
holds ``WeatherReading``; write number ``b`` is a batch of ``BATCH`` readings, ``b * BATCH`` to
``b * BATCH + BATCH - 1``. Keys put the time after the account or the station, so a read of one
account's or station's recent rows is a range of the primary key and costs the same in the last
hour of the soak as in the first.

Timings are kept in log-linear buckets: eight a doubling, from a microsecond.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

LANGUAGES = {"python": 1, "typescript": 2, "cpp": 3}
BASE = datetime(2026, 11, 20, 0, 0, 0, tzinfo=UTC)
BATCH = 20
ACCOUNTS = 3

POSTGRES_DECLARATION: dict[str, Any] = {
    "entities": [
        {
            "name": "Order",
            "fields": [
                {"name": "account", "type": "string"},
                {"name": "id", "type": "uuid"},
                {"name": "placed_at", "type": "timestamptz"},
                {"name": "price", "type": "decimal(18,8)"},
                {"name": "qty", "type": "decimal(18,8)"},
                {"name": "side", "type": "string"},
            ],
            "key": ["account", "placed_at"],
            "residency": "EU",
        },
        {
            "name": "Fill",
            "fields": [
                {"name": "account", "type": "string"},
                {"name": "at", "type": "timestamptz"},
                {"name": "id", "type": "uuid"},
                {"name": "order_id", "type": "uuid"},
                {"name": "price", "type": "decimal(18,8)"},
                {"name": "qty", "type": "decimal(18,8)"},
            ],
            "key": ["account", "at"],
            "residency": "EU",
        },
    ],
    "relations": [],
    "atomic": [["Fill", "Order"]],
    "cost_ceiling": {"amount": "500.00", "currency": "EUR"},
}

CLICKHOUSE_DECLARATION: dict[str, Any] = {
    "entities": [
        {
            "name": "WeatherReading",
            "fields": [
                {"name": "at", "type": "timestamptz"},
                {"name": "celsius", "type": "decimal(8,2)"},
                {"name": "humidity", "type": "int64"},
                {"name": "id", "type": "uuid"},
                {"name": "station", "type": "string"},
            ],
            "key": ["station", "at"],
            "residency": "EU",
        }
    ],
    "relations": [],
    "atomic": [],
    "cost_ceiling": {"amount": "500.00", "currency": "EUR"},
}

# The operations of one cycle, one a period. A write takes the next write number.
CYCLES: dict[str, tuple[str, ...]] = {
    "postgres": (
        "write", "get", "write", "get", "write", "get", "write", "get", "write", "get",
        "write", "get", "write", "get", "write", "get", "write", "get", "scan", "count",
    ),
    "clickhouse": (
        "write", "get", "get", "get", "get", "write", "get", "get", "get", "scan",
        "write", "get", "get", "get", "get", "write", "get", "get", "get", "count",
    ),
}  # fmt: skip


def account(language: str, s: int) -> str:
    return f"soak-{language}-{s % ACCOUNTS}"


def station(language: str) -> str:
    return f"soak-{language}"


def is_transaction(s: int) -> bool:
    return s % 3 == 0


def _uuid(language: str, kind: int, number: int) -> UUID:
    return UUID(f"{LANGUAGES[language]:08x}-{kind:04x}-4000-8000-{number:012x}")


def order(language: str, s: int) -> dict[str, Any]:
    return {
        "account": account(language, s),
        "id": _uuid(language, 1, s),
        "placed_at": BASE + timedelta(microseconds=s * 10),
        "price": Decimal(f"{100 + s % 50}.12345678"),
        "qty": Decimal(f"{1 + s % 5}.50000000"),
        "side": "buy" if s % 2 == 0 else "sell",
    }


def fills(language: str, s: int) -> list[dict[str, Any]]:
    placed = order(language, s)
    return [
        {
            "account": placed["account"],
            "at": placed["placed_at"] + timedelta(microseconds=part),
            "id": _uuid(language, 2, s * 2 + part),
            "order_id": placed["id"],
            "price": placed["price"],
            "qty": Decimal("0.75000000"),
        }
        for part in (1, 2)
    ]


def order_number(row: Mapping[str, Any]) -> int:
    """The write number of an order from its key, as the oracle and the scans read it."""
    delta: timedelta = row["placed_at"] - BASE
    micros = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    if micros % 10:
        raise ValueError("not an order's time")
    return micros // 10


def reading(language: str, r: int) -> dict[str, Any]:
    return {
        "at": BASE + timedelta(microseconds=r),
        "celsius": Decimal(1525 + r % 1000).scaleb(-2),
        "humidity": 30 + r % 70,
        "id": _uuid(language, 3, r),
        "station": station(language),
    }


def reading_number(row: Mapping[str, Any]) -> int:
    delta: timedelta = row["at"] - BASE
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def batch(language: str, b: int) -> list[dict[str, Any]]:
    return [reading(language, b * BATCH + i) for i in range(BATCH)]


# --- timings --------------------------------------------------------------------------------------

SUB_BUCKETS = 8


def bucket(nanoseconds: int) -> int:
    """The bucket of a duration: 0 below 1 µs, then eight a doubling of microseconds."""
    micros = nanoseconds / 1000
    if micros < 1:
        return 0
    return 1 + math.floor(math.log2(micros) * SUB_BUCKETS)


def bucket_upper_ns(index: int) -> float:
    """The largest duration a bucket holds, in nanoseconds: what a percentile reports."""
    if index == 0:
        return 1000.0
    return 1000.0 * 2 ** (index / SUB_BUCKETS)


def percentile(histogram: Mapping[int, int], fraction: float) -> float | None:
    """The upper bound of the bucket holding the ``fraction`` quantile (nearest rank)."""
    total = sum(histogram.values())
    if total == 0:
        return None
    rank = max(1, math.ceil(fraction * total))
    seen = 0
    for index in sorted(histogram):
        seen += histogram[index]
        if seen >= rank:
            return bucket_upper_ns(index)
    raise AssertionError("unreachable: the ranks add up to the total")


def merge(into: dict[int, int], histogram: Mapping[Any, int]) -> None:
    for index, count in histogram.items():
        into[int(index)] = into.get(int(index), 0) + int(count)

"""The soak's oracle: every acknowledged write is in the engine, with the values sent.

It reads each engine with a connection of the administrator and the driver alone - not with the
library that wrote the rows, so a defect in the library's reads cannot hide one in its writes - and
regenerates every expected row from its write number (``soak_traffic``). What a process did comes
from its own records: the highest number attempted, the highest acknowledged and the numbers that
failed. Below the highest acknowledged, every number is one or the other; above it, a number is
failed or was in flight when the process stopped, and is judged as failed.

A breach is any of:
- an acknowledged write missing, or present with other values;
- a transaction or a batch present in part;
- a row of a number above the highest attempted, or a row no number generates;
- in ClickHouse, more than one physical row under one key: a write sent twice. Merges remove
  such rows in the background, so this check sees only the ones not merged yet.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import soak_traffic as traffic

KEPT_BREACHES = 100


@dataclass
class Record:
    """What one process says it did."""

    attempted: int
    highest_acked: int
    failed: set[int] = field(default_factory=set)

    def acknowledged(self, number: int) -> bool:
        return number <= self.highest_acked and number not in self.failed


@dataclass
class Verdict:
    acknowledged: int = 0
    acknowledged_present: int = 0
    failed: int = 0
    failed_present: int = 0
    failed_absent: int = 0
    breach_count: int = 0
    breaches: list[dict[str, Any]] = field(default_factory=list)

    def breach(self, kind: str, number: int | None, what: str) -> None:
        self.breach_count += 1
        if len(self.breaches) < KEPT_BREACHES:
            self.breaches.append({"kind": kind, "number": number, "what": what})

    def as_dict(self) -> dict[str, Any]:
        return {
            "acknowledged": self.acknowledged,
            "acknowledged_present": self.acknowledged_present,
            "failed": self.failed,
            "failed_present": self.failed_present,
            "failed_absent": self.failed_absent,
            "breach_count": self.breach_count,
            "breaches": self.breaches,
            "passed": self.breach_count == 0,
        }


def _utc(value: Any) -> Any:
    """A naive timestamp from a driver is UTC: the columns are declared so."""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _normal(row: Mapping[str, Any]) -> dict[str, Any]:
    return {name: _utc(value) for name, value in row.items()}


def _count(verdict: Verdict, record: Record, number: int, present: bool) -> None:
    if record.acknowledged(number):
        verdict.acknowledged += 1
        if present:
            verdict.acknowledged_present += 1
        else:
            verdict.breach("acknowledged write missing", number, "not in the engine")
    else:
        verdict.failed += 1
        if present:
            verdict.failed_present += 1
        else:
            verdict.failed_absent += 1


def judge_orders(
    language: str,
    record: Record,
    orders: Iterable[Mapping[str, Any]],
    fills: Iterable[Mapping[str, Any]],
) -> Verdict:
    """PostgreSQL: orders, and the two fills of each order written in a transaction."""
    verdict = Verdict()
    found_orders: dict[int, dict[str, Any]] = {}
    for raw in orders:
        row = _normal(raw)
        try:
            number = traffic.order_number(row)
        except (KeyError, ValueError, TypeError):
            verdict.breach("unknown row", None, "an order no write number generates")
            continue
        if number in found_orders:
            verdict.breach("written twice", number, "two orders under one key")
        found_orders[number] = row
    found_fills: dict[int, list[dict[str, Any]]] = {}
    for raw in fills:
        row = _normal(raw)
        delta = row["at"] - traffic.BASE
        micros = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
        number, part = divmod(micros, 10)
        if part not in (1, 2):
            verdict.breach("unknown row", None, "a fill no write number generates")
            continue
        found_fills.setdefault(number, []).append(row)
    for number in sorted(set(found_orders) | set(found_fills)):
        if number > record.attempted:
            verdict.breach("beyond attempted", number, "a row above the highest number attempted")
    for number in range(record.attempted + 1):
        expected = traffic.order(language, number)
        present = found_orders.get(number)
        if present is not None and present != expected:
            verdict.breach("value differs", number, "the order is not the one written")
        if traffic.is_transaction(number):
            written = found_fills.get(number, [])
            expected_fills = {row["at"]: row for row in traffic.fills(language, number)}
            if any(expected_fills.get(row["at"]) != row for row in written):
                verdict.breach("value differs", number, "a fill is not the one written")
            parts = (present is not None) + len(written)
            if parts not in (0, 3):
                verdict.breach("partial transaction", number, f"{parts} of 3 rows in the engine")
            _count(verdict, record, number, parts == 3)
        else:
            if found_fills.get(number):
                verdict.breach("unknown row", number, "fills of an order written alone")
            _count(verdict, record, number, present is not None)
    return verdict


def judge_readings(
    language: str,
    record: Record,
    rows: Iterable[Mapping[str, Any]],
    physical: Mapping[int, int],
) -> Verdict:
    """ClickHouse: batches of readings, all or nothing. ``physical`` is rows a reading number,
    read without FINAL."""
    verdict = Verdict()
    found: dict[int, dict[str, Any]] = {}
    for raw in rows:
        row = _normal(raw)
        try:
            number = traffic.reading_number(row)
        except (KeyError, TypeError):
            verdict.breach("unknown row", None, "a reading no number generates")
            continue
        found[number] = row
    twice: dict[int, int] = {}
    for number, copies in physical.items():
        if copies > 1:
            batch = number // traffic.BATCH
            twice[batch] = max(twice.get(batch, 0), copies)
    for batch, copies in sorted(twice.items()):
        verdict.breach("written twice", batch, f"up to {copies} physical rows a key")
    batches = Counter(number // traffic.BATCH for number in found)
    for batch in sorted(batches):
        if batch > record.attempted:
            verdict.breach("beyond attempted", batch, "a batch above the highest number attempted")
    for batch in range(record.attempted + 1):
        rows_here = batches.get(batch, 0)
        if rows_here not in (0, traffic.BATCH):
            verdict.breach("partial batch", batch, f"{rows_here} of {traffic.BATCH} rows")
        for expected in traffic.batch(language, batch):
            number = traffic.reading_number(expected)
            present = found.get(number)
            if present is not None and present != expected:
                verdict.breach("value differs", batch, "a reading is not the one written")
                break
        _count(verdict, record, batch, rows_here == traffic.BATCH)
    return verdict


# --- reading the engines --------------------------------------------------------------------------


def read_postgres(dsn: str, namespace: str, language: str) -> tuple[list[dict[str, Any]], ...]:
    """Every order and fill of one language, with the administrator's connection."""
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    prefix = f"soak-{language}-%"
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        orders = connection.execute(
            sql.SQL(
                "SELECT account, id, placed_at, price, qty, side FROM {}.{} WHERE account LIKE %s"
            ).format(sql.Identifier(namespace), sql.Identifier("order")),
            (prefix,),
        ).fetchall()
        fills = connection.execute(
            sql.SQL(
                "SELECT account, at, id, order_id, price, qty FROM {}.{} WHERE account LIKE %s"
            ).format(sql.Identifier(namespace), sql.Identifier("fill")),
            (prefix,),
        ).fetchall()
    return [dict(row) for row in orders], [dict(row) for row in fills]


def read_clickhouse(
    dsn: str, database: str, language: str
) -> tuple[list[dict[str, Any]], dict[int, int]]:
    """Every reading of one language, logically (FINAL) and by physical row (without it)."""
    from urllib.parse import unquote, urlsplit

    import clickhouse_connect

    parts = urlsplit(dsn)
    client = clickhouse_connect.get_client(
        host=parts.hostname or "127.0.0.1",
        port=parts.port or 8123,
        username=unquote(parts.username or "default"),
        password=unquote(parts.password or ""),
        database=database,
        send_receive_timeout=600,
    )
    try:
        station = traffic.station(language)
        logical = client.query(
            "SELECT at, celsius, humidity, id, station FROM weather_reading FINAL "
            "WHERE station = {station:String}",
            parameters={"station": station},
        )
        rows = [
            dict(zip(logical.column_names, values, strict=True)) for values in logical.result_rows
        ]
        physical = client.query(
            "SELECT at, count() FROM weather_reading WHERE station = {station:String} "
            "GROUP BY at HAVING count() > 1",
            parameters={"station": station},
        )
        copies = {
            traffic.reading_number({"at": _utc(at)}): int(count)
            for at, count in physical.result_rows
        }
    finally:
        client.close()
    return rows, copies

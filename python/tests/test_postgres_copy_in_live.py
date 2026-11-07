"""PostgreSQL ``copy_in``: a backfill's and a repair's chunk, written by COPY as INSERT writes it.

docs/qualification/cutover-limits. ``copy_in`` writes a chunk by ``COPY`` and goes back to
``INSERT ... ON CONFLICT DO NOTHING`` only when a key is already there. Two forms of one write have
to be one write, so the first test writes every neutral type, with the values most likely to come
out differently from a text ``COPY`` and from a bound parameter, by ``insert_many`` into one table
and by ``copy_in`` into another, and compares the two as the server prints them.
"""

from __future__ import annotations

import datetime as dt
import os
import struct
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from sde.engines.postgres import PostgresEngine
from sde.errors import EngineError
from sde.layout import POSTGRES_TYPES

DSN = os.environ.get("SDE_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set SDE_POSTGRES_DSN to run the PostgreSQL copy_in checks against a server"
)

COLUMNS = {
    "flag": "boolean",
    "i32": "integer",
    "i64": "bigint",
    "f32": "real",
    "f64": "double precision",
    "words": "text",
    "blob": "bytea",
    "uid": "uuid",
    "day": "date",
    "naive": "timestamp",
    "aware": "timestamptz",
    "doc": "jsonb",
}


def _f32(value: float) -> float:
    single: float = struct.unpack("<f", struct.pack("<f", value))[0]
    return single


ZONE = dt.timezone(dt.timedelta(hours=5, minutes=30))
NAN, INF = float("nan"), float("inf")
EDGES: dict[str, list[Any]] = {
    "flag": [True, False],
    "i32": [-(2**31), 2**31 - 1, 0],
    "i64": [-(2**63), 2**63 - 1, 1],
    "f32": [_f32(0.1), 3.4028234663852886e38, 1.401298464324817e-45, -0.0, NAN, INF, -INF],
    "f64": [0.1, 1 / 3, 1.7976931348623157e308, 5e-324, -0.0, NAN, INF, -INF],
    "words": ["", "tab\there", "new\nline", "back\\slash", "\\N", "carriage\rreturn",
              "quote'\"", "ąę ж 😀", "x" * 10000],
    "blob": [b"", b"\x00\x01\xff", b"\\x00", b"\\N", bytes(range(256))],
    "uid": [uuid.UUID(int=0), uuid.UUID("9b2f7f1e-8a36-4c46-a1d3-3c0fd8a1b0c2")],
    "day": [dt.date(1, 1, 1), dt.date(1970, 1, 1), dt.date(9999, 12, 31)],
    "naive": [dt.datetime(1969, 12, 31, 23, 59, 59, 999999), dt.datetime(2026, 11, 7, 12, 0, 0, 1),
              dt.datetime(1, 1, 1)],
    "aware": [dt.datetime(2026, 11, 7, 12, 0, 0, 123456, tzinfo=dt.UTC),
              dt.datetime(1969, 7, 20, 20, 17, 40, 1, tzinfo=ZONE)],
    "doc": [{"a": 1, "nested": {"b": [1, 2, None]}}, [1, "x", 0.1], {}, [],
            {"esc": "tab\t \\N ą"}, {"big": 12345678901234567890}],
}


def _rows() -> list[dict[str, Any]]:
    """One row per edge value, every other column of it NULL, and a row of NULLs."""
    rows: list[dict[str, Any]] = [{"id": 0, **dict.fromkeys(COLUMNS)}]
    for name, values in EDGES.items():
        for value in values:
            rows.append({"id": len(rows), **dict.fromkeys(COLUMNS), name: value})
    return rows


@pytest.fixture()
def engine() -> Iterator[PostgresEngine]:
    assert DSN
    with PostgresEngine(DSN) as eng:
        yield eng


@pytest.fixture()
def tables(engine: PostgresEngine) -> Iterator[list[str]]:
    names = [f"copy_in_{uuid.uuid4().hex[:12]}" for _ in range(2)]
    columns = ", ".join(f'"{name}" {kind}' for name, kind in COLUMNS.items())
    with engine._cx.cursor() as cur:
        for name in names:
            cur.execute(f'CREATE TABLE "{name}" ("id" bigint PRIMARY KEY, {columns})')
    yield names
    with engine._cx.cursor() as cur:
        for name in names:
            cur.execute(f'DROP TABLE IF EXISTS "{name}"')


def _printed(engine: PostgresEngine, table: str) -> list[str]:
    with engine._cx.cursor() as cur:
        cur.execute(f'SELECT t::text FROM "{table}" t ORDER BY "id"')
        return [row[0] for row in cur.fetchall()]


def test_the_columns_cover_every_neutral_type() -> None:
    assert set(COLUMNS.values()) == set(POSTGRES_TYPES.values())
    assert set(EDGES) == set(COLUMNS)


def test_copy_in_writes_every_neutral_type_as_an_insert_writes_it(
    engine: PostgresEngine, tables: list[str]
) -> None:
    by_insert, by_copy = tables
    rows = _rows()
    engine.insert_many(by_insert, rows)
    engine.copy_in(by_copy, rows)
    printed = _printed(engine, by_copy)
    assert len(printed) == len(rows)
    assert printed == _printed(engine, by_insert)


@pytest.fixture()
def table(engine: PostgresEngine) -> Iterator[str]:
    name = f"copy_in_{uuid.uuid4().hex[:12]}"
    with engine._cx.cursor() as cur:
        cur.execute(f'CREATE TABLE "{name}" ("id" bigint PRIMARY KEY, "value" bigint)')
    yield name
    with engine._cx.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{name}"')


@pytest.fixture()
def statements(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Which form wrote: ``COPY`` for each copy, ``INSERT`` for each conflict-form statement."""
    import psycopg

    seen: list[str] = []
    real_copy, real_execute = psycopg.Cursor.copy, psycopg.Cursor.execute

    def copy(self: Any, statement: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append("COPY")
        return real_copy(self, statement, *args, **kwargs)

    def execute(self: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        if "ON CONFLICT DO NOTHING" in str(query):
            seen.append("INSERT")
        return real_execute(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "copy", copy)
    monkeypatch.setattr(psycopg.Cursor, "execute", execute)
    return seen


def _pairs(engine: PostgresEngine, table: str) -> list[tuple[int, int]]:
    with engine._cx.cursor() as cur:
        cur.execute(f'SELECT "id", "value" FROM "{table}" ORDER BY "id"')
        return [tuple(row) for row in cur.fetchall()]


def test_a_chunk_goes_in_by_copy_and_only_one_that_conflicts_by_the_conflict_form(
    engine: PostgresEngine, table: str, statements: list[str]
) -> None:
    engine.copy_in(table, [{"id": 1, "value": 10}, {"id": 2, "value": 20}])
    assert statements == ["COPY"]

    statements.clear()  # a recopied chunk, as a backfill resumed after a crash sends it
    engine.copy_in(table, [{"id": 2, "value": 99}, {"id": 3, "value": 30}])
    assert statements == ["COPY", "INSERT"]
    assert _pairs(engine, table) == [(1, 10), (2, 20), (3, 30)]

    statements.clear()  # a key twice in one chunk: the first is kept, as the conflict form keeps it
    engine.copy_in(table, [{"id": 4, "value": 40}, {"id": 4, "value": 41}])
    assert statements == ["COPY", "INSERT"]
    assert _pairs(engine, table)[-1] == (4, 40)


def test_a_conflict_inside_a_transaction_does_not_abort_it(
    engine: PostgresEngine, table: str
) -> None:
    with engine.transaction():
        engine.copy_in(table, [{"id": 1, "value": 10}])
        engine.copy_in(table, [{"id": 1, "value": 11}, {"id": 2, "value": 20}])
        engine.insert_many(table, [{"id": 3, "value": 30}])
    assert _pairs(engine, table) == [(1, 10), (2, 20), (3, 30)]


def test_a_refusal_that_is_not_a_conflict_writes_nothing_and_is_not_retried(
    engine: PostgresEngine, table: str, statements: list[str]
) -> None:
    with engine._cx.cursor() as cur:  # a closed write fence refuses this way: a CHECK
        cur.execute(f'ALTER TABLE "{table}" ADD CONSTRAINT "closed" CHECK (false)')
    statements.clear()
    with pytest.raises(EngineError, match="copying 2 rows"):
        engine.copy_in(table, [{"id": 1, "value": 10}, {"id": 2, "value": 20}])
    assert statements == ["COPY"]
    assert _pairs(engine, table) == []

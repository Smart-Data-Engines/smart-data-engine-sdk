"""What a repair of what differs asks of an engine, against PostgreSQL and ClickHouse.

``sde.reconcile`` writes a row over the row that is there (``upsert_in``), reads the rows that carry
one write generation (``stamped_range``) and reads a set of keys in one statement
(``rows_for_keys``). This file holds the three in both adapters, with a key of two columns - text
in PostgreSQL, an instant with microseconds in ClickHouse - and the bounds each adapter keeps a
statement within.

It also measures, on the server, the fact the two passes stand on: a table at a generation refuses
a write of another one. PostgreSQL holds an ``UPDATE`` of an old row to it as well. ClickHouse
checks an ``INSERT`` and not a mutation, which is why the operator admits no runtime login that
could run one.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from sde.engines import clickhouse, postgres
from sde.engines.clickhouse import ClickHouseEngine
from sde.engines.postgres import PostgresEngine
from sde.errors import EngineError
from sde.generation import EPOCH_COLUMN

POSTGRES = os.environ.get("SDE_POSTGRES_DSN")
CLICKHOUSE = os.environ.get("SDE_CLICKHOUSE_DSN")

pytestmark = pytest.mark.skipif(
    not (POSTGRES and CLICKHOUSE),
    reason="set SDE_POSTGRES_DSN and SDE_CLICKHOUSE_DSN to run the repair's engine calls",
)

BEFORE, OPERATION = 4, 5
KEY = ("a", "b")


def at(microsecond: int) -> dt.datetime:
    return dt.datetime(2026, 11, 14, 12, 0, 0, microsecond, tzinfo=dt.UTC)


def second(dialect: str, number: int) -> Any:
    """The key's second column: text where PostgreSQL orders it, an instant in ClickHouse."""
    return f"k{number:04d}" if dialect == "postgres" else at(number)


def row(dialect: str, number: int, value: int, epoch: int = BEFORE) -> dict[str, Any]:
    return {"a": number % 3, "b": second(dialect, number), "v": value, EPOCH_COLUMN: epoch}


@pytest.fixture(params=["postgres", "clickhouse"])
def table(request: pytest.FixtureRequest) -> Iterator[tuple[Any, str, str]]:
    """An engine, a table of its own keyed by two columns, and the dialect."""
    dialect = request.param
    name = f"reconcile_{uuid.uuid4().hex[:12]}"
    if dialect == "postgres":
        assert POSTGRES
        with PostgresEngine(POSTGRES) as engine:
            with engine._cx.cursor() as cur:
                cur.execute(
                    f'CREATE TABLE "{name}" (a bigint, b text, v integer, doc jsonb, '
                    f'"{EPOCH_COLUMN}" bigint NOT NULL DEFAULT 0, PRIMARY KEY (a, b))'
                )
            try:
                yield engine, name, dialect
            finally:
                with engine._cx.cursor() as cur:
                    cur.execute(f'DROP TABLE IF EXISTS "{name}"')
    else:
        assert CLICKHOUSE
        with ClickHouseEngine(CLICKHOUSE) as engine:
            engine._cx.command(
                f"CREATE TABLE `{name}` (a Int64, b DateTime64(6, 'UTC'), v Int32, "
                f"`{EPOCH_COLUMN}` Int64 DEFAULT 0) ENGINE = ReplacingMergeTree ORDER BY (a, b)"
            )
            try:
                yield engine, name, dialect
            finally:
                engine._cx.command(f"DROP TABLE IF EXISTS `{name}`")


def held(engine: Any, name: str) -> list[tuple[Any, ...]]:
    return [
        (r["a"], r["b"], r["v"], r[EPOCH_COLUMN]) for r in engine.key_range(name, KEY)
    ]


def test_a_row_is_written_over_the_row_of_its_key_and_carries_the_new_generation(
    table: tuple[Any, str, str]
) -> None:
    engine, name, dialect = table
    engine.copy_in(name, [row(dialect, n, n) for n in range(6)])
    engine.upsert_in(
        name,
        [row(dialect, 2, 222, OPERATION), row(dialect, 9, 9, OPERATION)],
        key=KEY,
    )
    assert engine.count(name) == 7
    by_number = {(r[0], r[1]): r for r in held(engine, name)}
    assert by_number[(2, second(dialect, 2))][2:] == (222, OPERATION)
    assert by_number[(0, second(dialect, 9))][2:] == (9, OPERATION)
    assert by_number[(1, second(dialect, 1))][2:] == (1, BEFORE)
    with pytest.raises(EngineError, match="without their key"):
        engine.upsert_in(name, [{"a": 1, "v": 1, EPOCH_COLUMN: OPERATION}], key=KEY)


def test_the_rows_of_one_generation_are_read_in_key_order_page_by_page(
    table: tuple[Any, str, str]
) -> None:
    engine, name, dialect = table
    engine.copy_in(name, [row(dialect, n, n) for n in range(9)])
    engine.upsert_in(name, [row(dialect, n, -n, OPERATION) for n in (7, 1, 4, 8)], key=KEY)
    everything = engine.stamped_range(name, KEY, epoch=OPERATION)
    keys = [(r["a"], r["b"]) for r in everything]
    assert keys == sorted(keys) and {r["v"] for r in everything} == {-7, -1, -4, -8}
    assert all(r[EPOCH_COLUMN] == OPERATION for r in everything)
    pages: list[tuple[Any, ...]] = []
    after: tuple[Any, ...] | None = None
    while True:
        page = engine.stamped_range(name, KEY, epoch=OPERATION, after=after, limit=3)
        if not page:
            break
        pages.extend((r["a"], r["b"]) for r in page)
        after = pages[-1]
    assert pages == keys
    assert engine.stamped_range(name, KEY, epoch=OPERATION + 1) == []


def test_a_set_of_keys_is_read_whole_and_a_key_that_is_not_there_is_left_out(
    table: tuple[Any, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, name, dialect = table
    engine.copy_in(name, [row(dialect, n, n) for n in range(40)])
    wanted = [(n % 3, second(dialect, n)) for n in range(0, 40, 2)]
    absent = [(7, second(dialect, 1)), (0, second(dialect, 1))]
    # Past one statement's bound in each adapter, so the set goes as several.
    monkeypatch.setattr(postgres, "_PARAMETERS", 10)
    monkeypatch.setattr(clickhouse, "_KEYS_A_STATEMENT", 6)
    found = engine.rows_for_keys(name, KEY, [*wanted, *absent])
    assert sorted((r["a"], r["b"]) for r in found) == sorted(wanted)
    assert sorted(r["v"] for r in found) == list(range(0, 40, 2))
    assert engine.rows_for_keys(name, KEY, []) == []
    with pytest.raises(EngineError, match="a key has 1 values"):
        engine.rows_for_keys(name, KEY, [(1,)])


def test_a_large_set_of_long_keys_stays_within_one_statements_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ClickHouse bounds a statement's text, and a key is part of it."""
    assert CLICKHOUSE
    name = f"reconcile_{uuid.uuid4().hex[:12]}"
    with ClickHouseEngine(CLICKHOUSE) as engine:
        engine._cx.command(
            f"CREATE TABLE `{name}` (k String, v Int32) ENGINE = ReplacingMergeTree ORDER BY k"
        )
        try:
            keys = [f"{n:03d}" + "x" * 900 for n in range(30)]
            engine.copy_in(name, [{"k": key, "v": n} for n, key in enumerate(keys)])
            monkeypatch.setattr(clickhouse, "_KEY_TEXT_A_STATEMENT", 2000)
            sent: list[str] = []
            query = engine._cx.query

            def recorded(sql: str, *args: Any, **kwargs: Any) -> Any:
                sent.append(sql)
                return query(sql, *args, **kwargs)

            monkeypatch.setattr(engine._cx, "query", recorded)
            found = engine.rows_for_keys(name, ["k"], [(key,) for key in keys])
            assert sorted(r["v"] for r in found) == list(range(30))
            assert len(sent) == 10, "three keys of 903 characters pass 2000, so three a statement"
        finally:
            engine._cx.command(f"DROP TABLE IF EXISTS `{name}`")


def test_clickhouse_reads_one_generation_by_the_newest_version_of_each_key() -> None:
    """Two versions of one key, both of the generation, in parts that have not merged. Read
    without ``FINAL`` they are two rows, and a page that ends between them starts the next after
    their key: the newer one, which is the row, would never be read."""
    assert CLICKHOUSE
    name = f"reconcile_{uuid.uuid4().hex[:12]}"
    with ClickHouseEngine(CLICKHOUSE) as engine:
        engine._cx.command(
            f"CREATE TABLE `{name}` (a Int64, v Int32, `{EPOCH_COLUMN}` Int64 DEFAULT 0) "
            f"ENGINE = ReplacingMergeTree ORDER BY a"
        )
        try:
            engine._cx.command(f"SYSTEM STOP MERGES `{name}`")
            engine.copy_in(name, [{"a": 1, "v": 10, EPOCH_COLUMN: OPERATION}])
            engine.copy_in(name, [{"a": 1, "v": 11, EPOCH_COLUMN: OPERATION}])
            engine.copy_in(name, [{"a": 2, "v": 20, EPOCH_COLUMN: BEFORE}])
            engine.copy_in(name, [{"a": 2, "v": 21, EPOCH_COLUMN: OPERATION}])
            engine.copy_in(name, [{"a": 3, "v": 30, EPOCH_COLUMN: OPERATION}])
            engine.copy_in(name, [{"a": 3, "v": 31, EPOCH_COLUMN: OPERATION + 1}])
            stored = engine._cx.query(f"SELECT count() FROM `{name}`").result_rows[0][0]
            assert stored == 6, "the versions merged, so this test would pass without FINAL"
            pages: list[tuple[int, int]] = []
            after: tuple[Any, ...] | None = None
            while True:
                page = engine.stamped_range(name, ["a"], epoch=OPERATION, after=after, limit=1)
                if not page:
                    break
                pages.extend((r["a"], r["v"]) for r in page)
                after = (page[-1]["a"],)
            # Key 3's newest version carries a later generation, so it is not one of these rows.
            assert pages == [(1, 11), (2, 21)]
            found = engine.rows_for_keys(name, ["a"], [(1,), (2,), (3,)])
            assert sorted((r["a"], r["v"]) for r in found) == [(1, 11), (2, 21), (3, 31)]
        finally:
            engine._cx.command(f"SYSTEM START MERGES `{name}`")
            engine._cx.command(f"DROP TABLE IF EXISTS `{name}`")


def test_clickhouse_streams_a_table_in_key_order_by_the_newest_version_of_each_key() -> None:
    """What a cutover's pass reads its frozen source by: one statement, chunked here."""
    assert CLICKHOUSE
    name = f"reconcile_{uuid.uuid4().hex[:12]}"
    with ClickHouseEngine(CLICKHOUSE) as engine:
        engine._cx.command(
            f"CREATE TABLE `{name}` (a Int64, b DateTime64(6, 'UTC'), v Int32, "
            f"`{EPOCH_COLUMN}` Int64 DEFAULT 0) ENGINE = ReplacingMergeTree ORDER BY (a, b)"
        )
        try:
            engine._cx.command(f"SYSTEM STOP MERGES `{name}`")
            for start in (20, 0, 10):  # three parts, written out of key order
                engine.copy_in(name, [row("clickhouse", n, n) for n in range(start, start + 10)])
            engine.copy_in(name, [row("clickhouse", 7, 700, OPERATION)])  # a newer version
            chunks = list(engine.key_stream(name, KEY, chunk_rows=7))
            assert [len(chunk) for chunk in chunks] == [7, 7, 7, 7, 2]
            rows = [r for chunk in chunks for r in chunk]
            assert rows == engine.key_range(name, KEY)
            keys = [(r["a"], r["b"]) for r in rows]
            assert keys == sorted(keys) and len(set(keys)) == 30
            assert next(r for r in rows if r["b"] == at(7))["v"] == 700
            assert rows[0]["b"].tzinfo is not None
            # Stopped early and closed: the adapter is free for the next call, and the next stream.
            stream = engine.key_stream(name, KEY, chunk_rows=7)
            assert len(next(stream)) == 7
            stream.close()
            assert engine._usage.operation_owner is None and engine.count(name) == 30
            assert sum(len(chunk) for chunk in engine.key_stream(name, KEY, chunk_rows=1000)) == 30
            with pytest.raises(EngineError, match="has none"):
                next(engine.key_stream(name, KEY, chunk_rows=0))
            with pytest.raises(EngineError, match=r"key stream of .* failed"):
                next(engine.key_stream(name + "_absent", KEY, chunk_rows=5))
            assert engine._usage.operation_owner is None
        finally:
            engine._cx.command(f"SYSTEM START MERGES `{name}`")
            engine._cx.command(f"DROP TABLE IF EXISTS `{name}`")


def test_postgres_writes_a_chunk_past_one_statements_parameters_as_several(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A statement binds at most 65535 values, and a chunk of 20 000 rows of four columns is past
    it. Both forms that bind a chunk - the write over a row and ``copy_in``'s conflict form - send
    it as several statements in one transaction, so a refusal in a later one leaves nothing."""
    assert POSTGRES
    name = f"reconcile_{uuid.uuid4().hex[:12]}"
    with PostgresEngine(POSTGRES) as engine:
        with engine._cx.cursor() as cur:
            cur.execute(
                f'CREATE TABLE "{name}" '
                f"(a bigint PRIMARY KEY, v integer CHECK (v <> 13), doc jsonb)"
            )
        try:
            monkeypatch.setattr(postgres, "_PARAMETERS", 9)  # three rows of three columns
            rows = [{"a": n, "v": n, "doc": {"n": [n]}} for n in range(10)]
            engine.upsert_in(name, rows, key=["a"])
            engine.upsert_in(name, [{**r, "v": r["v"] + 100} for r in rows[:7]], key=["a"])
            assert [r["v"] for r in engine.key_range(name, ["a"])] == [
                100, 101, 102, 103, 104, 105, 106, 7, 8, 9
            ]
            assert engine.get(name, {"a": 3})["doc"] == {"n": [3]}
            with pytest.raises(EngineError):
                engine.upsert_in(
                    name, [{"a": n, "v": 13 if n == 8 else -1, "doc": None} for n in range(10)],
                    key=["a"],
                )
            assert [r["v"] for r in engine.key_range(name, ["a"])][:3] == [100, 101, 102]
            with pytest.raises(EngineError, match="rows of their key alone"):
                engine.upsert_in(name, [{"a": 1}], key=["a"])
            # copy_in: a key that is there sends the chunk by the conflict form, which keeps it.
            engine.copy_in(name, [{"a": n, "v": 500, "doc": None} for n in range(5, 25)])
            after = {r["a"]: r["v"] for r in engine.key_range(name, ["a"])}
            assert after[5] == 105 and after[9] == 9 and after[10] == after[24] == 500
            assert len(after) == 25
        finally:
            with engine._cx.cursor() as cur:
                cur.execute(f'DROP TABLE IF EXISTS "{name}"')


def test_a_table_at_a_generation_refuses_a_write_of_another(table: tuple[Any, str, str]) -> None:
    """The fact a repair of what differs stands on, asked of the server.

    The table is at the operation's generation, by the two constraints the write-fence protocol
    keeps. Whatever is written or changed then carries that generation, so the rows that carry it
    are the rows a second comparison has to read. A deletion carries nothing, and is in the count.
    """
    engine, name, dialect = table
    engine.copy_in(name, [row(dialect, n, n) for n in range(4)])
    lower, upper = f"{EPOCH_COLUMN} >= {OPERATION}", f"{EPOCH_COLUMN} <= {OPERATION}"
    if dialect == "postgres":
        with engine._cx.cursor() as cur:
            cur.execute(f'ALTER TABLE "{name}" ADD CONSTRAINT lo CHECK ({lower}) NOT VALID')
            cur.execute(f'ALTER TABLE "{name}" ADD CONSTRAINT hi CHECK ({upper}) NOT VALID')
    else:
        engine._cx.command(f"ALTER TABLE `{name}` ADD CONSTRAINT lo CHECK {lower}")
        engine._cx.command(f"ALTER TABLE `{name}` ADD CONSTRAINT hi CHECK {upper}")
    with pytest.raises(EngineError):
        engine.copy_in(name, [row(dialect, 20, 1, BEFORE)])
    with pytest.raises(EngineError):
        engine.upsert_in(name, [row(dialect, 1, 99, BEFORE)], key=KEY)
    engine.upsert_in(name, [row(dialect, 1, 99, OPERATION)], key=KEY)
    stamped = engine.stamped_range(name, KEY, epoch=OPERATION)
    assert [(r["v"], r[EPOCH_COLUMN]) for r in stamped] == [(99, OPERATION)]
    if dialect == "postgres":
        import psycopg

        # An UPDATE of a row written before is held to the generation too: a NOT VALID check
        # leaves the old rows alone and judges every new version of one.
        with pytest.raises(psycopg.errors.CheckViolation), engine._cx.transaction():
            engine._cx.execute(f'UPDATE "{name}" SET v = 0 WHERE a = 0')
        with engine._cx.transaction():
            engine._cx.execute(
                f'UPDATE "{name}" SET v = 0, "{EPOCH_COLUMN}" = {OPERATION} WHERE v = 3'
            )
            engine._cx.execute(f'DELETE FROM "{name}" WHERE v = 2')
        assert engine.count(name) == 3
        assert sorted(r["v"] for r in engine.stamped_range(name, KEY, epoch=OPERATION)) == [0, 99]
    else:
        # ClickHouse 24.8 checks a constraint on INSERT and not on a mutation: this is the one
        # write the generation does not mark, and no runtime login the operator admits may run it
        # (only SELECT and INSERT are granted; test_operator_native_live.py holds that).
        engine._cx.command(
            f"ALTER TABLE `{name}` UPDATE v = 0 WHERE v = 3", settings={"mutations_sync": 2}
        )
        assert [r["v"] for r in engine.stamped_range(name, KEY, epoch=OPERATION)] == [99]
        assert (0, BEFORE) in {(r[2], r[3]) for r in held(engine, name)}

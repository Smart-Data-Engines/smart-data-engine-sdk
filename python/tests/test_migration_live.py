"""Backfill and verify against real engines, in every direction a group can move.

The logic is covered against fakes in ``test_migration.py``. What a fake cannot check is everything
here that is SQL, and three of those are claims a fake would have agreed with while being wrong:

- **keyset pagination over a composite key.** Both adapters emit a row-value comparison,
  ``(a, b) > (...)``, and the syntax is not the same in the two engines. A hand-rolled disjunction
  over the key's columns would pass a fake and skip rows here;
- **an idempotent copy, from each engine's own key semantics.** PostgreSQL skips a duplicate with a
  conflict clause; ClickHouse collapses one at merge time and shows it under ``FINAL``. Those are
  not the same mechanism and the backfill depends on both of them behaving as one;
- **two drivers agreeing on a value.** ``verify`` compares rows read through psycopg with rows read
  through clickhouse-connect, so a copy across engines fails the moment the two disagree about a
  type. That dependency is the reason ``test_engine_agreement.py`` exists, and this is its second
  consumer.

Parameterised over all four directions rather than the interesting two. A migration within one
engine is a real operation - a group moving to a different layout - and it is also the case where a
mistake in the SQL is easiest to see.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

import sde

PG_DSN = os.environ.get("SDE_POSTGRES_DSN")
CH_DSN = os.environ.get("SDE_CLICKHOUSE_DSN")

SUFFIX = uuid.uuid4().hex[:8]
SOURCE_TABLE = f"mig_src_{SUFFIX}"
TARGET_TABLE = f"mig_dst_{SUFFIX}"


@pytest.fixture(autouse=True)
def _isolate() -> None:
    sde.clear_registry()


def _open(dialect: str) -> Any:
    if dialect == "postgres":
        if not PG_DSN:
            pytest.skip("set SDE_POSTGRES_DSN for the PostgreSQL half")
        from sde.engines.postgres import PostgresEngine

        return PostgresEngine(PG_DSN)
    if not CH_DSN:
        pytest.skip("set SDE_CLICKHOUSE_DSN for the ClickHouse half")
    from sde.engines.clickhouse import ClickHouseEngine

    return ClickHouseEngine(CH_DSN)


def _drop(engine: Any, table: str) -> None:
    if engine.dialect == "postgres":
        with engine._cx.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS "{table}"')
    else:
        engine._cx.command(f'DROP TABLE IF EXISTS "{table}"')


def _delete(engine: Any, table: str, where: str) -> None:
    """One row out of the copy, by hand. Deliberate damage is the only way to test a gate."""
    if engine.dialect == "postgres":
        with engine._cx.cursor() as cur:
            cur.execute(f'DELETE FROM "{table}" WHERE {where}')
    else:
        engine._cx.command(
            f'DELETE FROM "{table}" WHERE {where}', settings={"mutations_sync": 2}
        )


@pytest.fixture(
    params=[
        ("postgres", "postgres"),
        ("clickhouse", "clickhouse"),
        ("postgres", "clickhouse"),
        ("clickhouse", "postgres"),
    ],
    ids=["pg-to-pg", "ch-to-ch", "pg-to-ch", "ch-to-pg"],
)
def pair(request: pytest.FixtureRequest) -> Iterator[tuple[Any, Any]]:
    source_dialect, target_dialect = request.param
    source = _open(source_dialect)
    target = source if source_dialect == target_dialect else _open(target_dialect)
    source.connect()
    target.connect()
    for engine, table in ((source, SOURCE_TABLE), (target, TARGET_TABLE)):
        _drop(engine, table)
    _drop(target, sde.BACKFILL_TABLE)
    try:
        yield source, target
    finally:
        for engine, table in ((source, SOURCE_TABLE), (target, TARGET_TABLE)):
            _drop(engine, table)
        _drop(target, sde.BACKFILL_TABLE)
        source.close()
        if target is not source:
            target.close()


def _model() -> sde.LogicalModel:
    """No timestamp column, deliberately.

    PostgreSQL keeps six sub-second digits and ClickHouse three, so a timestamp column would be
    refused before the copy - which is its own test, in ``test_migration.py``. Here the subject is
    the copy itself, so the model stays inside what both engines represent identically.
    """

    @sde.entity
    class Reading:
        tenant: sde.Int32
        seq: sde.Int32
        station: str

        class Meta:
            key = ["tenant", "seq"]

    return sde.build_model(Reading)


def _session(model: sde.LogicalModel, source: Any, target: Any) -> tuple[sde.Session, str]:
    group = sde.colocation_groups(model)[0].name
    columns = {
        "Reading": {
            "tenant": "integer" if source.dialect == "postgres" else "Int32",
            "seq": "integer" if source.dialect == "postgres" else "Int32",
            "station": "text" if source.dialect == "postgres" else "String",
        }
    }
    target_columns = {
        "Reading": {
            "tenant": "integer" if target.dialect == "postgres" else "Int32",
            "seq": "integer" if target.dialect == "postgres" else "Int32",
            "station": "text" if target.dialect == "postgres" else "String",
        }
    }
    raw: dict[str, Any] = {
        "contract": sde.MAP_CONTRACT,
        "model_version": model.version,
        "map_version": 1,
        "groups": {
            group: {
                "source": {
                    "id": "src",
                    "engine": "source",
                    "layout": {
                        "tables": {"Reading": SOURCE_TABLE},
                        "columns": columns,
                    },
                },
                "derived": [
                    {
                        "id": "dst",
                        "engine": "target",
                        "layout": {
                            "tables": {"Reading": TARGET_TABLE},
                            "columns": target_columns,
                        },
                        "lag_budget_ms": 30_000,
                    }
                ],
                "also_write": ["dst"],
            }
        },
    }
    session = sde.Session(
        model, sde.load_map(raw, model=model), {"source": source, "target": target}
    )
    session.ensure_schema()
    return session, group


def _fill(session: sde.Session, rows: int) -> None:
    """Rows straight into the source, bypassing the fan-out.

    Which is what the state before a migration looks like: these are the rows that existed when
    dual-write began, and the backfill's whole job is to be the thing that moves them.
    """
    placement = session.placement.placement_of(
        sde.colocation_groups(session.model)[0].name
    )
    engine = session.engines[placement.source.engine]
    for n in range(1, rows + 1):
        engine.insert(SOURCE_TABLE, {"tenant": 1 + n % 3, "seq": n, "station": f"s{n}"})


def test_a_group_is_copied_in_chunks_and_the_copy_verifies(pair: tuple[Any, Any]) -> None:
    source, target = pair
    model = _model()
    session, group = _session(model, source, target)
    _fill(session, 25)

    progress = sde.backfill(session, group, chunk_rows=7)
    assert progress.complete
    assert progress.rows_this_run == 25
    assert target.count(TARGET_TABLE) == 25

    report = sde.verify(session, group, chunk_rows=7)
    assert report.matched, report.for_a_human()
    assert report.chunks_compared == 4
    assert report.rows_source == report.rows_target == 25


def test_the_marker_survives_a_new_adapter_and_the_backfill_resumes(
    pair: tuple[Any, Any],
) -> None:
    """The marker is durable because it is a table in the target, and this is what that buys.

    A fresh adapter and a fresh session, which is what an operator re-running an interrupted job
    actually has. Anything kept in the process would have looked identical up to here.
    """
    source, target = pair
    model = _model()
    session, group = _session(model, source, target)
    _fill(session, 20)

    first = sde.backfill(session, group, chunk_rows=5, stop_after=2)
    assert not first.complete
    assert first.rows_this_run == 10

    sde.clear_registry()
    again = _model()
    resumed, group = _session(again, source, target)
    second = sde.backfill(resumed, group, chunk_rows=5)
    assert second.complete
    assert second.rows_this_run == 10
    assert target.count(TARGET_TABLE) == 20
    assert sde.verify(resumed, group, chunk_rows=5).matched


def test_a_recopied_chunk_leaves_one_row_in_each_engines_own_way(
    pair: tuple[Any, Any],
) -> None:
    """The idempotence the write-then-marker ordering depends on, from two different mechanisms.

    PostgreSQL refuses the duplicate through the primary key the layout created; ClickHouse accepts
    it and collapses it, which `count()` sees because it asks with `FINAL`. Neither is a property
    of this module and the backfill relies on both.
    """
    source, target = pair
    model = _model()
    session, group = _session(model, source, target)
    _fill(session, 12)

    assert sde.backfill(session, group, chunk_rows=12).complete
    marker = target.backfill_marker(materialization="dst", entity="Reading")
    assert marker == 12

    # The crash window, reproduced: the chunk landed and the marker did not move.
    target.record_backfill_marker(materialization="dst", entity="Reading", rows=0)
    _drop(target, sde.BACKFILL_TABLE)
    assert target.backfill_marker(materialization="dst", entity="Reading") == 0

    assert sde.backfill(session, group, chunk_rows=12).complete
    assert target.count(TARGET_TABLE) == 12
    assert sde.verify(session, group, chunk_rows=12).matched


def test_a_row_deleted_from_the_copy_stops_the_migration_and_names_the_row(
    pair: tuple[Any, Any],
) -> None:
    """A gate is only a gate if damage makes it refuse. The damage is done in SQL, outside us."""
    source, target = pair
    model = _model()
    session, group = _session(model, source, target)
    _fill(session, 15)
    assert sde.backfill(session, group, chunk_rows=5).complete

    _delete(target, TARGET_TABLE, "seq = 7")

    report = sde.verify(session, group, chunk_rows=5)
    assert not report.matched
    assert report.chunks_mismatched == 1
    assert report.tail_rows_missing_in_target == 0
    assert report.differences[0].key["seq"] == 7
    assert report.differences[0].absent


def test_a_write_the_fan_out_lost_is_caught_above_the_marker(pair: tuple[Any, Any]) -> None:
    """Task 12.11 against real engines: a row deliberately removed from the copy after the switch
    to dual write, which is the failure a migration must never let past.

    ``tenant`` is 4 rather than any value ``_fill`` used, and that is the point rather than a
    detail. The key is ``(tenant, seq)``, so a row written during the migration only lands *above*
    the marker if its key sorts above every existing one - and with a smaller tenant it would land
    below and be counted against the chunks instead. Same refusal either way; different attribution,
    and the unit suite pins that case too.
    """
    source, target = pair
    model = _model()
    session, group = _session(model, source, target)
    _fill(session, 10)
    assert sde.backfill(session, group, chunk_rows=10).complete

    session.save("Reading", {"tenant": 4, "seq": 999, "station": "arrived-by-fan-out"})
    assert target.count(TARGET_TABLE) == 11
    _delete(target, TARGET_TABLE, "seq = 999")

    report = sde.verify(session, group, chunk_rows=10)
    assert not report.matched
    assert report.chunks_mismatched == 0
    assert report.tail_rows_read == 1
    assert report.tail_rows_missing_in_target == 1


def test_a_value_changed_in_the_copy_is_found_and_the_column_named(
    pair: tuple[Any, Any],
) -> None:
    source, target = pair
    model = _model()
    session, group = _session(model, source, target)
    _fill(session, 6)
    assert sde.backfill(session, group, chunk_rows=6).complete

    _delete(target, TARGET_TABLE, "seq = 3")
    target.insert(TARGET_TABLE, {"tenant": 1, "seq": 3, "station": "not-what-was-written"})

    report = sde.verify(session, group, chunk_rows=6)
    assert not report.matched
    assert report.differences[0].columns == ("station",)


def test_an_untouched_engine_reports_no_marker_and_creates_the_table(
    pair: tuple[Any, Any],
) -> None:
    """`max()` over nothing, which the two engines answer differently in raw SQL.

    PostgreSQL returns null and ClickHouse returns 0 for an empty aggregate over Int64. Unlike the
    map watermark, both readings mean the same thing here - nothing has been copied - so this is
    the one place that quirk costs nothing, and it is worth an assertion rather than a comment.
    """
    _, target = pair
    assert target.backfill_marker(materialization="dst", entity="Reading") == 0
    assert target.backfill_marker(materialization="dst", entity="Reading") == 0
    target.record_backfill_marker(materialization="dst", entity="Reading", rows=3)
    target.record_backfill_marker(materialization="dst", entity="Reading", rows=1)
    assert target.backfill_marker(materialization="dst", entity="Reading") == 3, (
        "append-only, and the answer is max() - a stale row cannot lower the marker"
    )

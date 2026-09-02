"""The rollback protection against real engines, because the bookkeeping is SQL.

The logic is covered against fakes in ``test_rollback_protection.py``. What a fake cannot check is
the half that is a table: `CREATE TABLE IF NOT EXISTS` twice, `max()` over an empty table, a
timestamp default, and an append that two engines with very different ideas about keys and updates
both have to accept.

The last of those is the reason this file exists rather than one engine's slice being enough. The
design is append-only with `max()` **because** ClickHouse enforces no key - so the claim worth
testing is that both engines answer the same question the same way, and that claim needs both.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest

import sde

PG_DSN = os.environ.get("SDE_POSTGRES_DSN")
CH_DSN = os.environ.get("SDE_CLICKHOUSE_DSN")


@pytest.fixture(autouse=True)
def _isolate() -> None:
    sde.clear_registry()


def _model() -> sde.LogicalModel:
    @sde.entity
    class Reading:
        id: uuid.UUID
        station: str

    return sde.build_model(Reading)


def _map(model: sde.LogicalModel, *, version: int, engine: str) -> sde.PlacementMap:
    raw: dict[str, Any] = {
        "contract": sde.CONTRACT,
        "model_version": model.version,
        "map_version": version,
        "groups": {
            group.name: {
                "source": {
                    "id": f"{group.name}@{engine}",
                    "engine": engine,
                    "layout": {"auto": True},
                }
            }
            for group in sde.colocation_groups(model)
        },
    }
    return replace(sde.load_map(raw, model=model), signed=True)


def _sql(engine: Any, statement: str, **settings: Any) -> Any:
    """Run one statement against whichever engine the fixture produced.

    Reaching into the adapter's connection on purpose and in one place. These tests are about the
    table the adapter keeps, so they need to see it from outside the adapter - and putting a public
    "run arbitrary SQL" method on an engine to make a test tidier would be handing every client one.
    """
    if engine.dialect == "postgres":
        with engine._cx.cursor() as cur:
            cur.execute(statement)
            return cur.fetchone() if cur.description else None
    result = engine._cx.command(statement, settings=settings or None)
    return result


def _count(engine: Any) -> int:
    if engine.dialect == "postgres":
        row = _sql(engine, f'SELECT count(*) FROM "{sde.WATERMARK_TABLE}"')
        return int(row[0]) if row else 0
    result = engine._cx.query(f'SELECT count() FROM "{sde.WATERMARK_TABLE}"')
    return int(result.result_rows[0][0])


@pytest.fixture(params=["postgres", "clickhouse"])
def engine(request: pytest.FixtureRequest) -> Iterator[Any]:
    """One test body, both engines, so a divergence is a failure rather than a discovery.

    Parameterised rather than duplicated: the whole point of the append-and-`max()` design is that
    it needs nothing either engine lacks, and two copies of the test would let one drift.

    Dropped before and after: a watermark left by another run is another run's decision, and a test
    that inherits one is a test whose result depends on what ran before it.
    """
    if request.param == "postgres":
        if not PG_DSN:
            pytest.skip("set SDE_POSTGRES_DSN for the PostgreSQL half")
        from sde.engines.postgres import PostgresEngine

        with PostgresEngine(PG_DSN) as eng:
            _sql(eng, f'DROP TABLE IF EXISTS "{sde.WATERMARK_TABLE}"')
            yield eng
            _sql(eng, f'DROP TABLE IF EXISTS "{sde.WATERMARK_TABLE}"')
    else:
        if not CH_DSN:
            pytest.skip("set SDE_CLICKHOUSE_DSN for the ClickHouse half")
        from sde.engines.clickhouse import ClickHouseEngine

        with ClickHouseEngine(CH_DSN) as eng:
            _sql(eng, f'DROP TABLE IF EXISTS "{sde.WATERMARK_TABLE}"')
            yield eng
            _sql(eng, f'DROP TABLE IF EXISTS "{sde.WATERMARK_TABLE}"')


def test_an_empty_engine_reports_no_watermark_and_creates_the_table(engine: Any) -> None:
    """`max()` over nothing is the case each engine answers differently in raw SQL.

    PostgreSQL returns null; ClickHouse returns 0, because an empty aggregate over Int64 is zero
    rather than null. Both have to come back as "nothing has been applied", and the adapter is what
    makes that true - exactly the kind of belief a fake would have agreed with.
    """
    assert engine.map_watermark() is None
    # Idempotent: the table exists now, and asking again is the ordinary path on every restart.
    assert engine.map_watermark() is None
    assert _count(engine) == 0


def test_the_watermark_advances_and_a_rollback_is_refused(engine: Any) -> None:
    model = _model()
    session = sde.Session(model, _map(model, version=4, engine="e"), {"e": engine})
    assert session.rollback_protection.protection == "enforced"
    assert engine.map_watermark() == 4

    sde.Session(model, _map(model, version=9, engine="e"), {"e": engine})
    assert engine.map_watermark() == 9

    with pytest.raises(sde.MapRolledBack, match="version 9 has already been applied"):
        sde.Session(model, _map(model, version=8, engine="e"), {"e": engine})

    # Equal is allowed, and the watermark does not move.
    sde.Session(model, _map(model, version=9, engine="e"), {"e": engine})
    assert engine.map_watermark() == 9


def test_the_documented_escape_actually_works(engine: Any) -> None:
    """The refusal tells an operator to delete rows above the version they want. It has to work.

    A message naming a remedy that does not is worse than a message naming none: somebody will run
    it during an incident and conclude the product is lying to them.
    """
    model = _model()
    sde.Session(model, _map(model, version=9, engine="e"), {"e": engine})
    with pytest.raises(sde.MapRolledBack):
        sde.Session(model, _map(model, version=5, engine="e"), {"e": engine})

    statement = f'DELETE FROM "{sde.WATERMARK_TABLE}" WHERE map_version > 5'
    if engine.dialect == "postgres":
        _sql(engine, statement)
    else:
        # ClickHouse deletes asynchronously unless told otherwise, and a mutation still running
        # when the next statement arrives is exactly the confusion an operator does not need. The
        # refusal message says to let the deletion finish before restarting, which is the
        # dialect-free way to say this; running it here is that instruction, followed.
        _sql(engine, statement, mutations_sync=2)

    session = sde.Session(model, _map(model, version=5, engine="e"), {"e": engine})
    assert session.rollback_protection.protection == "enforced"
    assert engine.map_watermark() == 5


def test_the_bookkeeping_holds_one_row_per_version_and_not_per_start(engine: Any) -> None:
    """Recording every start would grow the table by a row per restart and say nothing more.

    Also the reason nothing here ever updates: append-only means no contention and no row-level
    semantics to differ between the two engines.
    """
    model = _model()
    for version in (1, 2, 2, 2, 3):
        sde.Session(model, _map(model, version=version, engine="e"), {"e": engine})
    assert _count(engine) == 3
    assert engine.map_watermark() == 3

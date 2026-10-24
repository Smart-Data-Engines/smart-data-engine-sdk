"""A group copied onto Elasticsearch and off it: the adapter's half of a migration, live.

Set SDE_ELASTICSEARCH_DSN to run, and SDE_POSTGRES_DSN as well for the copies between engines. Each
test makes its own indexes - and its own PostgreSQL schema - and removes them.
"""

from __future__ import annotations

import os
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID, uuid4

import pytest

import sde
from sde.engines._write_fences import fence_pipeline
from sde.engines.elasticsearch import KEY_PAGE, ElasticsearchEngine
from sde.engines.postgres import PostgresEngine
from sde.write_fence import EPOCH_COLUMN

DSN = os.environ.get("SDE_ELASTICSEARCH_DSN")
pytestmark = pytest.mark.skipif(DSN is None, reason="set SDE_ELASTICSEARCH_DSN to run")
PROJECT = "0" * 31 + "2"


@contextmanager
def elasticsearch() -> Iterator[tuple[ElasticsearchEngine, list[str]]]:
    """An adapter, and a list the test appends each index it creates to, for removal."""
    assert DSN is not None
    engine = ElasticsearchEngine(DSN)
    engine.connect()
    created: list[str] = []
    try:
        yield engine, created
    finally:
        for index in created:
            engine._request("PUT", f"/{index}/_settings", {"index.blocks.write": False})
            engine._request("DELETE", f"/{index}")
            engine._request("DELETE", f"/_ingest/pipeline/{fence_pipeline(index)}")
        engine.close()


def _index(engine: ElasticsearchEngine, created: list[str], columns: dict[str, str],
           key: list[str]) -> str:
    name = "sde_mig_" + uuid4().hex[:12]
    created.append(name)
    layout = sde.PhysicalLayout(tables={"Row": name}, columns={"Row": columns})
    engine.ensure_schema(layout, keys={"Row": key})
    return name


NAMES = ["a", "B", "\u00e4", "e\u0301", "\u00e9", "\U0001f600", "\uff5a", "", "a b"]


def test_a_key_range_reads_in_key_order_across_pages_with_both_bounds() -> None:
    """Keys sort as the other engines sort them - text by code point, which is UTF-8 byte order,
    and numbers as numbers - and a range crosses pages without a row repeated or skipped."""
    with elasticsearch() as (engine, created):
        index = _index(engine, created, {"name": "keyword", "n": "long", "body": "keyword"},
                       ["name", "n"])
        rows = [
            # 7919 is prime to 10007: every n is distinct, and they come in no order.
            {"name": NAMES[i % len(NAMES)], "n": (i * 7919) % 10007 - 5000, "body": f"row {i}"}
            for i in range(2 * KEY_PAGE + 345)
        ]
        engine.copy_in(index, rows)
        expected = sorted(rows, key=lambda row: (row["name"], row["n"]))
        keys = [(row["name"], row["n"]) for row in expected]

        def read(**bounds: Any) -> list[tuple[str, int]]:
            return [(row["name"], row["n"]) for row in engine.key_range(index, ["name", "n"],
                                                                        **bounds)]

        assert engine.key_range(index, ["name", "n"]) == expected
        assert read(after=keys[KEY_PAGE - 1], limit=KEY_PAGE + 500) == \
            keys[KEY_PAGE:2 * KEY_PAGE + 500]
        assert read(after=keys[10], upto=keys[KEY_PAGE + 500]) == keys[11:KEY_PAGE + 501]
        assert read(upto=keys[0]) == keys[:1]
        assert read(after=keys[-1]) == []
        assert read(limit=0) == []
        with pytest.raises(sde.EngineError, match="null"):
            read(after=(None, 1))
        with pytest.raises(sde.EngineError, match="values and the order has"):
            read(after=("a",))

        assert engine.nth_key(index, ["name", "n"], position=1) == keys[0]
        assert engine.nth_key(index, ["name", "n"], position=KEY_PAGE) == keys[KEY_PAGE - 1]
        assert engine.nth_key(index, ["name", "n"], position=KEY_PAGE + 1) == keys[KEY_PAGE]
        assert engine.nth_key(index, ["name", "n"], position=len(keys)) == keys[-1]
        assert engine.nth_key(index, ["name", "n"], position=len(keys) + 1) is None
        assert engine.count(index) == len(rows)


def test_every_key_type_orders_and_bounds_as_its_value() -> None:
    """A key of booleans, dates, instants, decimals and uuids: each compared as its value - an
    instant before 1970, a negative decimal - and a boolean, which has no range, by its two."""
    columns = {
        "flag": "boolean",
        "day": "date",
        "at": "long/timestamptz",
        "amount": "long/decimal(12,2)",
        "ref": "keyword/uuid",
    }
    order = list(columns)
    with elasticsearch() as (engine, created):
        index = _index(engine, created, columns, order)
        start = datetime(1969, 12, 31, 23, 59, 59, 999_999, tzinfo=UTC)
        rows = [
            {
                "flag": i % 2 == 0,
                "day": date(1999, 12, 30) + timedelta(days=i % 3),
                "at": start + timedelta(microseconds=(i * 37) % 11),
                "amount": Decimal(i * 13 % 17 - 8) / 4,
                "ref": UUID(int=(i * 2654435761) % (1 << 128)),
            }
            for i in range(60)
        ]
        engine.copy_in(index, rows)
        expected = sorted(
            {tuple(row[c] for c in order) for row in rows}  # equal keys are one document
        )

        def read(**bounds: Any) -> list[tuple[Any, ...]]:
            return [tuple(row[c] for c in order) for row in engine.key_range(index, order,
                                                                             **bounds)]

        assert read() == expected
        for at in (0, 1, len(expected) // 2, len(expected) - 2):
            assert read(after=expected[at]) == expected[at + 1:]
            assert read(upto=expected[at]) == expected[:at + 1]
            assert read(after=expected[0], upto=expected[at]) == expected[1:at + 1]
        assert engine.nth_key(index, order, position=len(expected)) == expected[-1]


def test_a_copy_leaves_a_document_already_there_and_says_what_it_refused() -> None:
    with elasticsearch() as (engine, created):
        index = _index(engine, created, {"id": "long", "body": "keyword"}, ["id"])
        engine.write_fence(index, project_id=PROJECT).prepare(3)
        engine.copy_in(index, [{"id": 1, "body": "first", EPOCH_COLUMN: 3},
                               {"id": 2, "body": "two", EPOCH_COLUMN: 3}])
        engine.copy_in(index, [{"id": 1, "body": "second", EPOCH_COLUMN: 3},
                               {"id": 3, "body": "three", EPOCH_COLUMN: 3}])
        # The newer document stays - a backfill's older copy never covers a fan-out's write - and
        # every copied document is visible to a search on return.
        assert engine.get(index, {"id": 1}) == {"id": 1, "body": "first"}
        assert engine.count(index) == 3
        assert [row[EPOCH_COLUMN] for row in engine.key_range(index, ["id"])] == [3, 3, 3]
        with pytest.raises(sde.EngineError, match=(
            r"1 were refused \(fail_processor_exception\), 0 were there already and 1 were "
            r"written")):
            engine.copy_in(index, [{"id": 4, "body": "x", EPOCH_COLUMN: 2},
                                   {"id": 5, "body": "y", EPOCH_COLUMN: 3}])
        with pytest.raises(sde.EngineError, match="different columns"):
            engine.copy_in(index, [{"id": 6, "body": "x", EPOCH_COLUMN: 3}, {"id": 7}])
        assert engine.count(index) == 4


def test_the_backfill_marker_is_the_largest_appended_for_its_copy_and_entity() -> None:
    with elasticsearch() as (engine, _):
        copy = "copy-" + uuid4().hex
        try:
            assert engine.backfill_marker(materialization=copy, entity="Note") == 0
            engine.record_backfill_marker(materialization=copy, entity="Note", rows=1000)
            engine.record_backfill_marker(materialization=copy, entity="Note", rows=500)
            engine.record_backfill_marker(materialization=copy, entity="Other", rows=7)
            assert engine.backfill_marker(materialization=copy, entity="Note") == 1000
            assert engine.backfill_marker(materialization=copy, entity="Other") == 7
            assert engine.backfill_marker(materialization=copy + "x", entity="Note") == 0
        finally:
            engine._request("POST", "/sde_backfill_state/_delete_by_query?refresh=true",
                            {"query": {"term": {"materialization": copy}}})


@contextmanager
def postgres() -> Iterator[PostgresEngine]:
    dsn = os.environ.get("SDE_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set SDE_POSTGRES_DSN for a copy between engines")
    schema = "sde_esmig_" + uuid4().hex[:12]
    with PostgresEngine(dsn) as engine:
        engine._cx.execute(f'CREATE SCHEMA "{schema}"')
        engine._cx.execute(f'SET search_path TO "{schema}"')
        try:
            yield engine
        finally:
            engine._cx.execute(f'DROP SCHEMA "{schema}" CASCADE')


def _note_model() -> sde.LogicalModel:
    sde.clear_registry()

    @sde.entity
    class Note:
        id: int
        body: str
        at: datetime
        amount: Annotated[Decimal, sde.precision(12, 2)]
        ref: UUID
        day: date
        ok: bool
        ratio: sde.Float32
        loose: sde.Timestamp | None

    return sde.build_model(Note)


def _material(model: sde.LogicalModel, role: str, engine: str, dialect: str,
              table: str) -> dict[str, Any]:
    group = next(g for g in sde.colocation_groups(model) if g.name == "Note")
    layout = sde.default_layout(model, group, dialect=dialect)
    columns = dict(layout.columns["Note"])
    return {"id": role, "engine": engine,
            "layout": {"tables": {"Note": table}, "columns": {"Note": columns}}}


def _row(i: int) -> dict[str, Any]:
    return {
        "id": i,
        "body": f"note {i} été",
        "at": datetime(2026, 10, 23, 9, 30, tzinfo=UTC) + timedelta(microseconds=i * 1001),
        "amount": Decimal(i) / 100 - Decimal(7),
        "ref": UUID(int=i * 7919),
        "day": date(2026, 1, 1) + timedelta(days=i % 300),
        "ok": i % 3 == 0,
        "ratio": i / 7,
        "loose": None if i % 4 else datetime(1960, 1, 1, 0, 0, i % 60),
    }


@pytest.mark.parametrize("source", ["postgres", "elasticsearch"])
def test_a_group_is_backfilled_between_postgres_and_elasticsearch_and_verified(
    source: str,
) -> None:
    """Rows saved before the copy existed are backfilled, later ones arrive by the fan-out, and
    verification compares every value of every row across the two engines - then finds one
    changed behind the library's back."""
    model = _note_model()
    with postgres() as pg, elasticsearch() as (es, created):
        index = "sde_mig_" + uuid4().hex[:12]
        created.append(index)
        table = "notes_" + uuid4().hex[:8]
        materials = {
            "postgres": _material(model, "pg", "pg", "postgres", table),
            "elasticsearch": _material(model, "es", "es", "elasticsearch", index),
        }
        target = "elasticsearch" if source == "postgres" else "postgres"
        # A copy's marker is named by its materialization id, and on Elasticsearch it is kept
        # cluster-wide, as `sde_map_state` is: each run names its copies afresh, as the control
        # plane names every staged copy.
        run = uuid4().hex[:8]
        materials["postgres"]["id"] = "pg-" + run
        materials["elasticsearch"]["id"] = "es-" + run

        def placement(version: int, *, copy: bool) -> sde.PlacementMap:
            group: dict[str, Any] = {"write_epoch": 1, "source": materials[source]}
            if copy:
                group["derived"] = [{**materials[target], "lag_budget_ms": 60000}]
                group["also_write"] = [materials[target]["id"]]
            return sde.load_map({"contract": 4, "project_id": PROJECT,
                                 "model_version": model.version, "map_version": version,
                                 "groups": {"Note": group}}, model=model)

        engines = {"pg": pg, "es": es}
        before = placement(1, copy=False)
        sde.prepare_schema(model, before, engines, project_id=PROJECT)
        early = [_row(i) for i in range(1, KEY_PAGE + 251)]
        first = sde.Session(model, before, engines, project_id=PROJECT)
        for at in range(0, len(early), 500):
            first.save_many("Note", early[at:at + 500])

        during = placement(2, copy=True)
        sde.prepare_schema(model, during, engines, project_id=PROJECT)
        session = sde.Session(model, during, engines, project_id=PROJECT)
        late = [_row(i) for i in range(KEY_PAGE + 251, KEY_PAGE + 261)]
        session.save_many("Note", late)

        progress = sde.backfill(session, "Note", chunk_rows=400)
        assert progress.complete and progress.entities[0].rows_copied == len(early) + len(late)
        report = sde.verify(session, "Note", chunk_rows=400)
        assert report.matched, report.for_a_human()
        assert report.rows_source == report.rows_target == len(early) + len(late)
        # A float32 reads back as the float32 it is, from either engine.
        assert session.get("Note", {"id": 5}) == {
            **_row(5), "ratio": struct.unpack("<f", struct.pack("<f", 5 / 7))[0]
        }

        # One value changed in the copy, outside the library: verification names the column.
        copy_engine = es if target == "elasticsearch" else pg
        es._request("POST", "/sde_backfill_state/_delete_by_query?refresh=true",
                    {"query": {"term": {"materialization": "es-" + run}}})
        copy_table = index if target == "elasticsearch" else table
        changed = {**_row(7), "body": "changed", EPOCH_COLUMN: 1}
        if target == "elasticsearch":
            identity, document = es._index(copy_table).document(changed)
            es._request("PUT", f"/{copy_table}/_doc/{identity}?refresh=true", document)
        else:
            pg._cx.execute(f'UPDATE "{copy_table}" SET body = %s WHERE id = 7', ["changed"])
        assert copy_engine.get(copy_table, {"id": 7})["body"] == "changed"
        report = sde.verify(session, "Note", chunk_rows=400)
        assert not report.matched
        assert [(d.key, d.columns) for d in report.differences] == [({"id": 7}, ("body",))]

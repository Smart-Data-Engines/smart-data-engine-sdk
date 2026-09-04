"""The ClickHouse slice, against a real server.

The second engine, and the reason the product exists: between PostgreSQL and ClickHouse lies the
decision clients get wrong once, at the start, and never revisit. With one adapter the planner
chooses
from a set of size one.

Against a real server rather than a fake, for the same reason as the PostgreSQL slice: a fake would
agree with whatever this library believes about types, quoting and merge semantics, and those
beliefs are the whole content of an adapter. This file exists because three of them turned out to be
wrong.

    docker run -d --name sde_test_ch -e CLICKHOUSE_PASSWORD=sde -e CLICKHOUSE_DB=sde \\
        -p 127.0.0.1:58123:8123 clickhouse/clickhouse-server:24.8-alpine
    SDE_CLICKHOUSE_DSN=clickhouse://default:sde@127.0.0.1:58123/sde pytest
"""

from __future__ import annotations

import datetime as dt
import decimal
import os
import uuid
from collections.abc import Iterator
from typing import Annotated

import pytest

import sde
from sde.engines.clickhouse import ClickHouseEngine
from sde.errors import EngineError
from sde.layout import default_layout

DSN = os.environ.get("SDE_CLICKHOUSE_DSN")

pytestmark = pytest.mark.skipif(
    not DSN,
    reason="set SDE_CLICKHOUSE_DSN to run the ClickHouse slice; skipped rather than faked, since "
    "a fake would agree with whatever this library believes about merge semantics",
)


@pytest.fixture
def model() -> sde.LogicalModel:
    sde.clear_registry()

    @sde.entity
    class Event:
        id: uuid.UUID
        name: str
        at: dt.datetime
        amount: Annotated[decimal.Decimal, sde.precision(12, 2)]

    return sde.build_model(Event)


@pytest.fixture
def engine(model: sde.LogicalModel) -> Iterator[ClickHouseEngine]:
    assert DSN
    group = sde.colocation_groups(model)[0]
    layout = default_layout(model, group, dialect="clickhouse")
    with ClickHouseEngine(DSN) as eng:
        for table in layout.tables.values():
            eng._cx.command(f"DROP TABLE IF EXISTS `{table}`")
        eng.ensure_schema(layout, keys={"Event": model.entity("Event").key})
        yield eng


def _event(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "name": "checkout",
        "at": dt.datetime(2026, 8, 27, 12, 0, 0, 500000),
        "amount": decimal.Decimal("1234.56"),
    }
    values.update(overrides)
    return values


def test_the_schema_is_created_and_creating_it_again_changes_nothing(
    engine: ClickHouseEngine, model: sde.LogicalModel
) -> None:
    group = sde.colocation_groups(model)[0]
    layout = default_layout(model, group, dialect="clickhouse")
    engine.ensure_schema(layout, keys={"Event": model.entity("Event").key})

    described = engine._cx.query("DESCRIBE TABLE `event`").result_rows
    types = {row[0]: row[1] for row in described}
    assert types == {
        "id": "UUID",
        "name": "String",
        "at": "DateTime64(3, 'UTC')",
        "amount": "Decimal(12, 2)",
    }

    created = engine._cx.query(
        "SELECT engine, sorting_key FROM system.tables WHERE database = currentDatabase() "
        "AND name = 'event'"
    ).result_rows
    assert created[0][0] == "ReplacingMergeTree"
    assert created[0][1] == "id", "ORDER BY has to be the declared key, or the table is unindexed"


def test_a_row_written_comes_back_identical(engine: ClickHouseEngine) -> None:
    values = _event()
    engine.insert("event", values)
    read = engine.get("event", {"id": values["id"]})

    assert read is not None
    assert read["name"] == "checkout"
    # Milliseconds, and the timezone. DateTime64(3) rather than DateTime is what keeps the .5, and
    # `_as_utc` is what stops the driver reading a naive value as local time - which it does by
    # default, and which put this value two hours out before the adapter existed.
    assert read["at"] == dt.datetime(2026, 8, 27, 12, 0, 0, 500000, tzinfo=dt.UTC)
    assert read["amount"] == decimal.Decimal("1234.56")
    assert isinstance(read["amount"], decimal.Decimal)


def test_a_naive_datetime_is_utc_and_an_aware_one_is_respected(
    engine: ClickHouseEngine,
) -> None:
    """The measured divergence this adapter exists to close.

    `clickhouse-connect` reads a naive datetime as *local* time. On a machine at UTC+2 the same
    value that PostgreSQL stored as 12:00:00+00:00 arrived here as 10:00:00 - two hours, from one
    call, in one library, with no error anywhere. After moving a group from one engine to the other,
    every timestamp in a client's analytics would shift by the offset of whichever machine wrote the
    row.
    """
    naive = _event(at=dt.datetime(2026, 8, 27, 12, 0, 0))
    engine.insert("event", naive)
    read = engine.get("event", {"id": naive["id"]})
    assert read is not None
    assert read["at"] == dt.datetime(2026, 8, 27, 12, 0, tzinfo=dt.UTC)

    plus_five = dt.timezone(dt.timedelta(hours=5))
    aware = _event(at=dt.datetime(2026, 8, 27, 12, 0, 0, tzinfo=plus_five))
    engine.insert("event", aware)
    read_aware = engine.get("event", {"id": aware["id"]})
    assert read_aware is not None
    assert read_aware["at"] == dt.datetime(2026, 8, 27, 7, 0, tzinfo=dt.UTC)


def test_a_timestamptz_column_gives_back_an_aware_datetime(engine: ClickHouseEngine) -> None:
    """Symmetry with psycopg, which matters more than it sounds.

    The driver returns a *naive* datetime for a `DateTime64(3, 'UTC')` column while psycopg returns
    an aware one for a `timestamptz`. Left alone, the same entity read from the two engines
    produces two datetimes Python refuses to compare - `can't compare offset-naive and offset-aware
    datetimes` - a TypeError in a client's code that appears on the day a group moves and not
    before.
    """
    values = _event()
    engine.insert("event", values)
    read = engine.get("event", {"id": values["id"]})
    assert read is not None
    assert read["at"].tzinfo is not None
    assert read["at"] == values["at"].replace(tzinfo=dt.UTC)  # type: ignore[union-attr]


def test_saving_the_same_key_twice_replaces_rather_than_raising(
    engine: ClickHouseEngine,
) -> None:
    """The divergence that cannot be removed, only chosen - so it is asserted rather than hidden.

    In PostgreSQL this is a primary key violation and `save()` raises. Here a MergeTree does not
    enforce uniqueness, so something has to happen instead. Plain `MergeTree` would leave two rows
    and make every aggregate over the group quietly wrong. `ReplacingMergeTree` keeps the newest,
    and `FINAL` on the read is what makes that visible now rather than at the next merge.

    **Merges are stopped for the duration, and that is the point of the test rather than a detail.**
    The first version of this ran without doing so and could not tell the difference: a background
    merge collapses the duplicate within seconds, after which a read with `FINAL` and a read without
    it return the same thing. Deleting `FINAL` from the adapter passed the whole suite. Measured
    with merges stopped, an unmerged point read returns ``'first'`` - the *superseded* row - so what
    the old test was really asserting was that ClickHouse had got there first.
    """
    engine._cx.command("SYSTEM STOP MERGES `event`")
    try:
        values = _event(name="first")
        engine.insert("event", values)
        engine.insert("event", {**values, "name": "second"})

        stored = engine._cx.query("SELECT count() FROM `event`").result_rows[0][0]
        assert stored == 2, (
            "two parts were expected, so the rest of this test is about deduplication on read. One "
            "row here means the insert or the merge stop did not do what this test assumes."
        )

        read = engine.get("event", {"id": values["id"]})
        assert read is not None
        assert read["name"] == "second", "a point read returned a superseded row"
        assert engine.count("event") == 1, (
            "count() must count entities rather than stored rows: a number that drifts and then "
            "settles makes a test flaky and a dashboard untrustworthy in the same way"
        )
        assert len(engine.range("event", "at")) == 1, "a range read returned the duplicate too"
    finally:
        engine._cx.command("SYSTEM START MERGES `event`")


def test_a_compatibility_view_carries_final_and_therefore_counts_entities(
    engine: ClickHouseEngine, model: sde.LogicalModel
) -> None:
    """Requirement 19.7's view, and the reason it is not a cosmetic wrapper around a table name.

    A hand-written query moved verbatim to a ClickHouse target reads a `ReplacingMergeTree` without
    `FINAL` and counts a row written twice under one key twice - no error, just a bigger number.
    The library's own reads use `FINAL`; a person's do not. So a compatibility view that omitted it
    would be worse than no view: it would look like the thing that made the old query safe.

    Merges are stopped for the same reason `test_saving_the_same_key_twice_replaces_rather_than_
    raising` stops them: a background merge collapses the duplicate within seconds, after which the
    two reads agree and deleting `FINAL` passes.
    """
    group = sde.colocation_groups(model)[0]
    layout = default_layout(model, group, dialect="clickhouse")
    views = sde.compatibility_views(layout, was={"Event": "old_event"}, dialect="clickhouse")
    assert views.create, "a renamed table has a view to render"

    engine._cx.command("SYSTEM STOP MERGES `event`")
    try:
        for statement in views.create:
            # Twice, because rendering is idempotent by construction everywhere else here.
            engine._cx.command(statement)
            engine._cx.command(statement)
        values = _event(name="first")
        engine.insert("event", values)
        engine.insert("event", {**values, "name": "second"})

        stored = engine._cx.query("SELECT count() FROM `event`").result_rows[0][0]
        assert stored == 2, "two parts were expected; the rest of this test is about the view"
        through = engine._cx.query("SELECT count() FROM `old_event`").result_rows[0][0]
        assert through == 1, (
            "the compatibility view returned the duplicate, so it went out without FINAL. A query "
            "reading it would report one entity as two and get no error to notice."
        )
        rows = engine._cx.query("SELECT `name` FROM `old_event`").result_rows
        assert rows == [("second",)], "and it is the newest row that survives, as on any read"
    finally:
        for statement in views.drop:
            engine._cx.command(statement)
        engine._cx.command("SYSTEM START MERGES `event`")
    assert (
        engine._cx.query(
            "SELECT count() FROM system.tables WHERE database = currentDatabase() "
            "AND name = 'old_event'"
        ).result_rows[0][0]
        == 0
    ), "the view goes with the source it stood in for"


def test_a_range_read_is_ordered_and_bounded(engine: ClickHouseEngine) -> None:
    base = dt.datetime(2026, 8, 27, 0, 0, tzinfo=dt.UTC)
    for hour in range(6):
        engine.insert("event", _event(at=base + dt.timedelta(hours=hour), name=f"h{hour}"))

    rows = engine.range(
        "event", "at", low=base + dt.timedelta(hours=1), high=base + dt.timedelta(hours=4)
    )
    assert [row["name"] for row in rows] == ["h1", "h2", "h3"]

    capped = engine.range("event", "at", low=base, limit=2)
    assert len(capped) == 2


def test_a_hostile_string_is_a_value_and_never_syntax(engine: ClickHouseEngine) -> None:
    """Identifiers come from the map, but *values* come from the client's users.

    Verified rather than assumed: the string is stored, matched by equality, and the same text used
    as
    a needle for a different key finds nothing - which is what an unescaped `OR 1=1` would break.
    """
    hostile = "x' OR 1=1 --"
    values = _event(name=hostile)
    engine.insert("event", values)

    read = engine.get("event", {"id": values["id"]})
    assert read is not None
    assert read["name"] == hostile

    rows = engine.range("event", "name", low=hostile, high=hostile + "￿")
    assert [row["name"] for row in rows] == [hostile]


def test_asking_for_a_transaction_refuses_instead_of_pretending(
    engine: ClickHouseEngine,
) -> None:
    """A no-op context manager would be the friendlier signature and the worse library.

    The caller would believe a group of writes was atomic and would learn otherwise from the state
    of the data. The message names the way out, which is a declaration rather than a flag.
    """
    with pytest.raises(EngineError, match="no multi-statement transactions"), engine.transaction():
        pass  # pragma: no cover - the call raises before the block is entered


def test_a_layout_carrying_indexes_is_refused_as_built_for_another_dialect(
    engine: ClickHouseEngine, model: sde.LogicalModel
) -> None:
    """A map derived for PostgreSQL, handed to this engine, fails with a sentence rather than SQL.

    `CREATE INDEX` in ClickHouse builds a data-skipping index with a type and a granularity, so a
    B-tree index definition is not something to approximate here.
    """
    group = sde.colocation_groups(model)[0]
    wrong = default_layout(model, group, dialect="postgres")
    wrong_with_index = sde.PhysicalLayout(
        tables=wrong.tables,
        columns=wrong.columns,
        indexes=({"entity": "Event", "name": "i", "columns": ["id"]},),
    )
    with pytest.raises(EngineError, match="no B-tree to put them in"):
        engine.ensure_schema(wrong_with_index, keys={"Event": ["id"]})


def test_a_key_naming_a_column_the_layout_lacks_is_refused(
    engine: ClickHouseEngine, model: sde.LogicalModel
) -> None:
    """In ClickHouse the key becomes ORDER BY, so this is a table that cannot be created."""
    group = sde.colocation_groups(model)[0]
    layout = default_layout(model, group, dialect="clickhouse")
    with pytest.raises(EngineError, match="names columns the layout does not have"):
        engine.ensure_schema(layout, keys={"Event": ["not_a_column"]})


def test_every_read_asks_for_FINAL(engine: ClickHouseEngine) -> None:
    """A structural assertion, and the reason it is structural is the interesting part.

    `FINAL` is what makes `ReplacingMergeTree` deduplicate at read time. Deleting it from the point
    read is a real defect - measured, an unmerged point read returns the *superseded* row - and it
    cannot be pinned behaviourally, which was discovered by trying:

    - with merges running, a background merge collapses the duplicate within seconds and both forms
      of the query agree, so the assertion passes on broken code
    - with merges stopped, two parts exist and `LIMIT 1` reads them in parallel, returning whichever
      finishes first. The mutation was caught when the file ran alone and missed when another file
      ran before it - the signature of a race rather than of a fix

    A behavioural test that catches a defect on some runs is worse than a structural one that
    catches
    it on all of them, because the first gets a re-run and the second gets read. `count()` and
    `range()` *are* pinned behaviourally in the test above - their results differ by a whole row -
    and this covers the third path.
    """
    sent: list[str] = []
    original = engine._cx.query

    def recording(sql: str, *args: object, **kwargs: object) -> object:
        sent.append(sql)
        return original(sql, *args, **kwargs)

    engine._cx.query = recording  # type: ignore[method-assign]
    try:
        values = _event()
        engine.insert("event", values)
        engine.get("event", {"id": values["id"]})
        engine.range("event", "at")
        engine.count("event")
    finally:
        engine._cx.query = original  # type: ignore[method-assign]

    # Only our own reads. `insert()` makes the driver issue a `DESCRIBE TABLE` of its own to learn
    # the column types, which goes through the same method and is not a read of ours.
    reads = [sql for sql in sent if sql.lstrip().upper().startswith("SELECT")]
    assert len(reads) == 3, f"expected one SELECT per read path, saw {reads} out of {sent}"
    for sql in reads:
        assert " FINAL" in sql, (
            f"a read went out without FINAL: {sql!r}. On a ReplacingMergeTree that returns rows "
            f"the "
            f"engine considers superseded, which is a wrong answer rather than a slow one."
        )


def test_a_composite_key_keeps_its_declared_order(engine: ClickHouseEngine) -> None:
    """ORDER BY is the key as declared, not the key sorted - and the difference is measurable.

    Found by a mutation that did nothing: every other test here uses a single-field key, so sorting
    a one-element list changes nothing and `sorted(key)` passed the whole suite. With a real
    composite key the order is positional and load-bearing. ClickHouse prunes granules on a *prefix*
    of the sorting key, so `(region, at)` makes "this region, this time range" cheap and `(at,
    region)` makes it a scan. Sorting the key alphabetically would silently swap those performances
    while leaving the placement map looking identical.
    """
    sde.clear_registry()

    @sde.entity
    class Reading:
        region: str
        at: dt.datetime
        value: Annotated[decimal.Decimal, sde.precision(10, 2)]

        class Meta:
            key = ["region", "at"]

    model = sde.build_model(Reading)
    group = sde.colocation_groups(model)[0]
    layout = default_layout(model, group, dialect="clickhouse")

    engine._cx.command("DROP TABLE IF EXISTS `reading`")
    engine.ensure_schema(layout, keys={"Reading": model.entity("Reading").key})

    sorting_key = engine._cx.query(
        "SELECT sorting_key FROM system.tables WHERE database = currentDatabase() "
        "AND name = 'reading'"
    ).result_rows[0][0]
    assert sorting_key == "region, at", (
        f"the sorting key is {sorting_key!r}. Declared order is ['region', 'at']; alphabetical "
        "order "
        f"would be ['at', 'region'], which is a different table with the same map."
    )

    engine.insert(
        "reading",
        {
            "region": "eu",
            "at": dt.datetime(2026, 8, 27, 12, 0, tzinfo=dt.UTC),
            "value": decimal.Decimal("1.50"),
        },
    )
    read = engine.get(
        "reading",
        {"region": "eu", "at": dt.datetime(2026, 8, 27, 12, 0, tzinfo=dt.UTC)},
    )
    assert read is not None
    assert read["value"] == decimal.Decimal("1.50")

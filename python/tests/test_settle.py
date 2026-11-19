"""A settle before a cutover, and the pause that stands on it, without an engine.

After a generation advance every write of the group carries the map's generation, here ``G``, and
the engines admit no other. So a row below it was written before the advance and has not changed
since, on the source and on the copy. ``sde.reconcile.settle`` compares those rows while the
application keeps writing; ``reconcile_since`` is then a cutover's first pass over the rest, the
rows that carry ``G``, and ``compare_stamped`` the comparison under the final barriers, as before.

The engines here are those of ``test_reconcile.py`` - in memory, with the real engines' rule that
whatever writes a row stamps it with the generation it is admitted under - and one more read: the
rows below a generation after a key. ``test_settle_live.py`` holds the same against PostgreSQL and
ClickHouse, with their own constraints doing the stamping and an application writing beside it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pytest
from test_reconcile import BEFORE, OPERATION, CutShort, Streaming, Table, fixture, held

from sde.errors import MigrationRefused
from sde.generation import EPOCH_COLUMN
from sde.inspection import InspectionContext
from sde.reconcile import (
    Reconciliation,
    Settlement,
    compare_stamped,
    reconcile_since,
    settle,
    settles_before_a_pause,
)

G = BEFORE
"""The generation of the map in force: the one the group was advanced to."""
M = OPERATION
"""A cutover's own generation, one above it: the only one the copy admits under the pause."""
OLD = G - 1
"""A generation from before the advance."""


class Earlier(Table):
    """An engine in memory that also answers the rows below a generation after a key."""

    def earlier_range(
        self,
        table: str,
        order: Sequence[str],
        *,
        epoch: int,
        after: Sequence[Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append("earlier_range")
        rows = sorted(
            (row for row in self.tables[table] if row[EPOCH_COLUMN] < epoch),
            key=lambda row: tuple(row[c] for c in order),
        )
        if after is not None:
            rows = [row for row in rows if tuple(row[c] for c in order) > tuple(after)]
        return [dict(row) for row in (rows if limit is None else rows[:limit])]


class EarlierStreaming(Earlier, Streaming):
    """A source that streams, as ClickHouse does."""


class EarlierCutShort(Earlier, CutShort):
    """A source whose stream ends after its first chunk and raises nothing."""


class Growing(Earlier):
    """A source an application appends to at its end faster than it is read: three new keys a
    statement, where a page holds two, as a busy series would."""

    def _append(self, table: str) -> None:
        for _ in range(3):
            top = max(row["id"] for row in self.tables[table])
            self.tables[table].append({"id": top + 1, "value": "new", EPOCH_COLUMN: G})

    def _windowed(self, table: str, order: Sequence[str], **bounds: Any) -> list[dict[str, Any]]:
        self._append(table)
        return super()._windowed(table, order, **bounds)

    def earlier_range(
        self, table: str, order: Sequence[str], **kwargs: Any
    ) -> list[dict[str, Any]]:
        self._append(table)
        return super().earlier_range(table, order, **kwargs)


def stamped(rows: Mapping[int, tuple[str, int]]) -> list[dict[str, Any]]:
    """Rows from key -> (value, the generation that wrote it)."""
    return [
        {"id": key, "value": value, EPOCH_COLUMN: epoch} for key, (value, epoch) in rows.items()
    ]


def settled_fixture(
    source: Mapping[int, tuple[str, int]],
    copy: Mapping[int, tuple[str, int]],
    *,
    source_engine: type[Table] = Earlier,
    copy_engine: type[Table] = Earlier,
) -> tuple[InspectionContext, Table, Table]:
    view, left, right = fixture({}, {}, source_engine=source_engine, copy_engine=copy_engine)
    left.tables["events"] = stamped(source)
    right.tables["events_copy"] = stamped(copy)
    return view, left, right


SOURCE = {
    1: ("a", OLD),
    2: ("b", OLD),
    3: ("c", OLD),
    4: ("d", G),  # changed since the advance: the pause's
    5: ("e", OLD),
    6: ("f", OLD),
}
COPY = {
    1: ("a", OLD),  # equal: settled, and never read again
    2: ("x", OLD),  # another value: written over
    # 3 missing: copied
    4: ("old", OLD),  # its source row is the pause's, so this one is too
    5: ("e", G),  # written since the advance: newer than what a settle reads, left to the pause
    6: ("f", OLD),
}


def test_a_settle_compares_the_rows_from_before_the_advance_and_leaves_the_rest() -> None:
    view, source, copy = settled_fixture(SOURCE, COPY)
    done = settle(view, "Event", epoch=G, chunk_rows=2)
    # Read by pages of the rows below the generation: [1, 2], [3, 5], [6], and one that is empty.
    assert done == Settlement(chunks=3, rows=5, earlier=5, written=2) and done.settled
    assert source.calls.count("earlier_range") == 4 and "key_range" not in source.calls
    assert held(copy) == {
        1: ("a", OLD),
        2: ("b", G),
        3: ("c", G),
        4: ("old", OLD),
        5: ("e", G),
        6: ("f", OLD),
    }
    # A row the copy lacks is copied, one it holds otherwise is written over: as a fan-out would,
    # under the map's generation.
    assert copy.calls.count("copy_in") == 1 and copy.calls.count("upsert_in") == 1
    assert done.as_record() == {
        "chunks": 3,
        "rows": 5,
        "earlier": 5,
        "written": 2,
        "unconfirmed": 0,
    }


def test_the_pause_after_a_settle_reads_only_the_rows_written_since_the_advance() -> None:
    view, source, copy = settled_fixture(SOURCE, COPY)
    settle(view, "Event", epoch=G, chunk_rows=2)
    source.calls.clear()
    copy.calls.clear()
    copy.asked.clear()
    done = reconcile_since(view, "Event", since=G, epoch=M, chunk_rows=2)
    # The source's one row since, written over a copy row that differs; the copy's three rows
    # since - two a settle wrote, one a fan-out did - compared and found equal.
    assert done == Reconciliation(chunks=3, rows=4, written=1, surplus=False) and done.settled
    assert held(copy)[4] == ("d", M)
    # Nothing from before the advance is read: no range, no stream, no count of either side.
    assert set(source.calls) == {"stamped_range", "rows_for_keys"}
    assert set(copy.calls) == {"stamped_range", "rows_for_keys", "upsert_in"}
    assert not {(1,), (6,)} & {key for keys in copy.asked for key in keys}
    report = compare_stamped(view, "Event", epoch=M, reconciled=done, chunk_rows=2)
    assert report.matched and (report.rows_source, report.rows_target) == (6, 6)
    assert (report.chunks_compared, report.chunks_mismatched) == (4, 0)


class OrderedOtherwise(Earlier):
    """A copy whose engine orders the key otherwise than this process does - 3, 1, 2, 4 - as
    ClickHouse orders a UUID otherwise than PostgreSQL and Python do."""

    ORDER: dict[Any, int] = {3: 0, 1: 1, 2: 2, 4: 3}

    def stamped_range(
        self,
        table: str,
        order: Sequence[str],
        *,
        epoch: int,
        after: Sequence[Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append("stamped_range")
        place = self.ORDER
        rows = sorted(
            (row for row in self.tables[table] if row[EPOCH_COLUMN] == epoch),
            key=lambda row: place[row["id"]],
        )
        if after is not None:
            rows = [row for row in rows if place[row["id"]] > place[after[0]]]
        return [dict(row) for row in (rows if limit is None else rows[:limit])]


def logged(source: Table, log: list[tuple[str, str]]) -> Callable[..., Any]:
    """A caller's wrapper that writes down each engine call in order: which side, and what."""

    def call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        side = "source" if getattr(function, "__self__", None) is source else "copy"
        log.append((side, getattr(function, "__name__", "")))
        return function(*args, **kwargs)

    return call


def test_a_pause_whose_two_sides_hold_the_same_rows_since_looks_up_no_key() -> None:
    """The application's rows since the advance are on both sides, the source's from the
    application and the copy's from the fan-out: read beside each other, by pages of each, and
    not one looked up by its key."""
    since = {4: ("d", G), 5: ("e", G), 6: ("f", G), 7: ("g", G), 8: ("h", G)}
    before = {1: ("a", OLD), 2: ("b", OLD), 3: ("c", OLD)}
    view, source, copy = settled_fixture({**before, **since}, {**before, **since})
    log: list[tuple[str, str]] = []
    done = reconcile_since(view, "Event", since=G, epoch=M, chunk_rows=2, call=logged(source, log))
    # Three pages of each side, and the one of each that answers none.
    assert done == Reconciliation(chunks=6, rows=10, written=0, surplus=False)
    assert source.calls == copy.calls == ["stamped_range"] * 4
    # Every statement went through the caller's wrapper, which is where a cutover keeps its
    # pause's budget: the copy's pages too.
    assert len(log) == len(source.calls) + len(copy.calls)
    report = compare_stamped(view, "Event", epoch=M, reconciled=done, chunk_rows=2)
    assert report.matched and (report.rows_source, report.rows_target) == (8, 8)


def test_rows_another_order_puts_beside_another_chunk_are_compared_and_written_once() -> None:
    """The copy's engine orders the key 3, 1, 2, 4. Beside the source's chunk [1, 2] it shows
    nothing (its first row, 3, is past the chunk), so both are looked up by key and 1 is written
    over. Beside [3, 4] it hands out 3 and 4, and 1 and 2 as orphans: 1 as it was before it was
    written, which is not compared again, and 2, which is."""
    view, source, copy = settled_fixture(
        {1: ("a", G), 2: ("b", G), 3: ("c", G), 4: ("d", G)},
        {1: ("x", G), 2: ("b", G), 3: ("c", G), 4: ("y", G)},
        copy_engine=OrderedOtherwise,
    )
    done = reconcile_since(view, "Event", since=G, epoch=M, chunk_rows=2)
    assert done.settled and not done.surplus and done.written == 2
    assert held(copy) == {1: ("a", M), 2: ("b", G), 3: ("c", G), 4: ("d", M)}
    # The copy looked up by the chunk it showed nothing beside, and read back twice; the source
    # asked for the one orphan the pass had not written.
    assert copy.asked == [[(1,), (2,)], [(1,)], [(4,)]]
    assert source.asked == [[(2,)]]
    assert compare_stamped(view, "Event", epoch=M, reconciled=done, chunk_rows=2).matched


class Unorderable(OrderedOtherwise):
    """An engine whose key is one this process cannot order: complex numbers stand in for it.
    The engine has an order of its own, 1j then 2j."""

    ORDER: dict[Any, int] = {1j: 0, 2j: 1}


def test_a_key_this_process_cannot_order_is_handed_out_beside_the_chunk() -> None:
    view, _, copy = settled_fixture(
        {1j: ("a", G), 2j: ("b", G)},  # type: ignore[dict-item]
        {1j: ("a", G), 2j: ("x", G)},  # type: ignore[dict-item]
        source_engine=Unorderable,
        copy_engine=Unorderable,
    )
    done = reconcile_since(view, "Event", since=G, epoch=M, chunk_rows=2)
    assert done.settled and done.written == 1
    assert held(copy)[2j] == ("b", M)  # type: ignore[index]
    # Both rows handed out beside their chunk: the copy is asked only to read its write back.
    assert copy.asked == [[(2j,)]]


def test_orphans_are_compared_a_chunk_at_a_time_while_the_source_is_walked() -> None:
    """Seven rows a settle wrote under the map's generation, whose source rows are older, sit
    beside the source's one row since: compared by their keys two at a time as they are handed
    out, so the pass never holds more than a chunk of them."""
    before = {key: (str(key), OLD) for key in range(1, 8)}
    view, source, copy = settled_fixture(
        {**before, 9: ("i", G)},
        {**{key: (value, G) for key, (value, _) in before.items()}, 9: ("i", G)},
    )
    log: list[tuple[str, str]] = []
    done = reconcile_since(view, "Event", since=G, epoch=M, chunk_rows=2, call=logged(source, log))
    assert done.settled and done.written == 0 and not done.surplus
    assert {epoch for _, epoch in held(copy).values()} == {G}
    assert source.asked == [[(1,), (2,)], [(3,), (4,)], [(5,), (6,)], [(7,)]]
    # Three of the four lookups come before the source's walk asks for its next page.
    walked = [index for index, entry in enumerate(log) if entry == ("source", "stamped_range")]
    lookups = [index for index, entry in enumerate(log) if entry == ("source", "rows_for_keys")]
    assert len(walked) == 2 and len([at for at in lookups if at < walked[1]]) == 3


@pytest.mark.parametrize(
    ("engine", "expected"),
    [
        (Earlier, Settlement(chunks=0, rows=0, earlier=0, written=0)),
        (EarlierStreaming, Settlement(chunks=1, rows=2, earlier=0, written=0)),
    ],
    ids=["by pages", "streamed"],
)
def test_rows_written_since_are_left(engine: type[Table], expected: Settlement) -> None:
    view, _, copy = settled_fixture({1: ("a", G), 2: ("b", G)}, {}, source_engine=engine)
    assert settle(view, "Event", epoch=G, chunk_rows=2) == expected
    assert held(copy) == {}


@pytest.mark.parametrize(
    "engine", [Earlier, EarlierStreaming, EarlierCutShort], ids=["by chunks", "streamed", "cut"]
)
def test_a_walk_is_carried_to_the_last_row_below_the_generation_however_it_reads(
    engine: type[Table],
) -> None:
    """A stream that ends after its first chunk, and raises nothing, is not the end: the rows below
    the generation after its last key are read until a statement answers none. The repair is the
    same whichever way the source was read."""
    view, source, copy = settled_fixture(SOURCE, COPY, source_engine=engine)
    done = settle(view, "Event", epoch=G, chunk_rows=2)
    assert done.settled and done.written == 2 and done.earlier == 5
    assert held(copy)[2] == ("b", G) and held(copy)[3] == ("c", G)
    if engine is EarlierCutShort:
        # The first chunk streamed, then [3, 5] and [6] below the generation, then none.
        assert source.calls.count("earlier_range") == 3 and (done.chunks, done.rows) == (3, 5)
    elif engine is EarlierStreaming:
        # The whole table in one stream, then one statement that finds nothing below after it.
        assert source.calls.count("earlier_range") == 1 and (done.chunks, done.rows) == (3, 6)
    else:
        assert source.calls.count("earlier_range") == 4 and (done.chunks, done.rows) == (3, 5)


def test_a_walk_ends_however_fast_the_application_appends() -> None:
    """Every statement the walk sends finds three more rows at the source's end, where a page
    holds two, so a walk by pages of every row would never come to a short page. The new rows
    carry the map's generation, and the walk asks for the rows below it."""
    view, source, copy = settled_fixture(SOURCE, COPY, source_engine=Growing)
    done = settle(view, "Event", epoch=G, chunk_rows=2)
    assert done.settled and done.earlier == 5 and done.written == 2
    assert held(copy)[3] == ("c", G)
    assert source.calls.count("earlier_range") == 4 and "key_range" not in source.calls
    assert len(source.tables["events"]) == len(SOURCE) + 3 * 4


def test_a_write_the_application_makes_while_a_settle_writes_is_the_pause_s() -> None:
    """The settle reads a row from before the advance and writes it over the copy's; between the
    two the application changes it, source and copy, under the map's generation. The settle then
    writes the value it read over the newer one. Both carry the generation, so the pause compares
    the source's row with the copy's and writes the newer one over it."""
    view, source, copy = settled_fixture(SOURCE, COPY)
    real = copy.upsert_in
    fired: list[bool] = []

    def the_application_first(
        table: str, rows: Sequence[Mapping[str, Any]], *, key: Sequence[str]
    ) -> None:
        if not fired:
            fired.append(True)
            source.upsert_in("events", stamped({2: ("b2", G)}), key=["id"])
            real(table, stamped({2: ("b2", G)}), key=key)
        real(table, rows, key=key)

    copy.upsert_in = the_application_first  # type: ignore[method-assign]
    assert settle(view, "Event", epoch=G, chunk_rows=2).settled
    assert held(copy)[2] == ("b", G) and held(source, "events")[2] == ("b2", G)
    done = reconcile_since(view, "Event", since=G, epoch=M, chunk_rows=2)
    assert done.settled and held(copy)[2] == ("b2", M)
    report = compare_stamped(view, "Event", epoch=M, reconciled=done, chunk_rows=2)
    assert report.matched


def test_a_copy_row_written_since_with_another_value_is_written_over_under_the_pause() -> None:
    view, _, copy = settled_fixture(SOURCE, {**COPY, 5: ("z", G)})
    settle(view, "Event", epoch=G, chunk_rows=2)
    assert held(copy)[5] == ("z", G)  # newer than the settle's read: left
    done = reconcile_since(view, "Event", since=G, epoch=M, chunk_rows=2)
    assert done.settled and held(copy)[5] == ("e", M)
    assert compare_stamped(view, "Event", epoch=M, reconciled=done).matched


def test_a_key_the_copy_holds_since_and_the_source_lacks_is_surplus() -> None:
    view, _, copy = settled_fixture(SOURCE, {**COPY, 9: ("left over", G)})
    settle(view, "Event", epoch=G)
    done = reconcile_since(view, "Event", since=G, epoch=M)
    assert done.surplus and not done.settled
    assert 9 in held(copy)  # reported, never deleted


@pytest.mark.parametrize("write", ["copy_in", "upsert_in"])
def test_a_settle_that_could_not_read_back_a_write_is_not_settled(write: str) -> None:
    view, _, copy = settled_fixture(SOURCE, COPY)
    setattr(copy, write, lambda *args, **kwargs: None)
    done = settle(view, "Event", epoch=G)
    assert done.unconfirmed == 1 and not done.settled


def test_a_write_under_the_pause_it_could_not_read_back_is_counted() -> None:
    view, _, copy = settled_fixture(SOURCE, {**COPY, 5: ("z", G)})
    settle(view, "Event", epoch=G)
    copy.upsert_in = lambda *args, **kwargs: None  # type: ignore[method-assign]
    done = reconcile_since(view, "Event", since=G, epoch=M)
    # The source's row since (4) and the copy's (5), neither kept.
    assert done.unconfirmed == 2 and not done.settled


def test_only_engines_that_read_below_a_generation_can_be_settled() -> None:
    view, _, _ = settled_fixture(SOURCE, COPY)
    assert settles_before_a_pause(view, "Event")
    view, _, _ = settled_fixture(SOURCE, COPY, source_engine=Table)
    assert not settles_before_a_pause(view, "Event")


@pytest.mark.parametrize("chunk_rows", [0, -1])
def test_a_chunk_is_some_rows(chunk_rows: int) -> None:
    view, _, _ = settled_fixture(SOURCE, COPY)
    with pytest.raises(MigrationRefused, match="is not a chunk"):
        settle(view, "Event", epoch=G, chunk_rows=chunk_rows)
    with pytest.raises(MigrationRefused, match="is not a chunk"):
        reconcile_since(view, "Event", since=G, epoch=M, chunk_rows=chunk_rows)

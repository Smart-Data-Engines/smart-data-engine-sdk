"""A copy repaired by what differs, and the comparison of what the repair wrote, without an engine.

The two passes of ``sde.reconcile`` are decided here against tables in memory: which rows the first
writes and which it leaves, what it says of a key the source lacks, and what the second finds when
something changes the copy after the first passed over it. The engines keep one rule of the real
ones, which is the fact both passes stand on: while the copy is at the operation's generation,
whatever writes a row stamps it with that generation. ``test_local_cutover_live.py`` holds the same
against PostgreSQL and ClickHouse, with their own constraints doing the stamping.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

import pytest

import sde
from sde.generation import EPOCH_COLUMN
from sde.inspection import InspectionContext
from sde.reconcile import (
    Reconciliation,
    compare_stamped,
    copy_is_mostly_there,
    reconcile,
    repairs_what_differs,
)
from sde.testing.loader import model_from_neutral
from sde.testing.memory import MemoryEngine

PROJECT = "1" * 32
BEFORE = 4
"""The generation the copy's rows were written under, by fan-out and by a backfill."""
OPERATION = 5
"""The cutover's own generation: the only one the copy admits while it is repaired."""


class Table(MemoryEngine):
    """An engine in memory that an operator can write over and read by generation and by keys.

    A key is one row here, as in the real engines, where ``MemoryEngine.copy_in`` only absorbs a
    recopy of the same row. ``hidden`` is a key its windows do not show, which is how an engine
    that orders a key differently looks from the outside.
    """

    def __init__(self, name: str, rows: Sequence[Mapping[str, Any]], *, table: str) -> None:
        super().__init__(name=name, tables={table: rows})
        self.hidden: set[tuple[Any, ...]] = set()
        self.calls: list[str] = []
        self.asked: list[list[tuple[Any, ...]]] = []
        self.key_range = self._windowed  # type: ignore[method-assign]
        self.copy_in = self._copied  # type: ignore[method-assign]

    def _windowed(self, table: str, order: Sequence[str], **bounds: Any) -> list[dict[str, Any]]:
        self.calls.append("key_range")
        rows = self._key_range(table, order, **bounds)
        return [row for row in rows if tuple(row[c] for c in order) not in self.hidden]

    def _copied(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        self.calls.append("copy_in")
        held = {row["id"] for row in self.tables[table]}
        assert not held & {row["id"] for row in rows}, "copy_in was given a key that is there"
        self.tables[table].extend(dict(row) for row in rows)

    def upsert_in(
        self, table: str, rows: Sequence[Mapping[str, Any]], *, key: Sequence[str]
    ) -> None:
        self.calls.append("upsert_in")
        replaced = {tuple(row[c] for c in key) for row in rows}
        kept = [r for r in self.tables[table] if tuple(r[c] for c in key) not in replaced]
        self.tables[table] = kept + [dict(row) for row in rows]

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
        rows = sorted(
            (row for row in self.tables[table] if row[EPOCH_COLUMN] == epoch),
            key=lambda row: tuple(row[c] for c in order),
        )
        if after is not None:
            rows = [row for row in rows if tuple(row[c] for c in order) > tuple(after)]
        return [dict(row) for row in (rows if limit is None else rows[:limit])]

    def rows_for_keys(
        self, table: str, order: Sequence[str], keys: Sequence[Sequence[Any]]
    ) -> list[dict[str, Any]]:
        self.calls.append("rows_for_keys")
        self.asked.append([tuple(key) for key in keys])
        wanted = {tuple(key) for key in keys}
        return [dict(r) for r in self.tables[table] if tuple(r[c] for c in order) in wanted]


class Streaming(Table):
    """A source that also answers its rows in key order as one stream, as ClickHouse does."""

    opened = closed = 0
    backwards = False
    """Whether the stream comes in an order this process does not share: here, the reverse."""

    def key_stream(
        self,
        table: str,
        order: Sequence[str],
        *,
        chunk_rows: int,
        after: Sequence[Any] | None = None,
    ) -> Iterator[list[dict[str, Any]]]:
        # Kept, as a driver keeps its response: a stream nobody closes stays open, where a
        # generator nothing refers to would be closed by the interpreter and hide a missing close.
        self.started = [*getattr(self, "started", []), after]
        self.streams = [
            *getattr(self, "streams", []), self._stream(table, order, chunk_rows, after)
        ]
        return self.streams[-1]

    def _stream(
        self, table: str, order: Sequence[str], chunk_rows: int, after: Sequence[Any] | None = None
    ) -> Iterator[list[dict[str, Any]]]:
        self.calls.append("key_stream")
        self.opened += 1
        try:
            rows = sorted(
                (
                    row
                    for row in self.tables[table]
                    if after is None or tuple(row[c] for c in order) > tuple(after)
                ),
                key=lambda row: tuple(row[c] for c in order),
                reverse=self.backwards,
            )
            for start in range(0, len(rows), chunk_rows):
                yield [dict(row) for row in rows[start : start + chunk_rows]]
        finally:
            self.closed += 1


class CutShort(Streaming):
    """An engine whose stream ends after its first chunk and raises nothing: how an answer cut at
    a block's edge looks through a driver that takes the end of the bytes for the end of the rows.
    """

    def _stream(
        self, table: str, order: Sequence[str], chunk_rows: int, after: Sequence[Any] | None = None
    ) -> Iterator[list[dict[str, Any]]]:
        chunks = super()._stream(table, order, chunk_rows, after)
        try:
            yield next(chunks)
        finally:
            chunks.close()  # type: ignore[attr-defined]


EARLIER = 3
"""A generation before the copy's: what the source's own rows carry."""


def stamped(rows: Mapping[int, str], epoch: int = BEFORE) -> list[dict[str, Any]]:
    return [{"id": key, "value": value, EPOCH_COLUMN: epoch} for key, value in rows.items()]


def fixture(
    source: Mapping[int, str],
    copy: Mapping[int, str],
    *,
    source_engine: type[Table] = Table,
    copy_engine: type[Table] = Table,
    one_engine: bool = False,
) -> tuple[InspectionContext, Table, Table]:
    sde.clear_registry()
    model = model_from_neutral(
        {
            "entities": [
                {
                    "name": "Event",
                    "fields": [
                        {"name": "id", "type": "int32"},
                        {"name": "value", "type": "string"},
                    ],
                    "key": ["id"],
                }
            ]
        }
    )

    def material(identity: str, table: str) -> dict[str, Any]:
        return {
            "id": identity,
            "engine": identity,
            "layout": {
                "tables": {"Event": table},
                "columns": {"Event": {"id": "integer", "value": "text"}},
            },
        }

    placement = sde.load_map(
        {
            "contract": 4,
            "project_id": PROJECT,
            "model_version": model.version,
            "map_version": 2,
            "groups": {
                "Event": {
                    "write_epoch": BEFORE,
                    "source": material("source", "events"),
                    "derived": [{**material("copy", "events_copy"), "lag_budget_ms": 1000}],
                    "also_write": ["copy"],
                }
            },
        },
        model=model,
    )
    left = source_engine("source", stamped(source, EARLIER), table="events")
    right = copy_engine("copy", stamped(copy), table="events_copy")
    if one_engine:
        # A relayout: the source's table and the copy's are in one engine, through one adapter.
        left.tables["events_copy"] = right.tables["events_copy"]
        right = left
    engines = {"source": left, "copy": right}
    return InspectionContext(model, placement, engines, PROJECT), left, right


def held(table: Table, name: str = "events_copy") -> dict[int, tuple[str, int]]:
    rows = table.tables[name]
    return {row["id"]: (row["value"], row[EPOCH_COLUMN]) for row in rows}


def group_of_two(
    events: tuple[Mapping[int, str], Mapping[int, str]],
    notes: tuple[Mapping[int, str], Mapping[int, str]],
) -> tuple[InspectionContext, Table, Table]:
    """Two entities that commit together, so one group and one copy of two tables. Each argument
    is an entity's rows in the source and in the copy."""
    sde.clear_registry()
    fields = [{"name": "id", "type": "int32"}, {"name": "value", "type": "string"}]
    model = model_from_neutral(
        {
            "entities": [
                {"name": "Event", "fields": fields, "key": ["id"]},
                {"name": "Note", "fields": fields, "key": ["id"]},
            ],
            "atomic": [["Event", "Note"]],
        }
    )
    columns = {"id": "integer", "value": "text"}

    def material(identity: str, suffix: str) -> dict[str, Any]:
        return {
            "id": identity,
            "engine": identity,
            "layout": {
                "tables": {"Event": "events" + suffix, "Note": "notes" + suffix},
                "columns": {"Event": columns, "Note": columns},
            },
        }

    placement = sde.load_map(
        {
            "contract": 4,
            "project_id": PROJECT,
            "model_version": model.version,
            "map_version": 2,
            "groups": {
                "Event": {
                    "write_epoch": BEFORE,
                    "source": material("source", ""),
                    "derived": [{**material("copy", "_copy"), "lag_budget_ms": 1000}],
                    "also_write": ["copy"],
                }
            },
        },
        model=model,
    )
    left = Table("source", stamped(events[0], EARLIER), table="events")
    left.tables["notes"] = stamped(notes[0], EARLIER)
    right = Table("copy", stamped(events[1]), table="events_copy")
    right.tables["notes_copy"] = stamped(notes[1])
    engines = {"source": left, "copy": right}
    return InspectionContext(model, placement, engines, PROJECT), left, right


ROWS = {1: "a", 2: "b", 3: "c", 4: "d", 5: "e"}


def test_an_equal_copy_is_read_and_not_written() -> None:
    """Equal but for the generation each side's rows carry, which is not part of a row: the
    source's were written under an earlier one than the copy's."""
    view, source, copy = fixture(ROWS, ROWS)
    assert {row[EPOCH_COLUMN] for row in source.tables["events"]} == {EARLIER}
    assert {generation for _, generation in held(copy).values()} == {BEFORE}
    done = reconcile(view, "Event", epoch=OPERATION, chunk_rows=2)
    assert done == Reconciliation(chunks=3, rows=5, written=0, surplus=False) and done.settled
    assert "copy_in" not in copy.calls and "upsert_in" not in copy.calls
    report = compare_stamped(view, "Event", epoch=OPERATION, reconciled=done, chunk_rows=2)
    assert report.matched and (report.rows_source, report.rows_target) == (5, 5)
    assert (report.chunks_compared, report.chunks_mismatched) == (3, 0)


def test_a_missing_row_is_copied_a_differing_one_written_over_and_the_rest_left() -> None:
    view, _, copy = fixture(ROWS, {1: "a", 2: "WRONG", 4: "d"})
    done = reconcile(view, "Event", epoch=OPERATION, chunk_rows=2)
    assert done == Reconciliation(chunks=3, rows=5, written=3, surplus=False)
    assert held(copy) == {
        1: ("a", BEFORE),
        2: ("b", OPERATION),
        3: ("c", OPERATION),
        4: ("d", BEFORE),
        5: ("e", OPERATION),
    }
    # The row that was missing went by the engine's copy, the one that was there by a write over.
    assert copy.calls.count("copy_in") == 2 and copy.calls.count("upsert_in") == 1
    report = compare_stamped(view, "Event", epoch=OPERATION, reconciled=done, chunk_rows=2)
    assert report.matched and (report.rows_source, report.rows_target) == (5, 5)
    # Three chunks of the first pass, and the three stamped rows in two chunks of the second.
    assert (report.chunks_compared, report.chunks_mismatched) == (5, 0)


def test_a_key_the_source_lacks_is_reported_and_never_deleted() -> None:
    view, _, copy = fixture(ROWS, {**ROWS, 9: "left over"})
    done = reconcile(view, "Event", epoch=OPERATION)
    assert done.surplus and done.written == 0 and not done.settled
    assert 9 in held(copy)


def test_a_row_the_window_does_not_show_is_looked_up_before_it_is_rewritten() -> None:
    view, _, copy = fixture(ROWS, ROWS)
    copy.hidden = {(3,)}
    done = reconcile(view, "Event", epoch=OPERATION)
    assert done.written == 0 and held(copy)[3] == ("c", BEFORE)
    assert "rows_for_keys" in copy.calls


@pytest.mark.parametrize(
    ("tamper", "columns"),
    [
        (lambda copy: copy.upsert_in("events_copy", stamped({2: "x"}, OPERATION), key=["id"]),
         ("value",)),
        (lambda copy: copy.upsert_in("events_copy", stamped({4: "x"}, OPERATION), key=["id"]),
         ("value",)),
        (lambda copy: copy.upsert_in("events_copy", stamped({9: "new"}, OPERATION), key=["id"]),
         ("id", "value")),
    ],
    ids=["a repaired row changed", "an untouched row changed", "a row the source lacks added"],
)
def test_a_write_after_the_first_pass_carries_the_generation_and_is_compared(
    tamper: Callable[[Table], None], columns: tuple[str, ...]
) -> None:
    """Whatever writes the copy while it is repaired stamps the operation's generation - the engine
    admits nothing else - so the second pass reads it, wherever the first pass had already been."""
    view, _, copy = fixture(ROWS, {1: "a", 2: "WRONG", 4: "d"})
    done = reconcile(view, "Event", epoch=OPERATION)
    tamper(copy)
    report = compare_stamped(view, "Event", epoch=OPERATION, reconciled=done)
    assert not report.matched and report.chunks_mismatched == 1
    (difference,) = report.differences
    assert difference.columns == columns and difference.table == "events_copy"


@pytest.mark.parametrize("write", ["copy_in", "upsert_in"])
def test_a_write_the_engine_answered_and_did_not_keep_is_counted(write: str) -> None:
    """The second pass finds a repaired row by its generation. A write that was dropped leaves the
    old row under its old generation, so the first pass reads back what it wrote and says so."""
    view, _, copy = fixture(ROWS, {1: "a", 2: "WRONG", 4: "d"})
    setattr(copy, write, lambda *args, **kwargs: None)
    done = reconcile(view, "Event", epoch=OPERATION)
    assert done.written == 3 and done.unconfirmed == (2 if write == "copy_in" else 1)
    assert not done.settled and not done.surplus


def test_a_write_kept_under_another_generation_is_counted() -> None:
    view, _, copy = fixture(ROWS, {1: "a", 2: "WRONG", 4: "d"})
    kept = copy.upsert_in

    def restamped(table: str, rows: Sequence[Mapping[str, Any]], *, key: Sequence[str]) -> None:
        kept(table, [{**row, EPOCH_COLUMN: BEFORE} for row in rows], key=key)

    copy.upsert_in = restamped  # type: ignore[method-assign]
    done = reconcile(view, "Event", epoch=OPERATION)
    assert done.unconfirmed == 1 and held(copy)[2] == ("b", BEFORE)


def test_a_row_deleted_after_the_first_pass_is_in_the_counts() -> None:
    view, _, copy = fixture(ROWS, ROWS)
    done = reconcile(view, "Event", epoch=OPERATION)
    copy.tables["events_copy"] = [r for r in copy.tables["events_copy"] if r["id"] != 4]
    report = compare_stamped(view, "Event", epoch=OPERATION, reconciled=done)
    # Nothing stamped differs, so the comparison alone matches: the counts are what a frozen
    # verification adds, and it requires them equal.
    assert report.matched and (report.rows_source, report.rows_target) == (5, 4)


def test_every_engine_call_of_the_first_pass_goes_through_the_callers_wrapper() -> None:
    view, source, copy = fixture(ROWS, {1: "a"})
    seen: list[str] = []

    def call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        seen.append(function.__name__)
        return function(*args, **kwargs)

    reconcile(view, "Event", epoch=OPERATION, chunk_rows=5, call=call)
    # One chunk and the read that finds the end, the first beside the copy's window; the four
    # missing keys looked up, copied and read back; and the two counts that find a key the
    # source lacks.
    assert seen == [
        "_windowed", "_windowed", "rows_for_keys", "_copied", "rows_for_keys",
        "_windowed", "_count", "_count",
    ]
    assert source.calls.count("key_range") == 2 and copy.calls.count("key_range") == 1


def test_a_source_that_streams_is_read_by_one_stream_and_gives_the_same_repair() -> None:
    copy_rows = {1: "a", 2: "WRONG", 4: "d"}
    view, _, plain = fixture(ROWS, copy_rows)
    expected = reconcile(view, "Event", epoch=OPERATION, chunk_rows=2)
    view, source, copy = fixture(ROWS, copy_rows, source_engine=Streaming)
    assert reconcile(view, "Event", epoch=OPERATION, chunk_rows=2) == expected
    assert held(copy) == held(plain)
    assert "key_range" not in source.calls and source.calls.count("key_stream") == 1
    assert isinstance(source, Streaming) and (source.opened, source.closed) == (1, 1)


def test_a_stream_is_closed_when_the_pass_stops_early() -> None:
    """A stream holds its engine until it ends, so a pass that fails has to end it."""
    view, source, copy = fixture(ROWS, {1: "a"}, source_engine=Streaming)

    def refused(*_args: Any, **_kwargs: Any) -> None:
        raise sde.EngineError("the copy's engine went away")

    copy.copy_in = refused  # type: ignore[method-assign]
    with pytest.raises(sde.EngineError, match="went away"):
        reconcile(view, "Event", epoch=OPERATION, chunk_rows=2)
    assert isinstance(source, Streaming) and (source.opened, source.closed) == (1, 1)


@pytest.mark.parametrize("backwards", [False, True], ids=["in key order", "in another order"])
def test_a_copy_that_streams_is_read_beside_the_source_and_gives_the_same_repair(
    backwards: bool,
) -> None:
    """The copy's rows come from one stream, read forward as the source is. Where the stream's
    order is not the one this process compares keys by, a row it has not reached is looked up by
    its key: the repair is the same, and no row that was right is written again."""
    copy_rows = {1: "a", 2: "WRONG", 4: "d", 5: "e"}
    view, _, plain = fixture(ROWS, copy_rows)
    expected = reconcile(view, "Event", epoch=OPERATION, chunk_rows=2)
    assert expected.written == 2
    view, _, copy = fixture(ROWS, copy_rows, copy_engine=Streaming)
    assert isinstance(copy, Streaming)
    copy.backwards = backwards
    assert reconcile(view, "Event", epoch=OPERATION, chunk_rows=2) == expected
    assert held(copy) == held(plain)
    assert "key_range" not in copy.calls and (copy.opened, copy.closed) == (1, 1)
    if not backwards:
        # Read forward beside the source, the stream shows every row that is there in the chunk
        # that asks for it: the only keys looked up are the row the copy lacks, once to find it
        # missing, and the two rows written, to read them back.
        assert copy.asked == [[(2,)], [(3,)], [(3,)]]
    else:
        assert len(copy.asked) > 3


def test_both_streams_are_closed_when_the_pass_stops_early() -> None:
    """Both are part-read when the copy refuses the first chunk's write: the source has chunks
    left, and the copy's stream has shown a row past the chunk and is waiting to be asked on."""
    view, source, copy = fixture(
        ROWS, {1: "a", 4: "d", 5: "e"}, source_engine=Streaming, copy_engine=Streaming
    )

    def refused(*_args: Any, **_kwargs: Any) -> None:
        raise sde.EngineError("the copy's engine went away")

    copy.copy_in = refused  # type: ignore[method-assign]
    with pytest.raises(sde.EngineError, match="went away"):
        reconcile(view, "Event", epoch=OPERATION, chunk_rows=2)
    for engine in (source, copy):
        assert isinstance(engine, Streaming) and (engine.opened, engine.closed) == (1, 1)


@pytest.mark.parametrize("walk", ["one stream", "a statement a chunk"])
def test_a_walk_that_ends_before_the_source_does_is_said_and_not_taken_for_its_end(
    walk: str,
) -> None:
    """The copy holds every key and differs in a row the walk never reached. Nothing is written,
    so nothing carries the generation, and the counts are equal: the second pass, which reads what
    the first wrote, has nothing to find. Only the first pass can say it did not read the whole
    source, and it holds itself to the source's count to say it."""
    wrong_past_the_walk = {**ROWS, 5: "WRONG"}
    if walk == "one stream":
        view, source, copy = fixture(ROWS, wrong_past_the_walk, source_engine=CutShort)
    else:
        view, source, copy = fixture(ROWS, wrong_past_the_walk)
        answer = source.key_range

        def first_chunk_only(table: str, order: Sequence[str], **bounds: Any) -> Any:
            return answer(table, order, **bounds) if bounds.get("after") is None else []

        source.key_range = first_chunk_only  # type: ignore[method-assign]
    done = reconcile(view, "Event", epoch=OPERATION, chunk_rows=2)
    assert (done.chunks, done.rows, done.written) == (1, 2, 0)
    assert (done.surplus, done.unconfirmed) == (False, 0)
    assert done.partial and not done.settled
    assert done.as_record()["partial"] is True
    assert held(copy)[5] == ("WRONG", BEFORE)
    # Why the first pass has to say it: the comparison of what was written sees nothing wrong.
    report = compare_stamped(view, "Event", epoch=OPERATION, reconciled=done)
    assert report.matched and report.rows_source == report.rows_target == 5


@pytest.mark.parametrize("counted", [4, 6], ids=["more rows read than counted", "fewer"])
def test_a_walk_is_the_source_only_when_it_read_as_many_rows_as_the_source_counts(
    counted: int,
) -> None:
    """Either way round: a source that counts fewer rows than were read was not still, or answered
    a row twice, and is no more the table the pass compared than one that counts more."""
    view, source, _ = fixture(ROWS, ROWS)
    source.count = lambda _table: counted  # type: ignore[method-assign]
    done = reconcile(view, "Event", epoch=OPERATION, chunk_rows=2)
    assert done.rows == 5 and done.partial and not done.settled
    # A copy with more rows than the source says it holds is a surplus as well, as before.
    assert done.surplus is (counted == 4)


def test_a_copy_whose_stream_ends_early_costs_lookups_and_changes_nothing() -> None:
    """The copy's stream does not have to be whole, as it does not have to be in order: a row it
    has not shown is looked up by its key before it is called missing."""
    copy_rows = {1: "a", 2: "b", 3: "WRONG", 5: "e"}
    view, _, plain = fixture(ROWS, copy_rows)
    expected = reconcile(view, "Event", epoch=OPERATION, chunk_rows=2)
    assert (expected.written, expected.settled) == (2, True)
    view, _, copy = fixture(ROWS, copy_rows, copy_engine=CutShort)
    assert reconcile(view, "Event", epoch=OPERATION, chunk_rows=2) == expected
    assert held(copy) == held(plain)
    # Rows 3 and 5 are there and past the stream's end: found by key, the first to be written
    # over and the second to be left as it was.
    assert held(copy)[5] == ("e", BEFORE) and held(copy)[3] == ("c", OPERATION)
    assert held(copy)[4] == ("d", OPERATION)


def test_a_group_of_two_entities_is_walked_counted_and_compared_an_entity_at_a_time() -> None:
    """Each entity's walk starts at its own first key and is held to its own source's count, and
    the second pass pages each entity's rows by themselves: a key of one says nothing of the
    other's, though here they are the same numbers."""
    view, _, copy = group_of_two(
        events=({1: "a", 2: "b", 3: "c"}, {1: "a", 2: "WRONG"}),
        notes=({1: "w", 2: "x", 3: "y", 4: "z"}, {2: "x", 3: "WRONG", 4: "z"}),
    )
    done = reconcile(view, "Event", epoch=OPERATION, chunk_rows=2)
    # Two chunks an entity; of each, one row written over and one copied.
    assert done == Reconciliation(chunks=4, rows=7, written=4, surplus=False) and done.settled
    assert held(copy) == {1: ("a", BEFORE), 2: ("b", OPERATION), 3: ("c", OPERATION)}
    assert held(copy, "notes_copy") == {
        1: ("w", OPERATION),
        2: ("x", BEFORE),
        3: ("y", OPERATION),
        4: ("z", BEFORE),
    }
    # The copy's window beside an entity's first chunk starts at that entity's beginning, not past
    # the last key of the entity before it: the only keys looked up are the two rows the copy
    # lacks, each once to find it missing, and the four rows written, each to read it back.
    assert copy.asked == [[(2,)], [(3,)], [(3,)], [(1,)], [(1,)], [(3,)]]
    report = compare_stamped(view, "Event", epoch=OPERATION, reconciled=done, chunk_rows=1)
    assert report.matched and (report.rows_source, report.rows_target) == (7, 7)
    # The first pass's four chunks, and the four stamped rows one a chunk.
    assert (report.chunks_compared, report.chunks_mismatched) == (8, 0)
    # A write into the second entity's table after the first pass is found there.
    copy.upsert_in("notes_copy", stamped({4: "changed"}, OPERATION), key=["id"])
    report = compare_stamped(view, "Event", epoch=OPERATION, reconciled=done, chunk_rows=1)
    (difference,) = report.differences
    assert not report.matched and (difference.entity, difference.table) == ("Note", "notes_copy")


def test_a_short_chunk_is_the_end_of_a_source_read_by_chunks() -> None:
    """No statement is spent on finding the table empty: under a pause every one is time. Five
    rows two a chunk are three reads, and a table that ends on a full chunk needs the fourth."""
    view, source, _ = fixture(ROWS, ROWS)
    assert reconcile(view, "Event", epoch=OPERATION, chunk_rows=2).chunks == 3
    assert source.calls.count("key_range") == 3
    view, source, _ = fixture({1: "a", 2: "b", 3: "c", 4: "d"}, {1: "a", 2: "b", 3: "c", 4: "d"})
    assert reconcile(view, "Event", epoch=OPERATION, chunk_rows=2).chunks == 2
    assert source.calls.count("key_range") == 3


def test_a_relayout_reads_its_one_engine_by_chunks_and_not_by_a_stream() -> None:
    """Source and copy in one engine, through one adapter: a stream would hold it against the
    reads and writes of the copy beside it."""
    view, source, copy = fixture(
        ROWS, {1: "a", 2: "WRONG", 4: "d"}, source_engine=Streaming, one_engine=True
    )
    assert source is copy
    done = reconcile(view, "Event", epoch=OPERATION, chunk_rows=2)
    assert done.written == 3 and done.settled
    assert "key_stream" not in source.calls
    report = compare_stamped(view, "Event", epoch=OPERATION, reconciled=done)
    assert report.matched and (report.rows_source, report.rows_target) == (5, 5)


def test_an_engine_that_cannot_be_written_over_is_not_asked_to() -> None:
    view, source, copy = fixture(ROWS, ROWS)
    assert repairs_what_differs(view, "Event")
    for engine, method in ((copy, "upsert_in"), (copy, "stamped_range"), (source, "rows_for_keys")):
        original = getattr(type(engine), method)
        setattr(engine, method, None)
        assert not repairs_what_differs(view, "Event"), method
        setattr(engine, method, original.__get__(engine))
    assert repairs_what_differs(view, "Event")


@pytest.mark.parametrize(("there", "worth_it"), [(0, False), (2, False), (3, True), (5, True)])
def test_a_copy_under_half_full_is_refilled_rather_than_compared(
    there: int, worth_it: bool
) -> None:
    view, _, _ = fixture(ROWS, dict(list(ROWS.items())[:there]))
    assert copy_is_mostly_there(view, "Event") is worth_it


def test_a_row_is_the_same_by_the_values_the_source_holds() -> None:
    """The rule of the whole comparison (format contract 7e), for one row and without rebuilding
    it: NaN is NaN, a zero is a zero of either sign, the generation is not a value, a column only
    the copy has is not the source's business, and one the copy lacks is a difference."""
    from sde.reconcile import _same_row

    nan = float("nan")
    source = {"id": 1, "x": nan, "z": 0.0, "t": "a", EPOCH_COLUMN: EARLIER}
    assert _same_row(source, {"id": 1, "x": float("nan"), "z": -0.0, "t": "a", EPOCH_COLUMN: 9})
    assert _same_row(source, {**source, "added_by_the_client": 1})
    assert not _same_row(source, {"id": 1, "x": nan, "z": 0.0, EPOCH_COLUMN: EARLIER})
    for column, other in (("id", 2), ("x", 1.0), ("z", 1.0), ("t", "b"), ("t", None)):
        assert not _same_row(source, {**source, column: other}), column
        # And past a NaN that is not the same object, which is how a row read from an engine has it.
        assert not _same_row(source, {**source, "x": float("nan"), column: other}), column


def test_a_chunk_that_is_not_one_is_refused() -> None:
    view, _, _ = fixture(ROWS, ROWS)
    with pytest.raises(sde.MigrationRefused, match="is not a chunk"):
        reconcile(view, "Event", epoch=OPERATION, chunk_rows=0)
    with pytest.raises(sde.MigrationRefused, match="is not a chunk"):
        compare_stamped(
            view, "Event", epoch=OPERATION, reconciled=Reconciliation(0, 0, 0, False), chunk_rows=0
        )

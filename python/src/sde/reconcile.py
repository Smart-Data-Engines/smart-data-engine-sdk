"""Repair a copy by what differs, then compare what the repair wrote.

A cutover used to empty the copy and refill it from the frozen source, then read both whole again
to compare them: two reads of the source and a write of every row, inside the pause, for a copy
that the library's fan-out and the backfill had already made equal but for a few rows. This module
is the other way to the same guarantee, in two passes that the local cutover runs in the places
of those two.

**The fact both passes stand on.** While a cutover repairs, the copy's tables are at the
operation's own write generation and admit a write of that generation only (the ``min`` and ``max``
constraints of :mod:`sde.write_fence`), an update included. So every row of the copy written or
changed since then carries that generation, and a row carrying any other is as it was. A deletion
leaves no mark, and changes the count.

- :func:`reconcile` walks the frozen source in key order and reads the copy beside it, value by
  value, as :func:`sde.migration.verify` does. A row the copy lacks or holds differently is written
  over it, stamped with the operation's generation, and read back: a write the engine answered
  and did not keep is counted, never assumed. Nothing is emptied and no row is deleted.
- :func:`compare_stamped`, under the final barriers on both sides, reads every row of the copy that
  carries the generation and compares it with the source's, and counts both sides.

Together they say what the full comparison said. At the decision, a row of the copy either carries
the generation, and was compared under the final barriers; or carries another, so it has not
changed since the first pass compared it with a source frozen since before that pass - had it
differed, the pass wrote over it and read it back carrying the generation. The first pass visits
every key of the source, so the copy holds each; equal counts then leave no room for a key the
source lacks or for a row deleted after it was compared. Nothing is inferred from a count or a
checksum: the counts only find what a walk of the source cannot see.

Three things the first pass does not settle, and says: a copy that holds a key the source lacks
(``surplus``), since removing it would be a deletion; a write it could not read back
(``unconfirmed``); and a walk that read another number of rows than the source counts
(``partial``), since "visits every key" is then not what happened, and a row the walk did not
reach is one neither pass compares. After any of them the caller refills the copy whole and
compares it whole, as before.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from operator import itemgetter
from typing import Any, Protocol, cast

from .errors import MigrationRefused
from .generation import EPOCH_COLUMN
from .inspection import MigrationView
from .migration import (
    _ABSENT,
    _DIFFERENCES_KEPT,
    CHUNK_ROWS,
    Difference,
    VerifyReport,
    _Copy,
    _differing_columns,
    _logical_copy_row,
    _plan,
    _same_value,
    _stamp,
    source_chunks,
)
from .verification import VerificationRequest

__all__ = [
    "Reconcilable",
    "Reconciliation",
    "Settlement",
    "compare_stamped",
    "copy_is_mostly_there",
    "reconcile",
    "reconcile_since",
    "repairs_what_differs",
    "settle",
    "settles_before_a_pause",
]


class Reconcilable(Protocol):
    """What an engine offers, beyond ``Migratable``, for a repair of what differs."""

    def upsert_in(
        self, table: str, rows: Sequence[Mapping[str, Any]], *, key: Sequence[str]
    ) -> None: ...

    def stamped_range(
        self,
        table: str,
        order: Sequence[str],
        *,
        epoch: int,
        after: Sequence[Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]: ...

    def rows_for_keys(
        self, table: str, order: Sequence[str], keys: Sequence[Sequence[Any]]
    ) -> list[dict[str, Any]]: ...

    def earlier_range(
        self,
        table: str,
        order: Sequence[str],
        *,
        epoch: int,
        after: Sequence[Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]: ...


_NEEDED = ("upsert_in", "stamped_range", "rows_for_keys")


def repairs_what_differs(view: MigrationView, group: str) -> bool:
    """Whether every engine of this group's copy can take part: the source to be read by keys, the
    copy to be written over and read by generation. An engine that cannot is not an error - the
    caller refills its copy whole."""
    for copy in _plan(view, group):
        if copy.write_epoch is None:
            return False
        if not callable(getattr(copy.source, "rows_for_keys", None)):
            return False
        if not all(callable(getattr(copy.target, name, None)) for name in _NEEDED):
            return False
    return True


def copy_is_mostly_there(
    view: MigrationView, group: str, *, call: Callable[..., Any] | None = None
) -> bool:
    """Whether every table of the copy holds at least half the rows of its source.

    A choice of the faster way, never of the outcome: both repairs end in a comparison. Comparing
    a copy that is mostly empty reads it for nothing and then reads the source by keys for every
    row written, which a refill followed by the full comparison does by range. Half is where the
    two cost about the same; a copy filled before the pause is far above it and one never filled
    far below.
    """
    call = call or _direct
    for copy in _plan(view, group):
        there = call(copy.target.count, copy.target_table)
        wanted = call(copy.source.count, copy.source_table)
        if 2 * there < wanted:
            return False
    return True


@dataclass(frozen=True)
class Reconciliation:
    """What the first pass did, in numbers: nothing here names a key or holds a value."""

    chunks: int
    rows: int
    written: int
    surplus: bool
    """The copy holds more rows than the source after the pass: a key the source lacks."""
    unconfirmed: int = 0
    """Rows written and then not read back as written, under the operation's generation."""
    partial: bool = False
    """The walk of a source read another number of rows than that source counts: a stream that
    ended before the table did, or a source that was not still. What the walk did not read, this
    pass did not compare, and the second pass reads only what this one wrote."""

    @property
    def settled(self) -> bool:
        """Whether the copy can be compared by the rows this pass wrote. Otherwise it is refilled
        whole and compared whole."""
        return not self.surplus and not self.partial and self.unconfirmed == 0

    def as_record(self) -> dict[str, Any]:
        return {
            "chunks": self.chunks,
            "rows": self.rows,
            "written": self.written,
            "surplus": self.surplus,
            "unconfirmed": self.unconfirmed,
            "partial": self.partial,
        }


def _direct(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return function(*args, **kwargs)


def _key(copy: _Copy, row: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(row[column] for column in copy.key)


def _identity(copy: _Copy) -> Callable[[Mapping[str, Any]], Any]:
    """A row's key as something to look rows up by: the value itself for a key of one column, a
    tuple for more. Computed for every row of both sides, so by ``itemgetter`` and once a row."""
    return itemgetter(*copy.key)


def _same_row(source: Mapping[str, Any], target: Mapping[str, Any]) -> bool:
    """Whether the copy's row holds what the source's holds, the write generation aside.

    Over the source's columns, as ``_differing_columns`` compares and for its reason, and with its
    rule for a value (:func:`sde.migration._same_value`). Written out because it runs for every
    row of the group inside the pause: no row is rebuilt without its generation to be compared,
    and equal values are found equal before anything else is asked.
    """
    for column, value in source.items():
        if column == EPOCH_COLUMN:
            continue
        other = target.get(column, _ABSENT)
        if other is value or other == value:
            continue
        if not _same_value(value, other):
            return False
    return True


class _Mirror:
    """The copy's rows beside each chunk of the source: those up to the chunk's last key.

    By one streamed statement where the copy's engine offers it and is not also the source's, read
    forward as the source is; otherwise by a range read a chunk. Neither has to be exact. A row
    handed out that the chunk does not ask for is ignored, and a row of the chunk that is not
    handed out is looked up by its key before it is called missing - which is also what makes a
    stream safe where the two engines, or this process, order a key differently.
    """

    def __init__(self, copy: _Copy, chunk_rows: int, call: Callable[..., Any]) -> None:
        self._copy, self._call = copy, call
        self._identity = _identity(copy)
        stream = getattr(copy.target, "key_stream", None)
        self._chunks: Generator[list[dict[str, Any]], None, None] | None = (
            stream(copy.target_table, copy.key, chunk_rows=chunk_rows)
            if callable(stream) and copy.source is not copy.target
            else None
        )
        self._read: list[dict[str, Any]] = []
        self._next = 0

    def upto(self, after: tuple[Any, ...] | None, high: tuple[Any, ...]) -> list[dict[str, Any]]:
        copy = self._copy
        if self._chunks is None:
            rows: list[dict[str, Any]] = self._call(
                copy.target.key_range, copy.target_table, copy.key, after=after, upto=high
            )
            return rows
        last = self._identity(dict(zip(copy.key, high, strict=True)))
        out: list[dict[str, Any]] = []
        while True:
            while self._next < len(self._read):
                row = self._read[self._next]
                try:
                    if self._identity(row) > last:
                        return out
                except TypeError:
                    pass  # Keys this process cannot order: handed out, and found by key if not.
                out.append(row)
                self._next += 1
            chunk = self._call(next, self._chunks, None)
            if not chunk:
                return out
            self._read, self._next = chunk, 0

    def close(self) -> None:
        if self._chunks is not None:
            self._chunks.close()


def reconcile(
    view: MigrationView,
    group: str,
    *,
    epoch: int,
    chunk_rows: int = CHUNK_ROWS,
    call: Callable[..., Any] = _direct,
) -> Reconciliation:
    """Compare the copy with the source, chunk by chunk, and write over what differs.

    The caller holds the source frozen and the copy at ``epoch`` with nobody else admitted; this
    function checks neither, as the refill it replaces checked neither. What it does check is its
    own walk: the rows it read of each source against that source's count. ``call`` wraps every
    engine call, which is where a cutover checks its pause budget.

    A source row the copy's window does not show is looked up by key before it is called missing:
    two engines may order one key differently, and a row that is there must not be rewritten for
    it - a rewritten row is one more for the second pass to compare.
    """
    if chunk_rows < 1:
        raise MigrationRefused(f"a chunk of {chunk_rows} rows is not a chunk")
    chunks = rows = written = unconfirmed = 0
    surplus = partial = False
    for copy in _plan(view, group):
        target = cast(Reconcilable, copy.target)
        after: tuple[Any, ...] | None = None
        walked = 0
        mirror = _Mirror(copy, chunk_rows, call)
        source_chunks_ = source_chunks(copy, chunk_rows, call=call)
        try:
            for wanted in source_chunks_:
                done = _repair_chunk(copy, target, mirror, wanted, after, epoch, call)
                written += done[0]
                unconfirmed += done[1]
                chunks += 1
                walked += len(wanted)
                after = _key(copy, wanted[-1])
        finally:
            source_chunks_.close()
            mirror.close()
        rows += walked
        there = call(copy.target.count, copy.target_table)
        counted = call(copy.source.count, copy.source_table)
        if there > counted:
            surplus = True
        # The walk is every row of the source only if it read as many as the source counts. An
        # answer that ended early without an error would otherwise pass for the table's end, and
        # the rows after it be compared by nobody: the second pass reads what this one wrote.
        if walked != counted:
            partial = True
    return Reconciliation(
        chunks=chunks,
        rows=rows,
        written=written,
        surplus=surplus,
        unconfirmed=unconfirmed,
        partial=partial,
    )


def _repair_chunk(
    copy: _Copy,
    target: Reconcilable,
    mirror: _Mirror,
    wanted: list[dict[str, Any]],
    after: tuple[Any, ...] | None,
    epoch: int,
    call: Callable[..., Any],
    *,
    high: tuple[Any, ...] | None = None,
    leave_at: int | None = None,
) -> tuple[int, int]:
    """One chunk of the source against the copy: rows written, and rows not read back.

    ``high`` is the chunk's last key where ``wanted`` is only some of the chunk's rows, so that
    the mirror is read as far as the chunk goes. A copy row that carries ``leave_at`` or more is
    left as it is: a settle does not write over what was written since the advance.
    """
    identity = _identity(copy)
    if high is None:
        high = _key(copy, wanted[-1])
    held = {identity(row): row for row in mirror.upto(after, high)}
    repaired, unconfirmed = _repair_against(
        copy, target, held, wanted, epoch, call, leave_at=leave_at
    )
    return len(repaired), unconfirmed


def _repair_against(
    copy: _Copy,
    target: Reconcilable,
    held: dict[Any, dict[str, Any]],
    wanted: list[dict[str, Any]],
    epoch: int,
    call: Callable[..., Any],
    *,
    leave_at: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Rows of the source against the copy's rows beside them, by identity in ``held``: the rows
    written, and how many of them were not read back.

    A source row ``held`` lacks is looked up in the copy by its key before it is called missing.
    """
    identity = _identity(copy)
    several = len(copy.key) > 1
    unseen = [key for key in map(identity, wanted) if key not in held]
    if unseen:
        looked_up = call(
            target.rows_for_keys,
            copy.target_table,
            copy.key,
            unseen if several else [(key,) for key in unseen],
        )
        for row in looked_up:
            held[identity(row)] = row
    missing: list[dict[str, Any]] = []
    differing: list[dict[str, Any]] = []
    for row in wanted:
        there = held.get(identity(row))
        if there is None:
            missing.append({**row, EPOCH_COLUMN: epoch})
        elif leave_at is not None and there[EPOCH_COLUMN] >= leave_at:
            continue
        elif not _same_row(row, there):
            differing.append({**row, EPOCH_COLUMN: epoch})
    # A row that is not there is copied, which is the engine's fastest write; only a row
    # that is there with another value has to replace one.
    if missing:
        call(copy.target.copy_in, copy.target_table, missing)
    if differing:
        call(target.upsert_in, copy.target_table, differing, key=copy.key)
    repaired = missing + differing
    # Read back, because the second pass finds a repaired row by the generation it carries: a
    # write that was answered and not kept would leave the old row under its old generation,
    # where nothing would look at it again.
    return repaired, _not_read_back(copy, target, repaired, epoch, call)


def _not_read_back(
    copy: _Copy,
    target: Reconcilable,
    written: list[dict[str, Any]],
    epoch: int,
    call: Callable[..., Any],
) -> int:
    """How many of the rows just written the copy does not hold under ``epoch`` when read back."""
    if not written:
        return 0
    identity = _identity(copy)
    back = {
        identity(row): row
        for row in call(
            target.rows_for_keys,
            copy.target_table,
            copy.key,
            [_key(copy, row) for row in written],
        )
    }
    unconfirmed = 0
    for row in written:
        kept = back.get(identity(row))
        # There, under the generation: that is all the second pass needs to find it. What it
        # holds is compared then, under the barriers, with every other.
        if kept is None or kept.get(EPOCH_COLUMN) != epoch:
            unconfirmed += 1
    return unconfirmed


# --- settled before the pause ---------------------------------------------------------------------
#
# After a generation advance every write of the group carries the map's generation, G, and the
# engine admits no other. So a row that carries less was written before the advance and has not
# been written since, on the source and on the copy alike. :func:`settle` compares those rows
# while the application keeps writing, and :func:`reconcile_since` is then the cutover's first
# pass over the rest: the rows that carry G.


def settles_before_a_pause(view: MigrationView, group: str) -> bool:
    """Whether both engines of every copy of the group can be read by generation and by key, and
    the copy written over: what a settle pass and the pause after it need."""
    if not repairs_what_differs(view, group):
        return False
    return all(
        callable(getattr(copy.source, name, None))
        for copy in _plan(view, group)
        for name in ("stamped_range", "earlier_range", "rows_for_keys")
    )


@dataclass(frozen=True)
class Settlement:
    """What a settle pass did, in numbers: nothing here names a key or holds a value."""

    chunks: int
    rows: int
    """Rows of the source the pass read: every row where the source streams, and those below the
    generation where it is read by pages."""
    earlier: int
    """Of them, the rows that carry a generation below the map's: the ones it compared."""
    written: int
    unconfirmed: int = 0
    """Rows written and then not read back under the map's generation."""

    @property
    def settled(self) -> bool:
        return self.unconfirmed == 0

    def as_record(self) -> dict[str, Any]:
        return {
            "chunks": self.chunks,
            "rows": self.rows,
            "earlier": self.earlier,
            "written": self.written,
            "unconfirmed": self.unconfirmed,
        }


def _to_the_end(
    copy: _Copy, chunk_rows: int, epoch: int, call: Callable[..., Any]
) -> Generator[list[dict[str, Any]], None, None]:
    """The source's rows in key order, to the last row below ``epoch``.

    The pass that reads this compares rows nothing will compare again, so "it read them all" has
    to be a fact, and the source is being written to meanwhile: no count can say it, since an
    insert hides a row that was not read. The rows that matter are those below the generation,
    and no new row can be one of them, so they are a set that only stays or shrinks.

    - Where the source streams, one statement reads the table as it was when it began - every
      row, those since the advance too - and then the rows below the generation after its last
      key are read by statements of their own, until one answers none: a stream that ended before
      the table did is carried on, and an answer that ended early without an error is not taken
      for the end.
    - Otherwise every statement asks for the rows below the generation only, page after page,
      until one answers none. A page of every row would end on a short page, and an application
      that appends faster than pages are read would never leave one.

    Either way this ends however fast the application writes.
    """
    after: tuple[Any, ...] | None = None
    if callable(getattr(copy.source, "key_stream", None)) and copy.source is not copy.target:
        chunks = source_chunks(copy, chunk_rows, call=call)
        try:
            for chunk in chunks:
                yield chunk
                after = _key(copy, chunk[-1])
        finally:
            chunks.close()
    source = cast(Reconcilable, copy.source)
    while True:
        page: list[dict[str, Any]] = call(
            source.earlier_range,
            copy.source_table, copy.key, epoch=epoch, after=after, limit=chunk_rows,
        )  # fmt: skip
        if not page:
            return
        yield page
        after = _key(copy, page[-1])


def settle(
    view: MigrationView,
    group: str,
    *,
    epoch: int,
    chunk_rows: int = CHUNK_ROWS,
    call: Callable[..., Any] = _direct,
) -> Settlement:
    """Compare every row written before the group's last advance with the copy's, and write over
    what differs, while the application keeps writing.

    ``epoch`` is the generation of the map in force. A source row that carries it was written
    since the advance and is left to the pause. One that carries less is compared: a row the copy
    lacks, or holds differently under an earlier generation, is written to the copy under
    ``epoch`` - as a fan-out would write it - and read back. A copy row that carries ``epoch``
    already is newer than what this pass read of the source, and is left to the pause too.

    A write of the application's that crosses this pass leaves both rows carrying ``epoch``, and
    the pause compares every row that does, on both sides: no interleaving has to be excluded
    here, only made visible there.
    """
    if chunk_rows < 1:
        raise MigrationRefused(f"a chunk of {chunk_rows} rows is not a chunk")
    chunks = rows = earlier = written = unconfirmed = 0
    for copy in _plan(view, group):
        target = cast(Reconcilable, copy.target)
        after: tuple[Any, ...] | None = None
        mirror = _Mirror(copy, chunk_rows, call)
        try:
            for wanted in _to_the_end(copy, chunk_rows, epoch, call):
                before = [row for row in wanted if row[EPOCH_COLUMN] < epoch]
                done = _repair_chunk(
                    copy, target, mirror, before, after, epoch, call,
                    high=_key(copy, wanted[-1]), leave_at=epoch,
                )  # fmt: skip
                written += done[0]
                unconfirmed += done[1]
                chunks += 1
                rows += len(wanted)
                earlier += len(before)
                after = _key(copy, wanted[-1])
        finally:
            mirror.close()
    return Settlement(
        chunks=chunks, rows=rows, earlier=earlier, written=written, unconfirmed=unconfirmed
    )


def reconcile_since(
    view: MigrationView,
    group: str,
    *,
    since: int,
    epoch: int,
    chunk_rows: int = CHUNK_ROWS,
    call: Callable[..., Any] = _direct,
) -> Reconciliation:
    """A cutover's first pass after a settle: the rows written since the advance, and no others.

    The caller holds the source frozen at ``since`` and the copy at ``epoch``, and holds a settle
    of the group made at ``since`` under the map that is still in force. A row the application
    wrote since the advance carries ``since`` on both sides - the source's from the application,
    the copy's from the fan-out - so the two sides' rows that carry it are read beside each other,
    each by pages in its engine's key order, and a row is looked up by its key only where the
    other side did not show it:

    - each page of the source's rows that carry ``since`` is compared with the copy's rows that
      carry it, up to the page's last key (:class:`_StampedMirror`). A source row the copy did not
      show there is looked up by its key; one the copy lacks or holds differently is written over
      it under ``epoch`` and read back;
    - a copy row that carries ``since`` and that no source row beside it asked for is an orphan:
      one a settle or a backfill after the advance wrote, whose source row is older; a key the
      source lacks; or a row the two orders put beside another page. Orphans are looked up in the
      source by key, a page of them at a time: one that differs is written over, and a key the
      source lacks is ``surplus``.

    Every copy row that carries ``since`` is compared once this way, and the comparison is the
    same in both places. The pause holds both sides still, so the only rows that leave the copy's
    pages meanwhile are those this pass writes, and it compared each first. An orphan whose row
    this pass wrote is not compared again: what the mirror handed out is what the copy held before.
    Rows under an earlier generation are the settle's, and are not read here.
    """
    if chunk_rows < 1:
        raise MigrationRefused(f"a chunk of {chunk_rows} rows is not a chunk")
    chunks = rows = written = unconfirmed = 0
    surplus = False
    for copy in _plan(view, group):
        source = cast(Reconcilable, copy.source)
        walk = _SinceWalk(copy, since=since, epoch=epoch, chunk_rows=chunk_rows, call=call)
        after: tuple[Any, ...] | None = None
        while True:
            wanted = call(
                source.stamped_range,
                copy.source_table, copy.key, epoch=since, after=after, limit=chunk_rows,
            )  # fmt: skip
            if not wanted:
                break
            walk.chunk(wanted)
            chunks += 1
            rows += len(wanted)
            after = _key(copy, wanted[-1])
        walk.finish()
        # The copy's pages are the reads the pass made of its side, as the source's are of the
        # other: the same statements a walk of the copy alone would send, sent beside the source's.
        chunks += walk.mirror.pages
        rows += walk.mirror.rows
        written += walk.written
        unconfirmed += walk.unconfirmed
        surplus = surplus or walk.surplus
    return Reconciliation(
        chunks=chunks, rows=rows, written=written, surplus=surplus, unconfirmed=unconfirmed
    )


class _StampedMirror:
    """The copy's rows that carry one generation, read forward beside the source's.

    By pages of :meth:`Reconcilable.stamped_range`, each after the last key of the one before, so
    the pages split the rows in the copy's engine's own order whatever this process's is, and
    handed out up to a chunk's last key in this process's order. Every row it reads it hands out
    once. Neither order has to agree with the other: a row handed out beside a chunk that does not
    ask for it is the caller's to compare by its key, and one of the chunk that is not handed out
    is looked up by its key, as :class:`_Mirror`'s are.
    """

    def __init__(self, copy: _Copy, since: int, chunk_rows: int, call: Callable[..., Any]) -> None:
        self._copy, self._since, self._chunk_rows, self._call = copy, since, chunk_rows, call
        self._identity = _identity(copy)
        self._read: list[dict[str, Any]] = []
        self._next = 0
        self._after: tuple[Any, ...] | None = None
        self._ended = False
        self.pages = 0
        """Pages read that held a row."""
        self.rows = 0

    def _page(self) -> bool:
        """The next page into the buffer; ``False`` once a statement answers none. A short page is
        not taken for the end: an answer that ended early without an error would pass for it."""
        if self._ended:
            return False
        copy = self._copy
        page: list[dict[str, Any]] = self._call(
            cast(Reconcilable, copy.target).stamped_range,
            copy.target_table, copy.key,
            epoch=self._since, after=self._after, limit=self._chunk_rows,
        )  # fmt: skip
        if not page:
            self._ended = True
            return False
        self.pages += 1
        self.rows += len(page)
        self._read, self._next = page, 0
        self._after = _key(copy, page[-1])
        return True

    def upto(self, high: tuple[Any, ...]) -> Iterator[dict[str, Any]]:
        """The rows up to ``high``: to the first one after it, in this process's order."""
        last = self._identity(dict(zip(self._copy.key, high, strict=True)))
        while True:
            while self._next < len(self._read):
                row = self._read[self._next]
                try:
                    if self._identity(row) > last:
                        return
                except TypeError:
                    pass  # Keys this process cannot order: handed out, and found by key if not.
                self._next += 1
                yield row
            if not self._page():
                return

    def rest(self) -> Iterator[dict[str, Any]]:
        """Every row not handed out yet."""
        while True:
            while self._next < len(self._read):
                row = self._read[self._next]
                self._next += 1
                yield row
            if not self._page():
                return


class _SinceWalk:
    """One copy's part of :func:`reconcile_since`: the copy's rows beside each chunk of the
    source's, and the orphans, and what both came to.

    Holds a chunk of the source's, a page of the copy's, the copy's rows the chunk asked for and
    fewer than a chunk of orphans: four chunks at most, however many rows the pause reads. And the
    identity of every row it wrote, to leave out an orphan that is that row as it was before.
    """

    def __init__(
        self, copy: _Copy, *, since: int, epoch: int, chunk_rows: int, call: Callable[..., Any]
    ) -> None:
        self._copy, self._epoch, self._chunk_rows, self._call = copy, epoch, chunk_rows, call
        self._source = cast(Reconcilable, copy.source)
        self._target = cast(Reconcilable, copy.target)
        self._identity = _identity(copy)
        self.mirror = _StampedMirror(copy, since, chunk_rows, call)
        self.written = self.unconfirmed = 0
        self.surplus = False
        self._wrote: set[Any] = set()
        self._orphans: list[dict[str, Any]] = []

    def chunk(self, wanted: list[dict[str, Any]]) -> None:
        identity = self._identity
        asked = {identity(row) for row in wanted}
        held: dict[Any, dict[str, Any]] = {}
        for there in self.mirror.upto(_key(self._copy, wanted[-1])):
            key = identity(there)
            if key in asked:
                held[key] = there
            else:
                self._orphan(there)
        repaired, unconfirmed = _repair_against(
            self._copy, self._target, held, wanted, self._epoch, self._call
        )
        self.written += len(repaired)
        self.unconfirmed += unconfirmed
        self._wrote.update(map(identity, repaired))

    def finish(self) -> None:
        """The copy's rows after the source's last key, and the orphans still held."""
        for there in self.mirror.rest():
            self._orphan(there)
        self._compare_orphans()

    def _orphan(self, row: dict[str, Any]) -> None:
        self._orphans.append(row)
        if len(self._orphans) >= self._chunk_rows:
            self._compare_orphans()

    def _compare_orphans(self) -> None:
        copy, identity, epoch = self._copy, self._identity, self._epoch
        # A row this pass wrote is the source's under the operation's generation, read back; the
        # orphan is what the copy held before, read ahead of the write.
        orphans = [row for row in self._orphans if identity(row) not in self._wrote]
        self._orphans = []
        if not orphans:
            return
        originals = {
            identity(row): row
            for row in self._call(
                self._source.rows_for_keys,
                copy.source_table,
                copy.key,
                [_key(copy, row) for row in orphans],
            )
        }
        differing: list[dict[str, Any]] = []
        for there in orphans:
            original = originals.get(identity(there))
            if original is None:
                self.surplus = True
            elif not _same_row(original, there):
                differing.append({**original, EPOCH_COLUMN: epoch})
        if differing:
            self._call(self._target.upsert_in, copy.target_table, differing, key=copy.key)
            self.unconfirmed += _not_read_back(copy, self._target, differing, epoch, self._call)
            self.written += len(differing)


def compare_stamped(
    view: MigrationView,
    group: str,
    *,
    epoch: int,
    reconciled: Reconciliation,
    chunk_rows: int = CHUNK_ROWS,
    request: VerificationRequest | None = None,
    at: str | None = None,
) -> VerifyReport:
    """Compare every row of the copy that carries ``epoch`` with the source's, and count both.

    The comparison a frozen verification runs after :func:`reconcile`, under its barriers, in
    place of the full one. Its report has the full one's shape and meaning: ``chunks_compared`` is
    the chunks both passes compared, ``chunks_mismatched`` the chunks of this pass with a row that
    is not the source's, and the two counts are taken under the barriers. A row of the copy with
    no row in the source is a difference in every column.
    """
    if request is not None:
        request.check_session(view.placement, project_id=view.project_id, group=group)
        if view.model.version != request.model_version:
            raise MigrationRefused("verification request names another session model")
        if at is not None:
            request.check_time(at)
    if chunk_rows < 1:
        raise MigrationRefused(f"a chunk of {chunk_rows} rows is not a chunk")
    chunks_compared = reconciled.chunks
    chunks_mismatched = 0
    rows_source = rows_target = 0
    differences: list[Difference] = []
    suppressed = 0
    for copy in _plan(view, group):
        source = cast(Reconcilable, copy.source)
        target = cast(Reconcilable, copy.target)
        rows_source += copy.source.count(copy.source_table)
        rows_target += copy.target.count(copy.target_table)
        after: tuple[Any, ...] | None = None
        while True:
            stamped = target.stamped_range(
                copy.target_table, copy.key, epoch=epoch, after=after, limit=chunk_rows
            )
            if not stamped:
                break
            keys = [_key(copy, row) for row in stamped]
            identity = _identity(copy)
            originals = {
                identity(row): row
                for row in source.rows_for_keys(copy.source_table, copy.key, keys)
            }
            wrong = 0
            for physical in stamped:
                original = originals.get(identity(physical))
                if original is not None and _same_row(original, physical):
                    continue
                row = _logical_copy_row(copy, physical)
                columns = (
                    tuple(sorted(row))
                    if original is None
                    else _differing_columns(_logical_copy_row(copy, original), row)
                )
                wrong += 1
                if len(differences) < _DIFFERENCES_KEPT:
                    differences.append(
                        Difference(
                            entity=copy.entity,
                            table=copy.target_table,
                            key={column: row[column] for column in copy.key},
                            columns=columns,
                        )
                    )
                else:
                    suppressed += 1
            chunks_compared += 1
            chunks_mismatched += 1 if wrong else 0
            after = keys[-1]
    return VerifyReport(
        at=_stamp(at, request),
        request=request,
        group=group,
        chunks_compared=chunks_compared,
        chunks_mismatched=chunks_mismatched,
        tail_rows_read=0,
        tail_rows_missing_in_target=0,
        rows_source=rows_source,
        rows_target=rows_target,
        differences=tuple(differences),
        differences_suppressed=suppressed,
    )

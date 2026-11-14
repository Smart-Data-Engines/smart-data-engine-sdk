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

from collections.abc import Callable, Generator, Mapping, Sequence
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
)
from .verification import VerificationRequest

__all__ = [
    "Reconcilable",
    "Reconciliation",
    "compare_stamped",
    "copy_is_mostly_there",
    "reconcile",
    "repairs_what_differs",
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


def _source_chunks(
    copy: _Copy, chunk_rows: int, call: Callable[..., Any]
) -> Generator[list[dict[str, Any]], None, None]:
    """The source's rows in key order, a chunk at a time.

    By one streamed statement where the source's engine offers it and is not also the copy's (a
    stream holds its engine until it ends, and a relayout reads and writes one engine); otherwise
    by a keyset read a chunk. ``call`` wraps each fetch, which is where a cutover checks its budget.
    """
    stream = getattr(copy.source, "key_stream", None)
    if callable(stream) and copy.source is not copy.target:
        chunks = stream(copy.source_table, copy.key, chunk_rows=chunk_rows)
        try:
            while True:
                chunk = call(next, chunks, None)
                if not chunk:
                    return
                yield chunk
        finally:
            chunks.close()
    after: tuple[Any, ...] | None = None
    while True:
        rows = call(
            copy.source.key_range, copy.source_table, copy.key, after=after, limit=chunk_rows
        )
        if not rows:
            return
        yield rows
        after = _key(copy, rows[-1])


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
        source_chunks = _source_chunks(copy, chunk_rows, call)
        try:
            for wanted in source_chunks:
                done = _repair_chunk(copy, target, mirror, wanted, after, epoch, call)
                written += done[0]
                unconfirmed += done[1]
                chunks += 1
                walked += len(wanted)
                after = _key(copy, wanted[-1])
        finally:
            source_chunks.close()
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
) -> tuple[int, int]:
    """One chunk of the source against the copy: rows written, and rows not read back."""
    identity = _identity(copy)
    several = len(copy.key) > 1
    unconfirmed = 0
    high = _key(copy, wanted[-1])
    held = {identity(row): row for row in mirror.upto(after, high)}
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
        elif not _same_row(row, there):
            differing.append({**row, EPOCH_COLUMN: epoch})
    # A row that is not there is copied, which is the engine's fastest write; only a row
    # that is there with another value has to replace one.
    if missing:
        call(copy.target.copy_in, copy.target_table, missing)
    if differing:
        call(target.upsert_in, copy.target_table, differing, key=copy.key)
    repaired = missing + differing
    if repaired:
        # Read back, because the second pass finds a repaired row by the generation it
        # carries: a write that was answered and not kept would leave the old row under
        # its old generation, where nothing would look at it again.
        back = {
            identity(row): row
            for row in call(
                target.rows_for_keys,
                copy.target_table,
                copy.key,
                [_key(copy, row) for row in repaired],
            )
        }
        for row in repaired:
            kept = back.get(identity(row))
            # There, under the generation: that is all the second pass needs to find it.
            # What it holds is compared then, under the barriers, with every other.
            if kept is None or kept.get(EPOCH_COLUMN) != epoch:
                unconfirmed += 1
    return len(repaired), unconfirmed


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

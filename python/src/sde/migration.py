"""Copying a group into its second engine, and proving the copy is complete.

The two halves of a migration that touch data, and therefore the two halves that cannot be ours.
Copying a row means reading a client's row; comparing two copies means holding both engines open at
once. We have credentials to neither and never will, which is why this file is in the public
library and the state machine that gates on it is not.

So the division is: **this module produces numbers, and the control plane decides.**
:class:`VerifyReport` is designed around that boundary - ``as_record()`` carries seven counts and
nothing else, while the detail an operator needs to *fix* a mismatch stays here, on their machine,
in a field the record does not have.

Three decisions in the backfill are worth reading before the code, because each one is the reason a
simpler version would lose rows.

**The marker is a row count, not a key.** The obvious marker is "the last key copied", which
resumes exactly. It also needs a codec: a key value has to survive a round trip through whatever
column the marker table has, for every type a key can be, in every language that will later grow an
adapter. A codec whose failure mode is a resume point *past* rows that were never copied is a codec
whose failure mode is silent data loss. A row count cannot fail that way, and the reason is worth
stating precisely because the loose version of it is false.

Resuming means one ``OFFSET`` query - "give me the key of row N". If rows have been inserted below
that point since, row N is now an *earlier* row, so the resume key moves down and the backfill
recopies. It does **not** follow that no row is ever stepped over: rows inserted below the new
resume key are stepped over, and the loose claim that "every error points at recopying" is wrong
about them. What holds is the statement that matters. Call the rows that existed when this backfill
began S. Marker N was written when the first N rows in key order were copied, so every row of S at
or below that boundary key B is copied. The new resume key A is at or below B, because inserting
rows can only push a given row later in the order - so the rows of S at or below A are a subset of
those at or below B, and therefore copied. **No row of S is ever skipped**, which is the entire job
of the backfill. The rows that are stepped over are rows that arrived after it began, and those
belong to the fan-out - the same premise the absence of a ceiling rests on, below.

If the fan-out failed for one of them, it is missing from the copy and it now sits *below* the final
marker, so :func:`verify` counts it against the chunks rather than against the tail. The attribution
is off by one mechanism in that case and the row is still caught, which is the right way round.

**The chunk is written before the marker moves, and the copy is idempotent.** A crash between the
two leaves a chunk that will be copied again, which is why the target's key semantics have to
absorb a duplicate: ``ON CONFLICT DO NOTHING`` in PostgreSQL, ``ReplacingMergeTree`` collapsing
under ``FINAL`` in ClickHouse. Order the two writes the other way round and the crash window loses
a chunk permanently. These two decisions hold each other up: the ordering is only safe because the
copy is idempotent, and the copy only needs to be idempotent because of the ordering.

**There is no ceiling, and the backfill does not chase its own tail.** The source keeps growing
while the copy runs - that is what a live migration is - so a naive "copy until the source stops
growing" never terminates. It does not need to. ``DUAL_WRITE`` precedes ``BACKFILL``, so every row
written from then on reaches the copy through the fan-out; the backfill's job is only the rows that
were there before it started. Reaching the end of the table **once** is therefore enough, and a
chunk that comes back short is what says so. Every row in the source is then in one of two regions:
below the final marker and copied here, or written after dual-write began and copied by
:meth:`sde.Session.save`.

That argument has a visible failure mode, and it is the one the gate exists for. If dual-write was
not actually running everywhere - a deployment half-rolled-out, one process still on the previous
map - then rows written by the stragglers land above the marker and nothing copies them.
:func:`verify` reads the tail above the marker and finds them missing, and the migration stops with
reads still on the source. The design defends itself rather than trusting the operator to have
sequenced the phases correctly.

**On what ``verify`` compares, which is a correction to an earlier design of it.** That design said
chunks below the marker are immutable and so compare exactly, while the live tail above it is
checked for containment. The premise is false: "below the marker" is a position in *key* order, and
a key is not required to increase with insertion time, so a row written during the migration can
land anywhere - including below the marker, where it is perfectly mutable. What the two regions
actually distinguish is **which mechanism failed**: a source row missing below the marker means the
backfill did not copy it, and one missing above means the fan-out did not. Different cause,
different fix, and worth two counters. The rule applied is the same in both, and it is containment
with equality of content: every row the source has, the target has, byte for byte in every column.
Extra rows in the target are not gated on, for the reason the row counts are not - the two reads
happen at different instants and a write between them is not a defect.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from .capabilities import satisfies
from .errors import EngineError, MigrationRefused
from .groups import Group, colocation_groups
from .layout import group_columns
from .logging import log
from .placement import BACKFILL_TABLE, Materialization

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .session import Session

__all__ = [
    "BACKFILL_TABLE",
    "CHUNK_ROWS",
    "DIALECT_PRECISION",
    "PRECISION_INDEPENDENT",
    "BackfillProgress",
    "Difference",
    "EntityProgress",
    "Migratable",
    "VerifyReport",
    "backfill",
    "verify",
]

CHUNK_ROWS = 1000
"""Rows per chunk, by default.

A thousand rather than a round ten thousand: a chunk is held in memory twice during
:func:`verify` - the source's rows and the target's - and the number that matters is not throughput
but how much work a crash discards, which is one chunk.
"""

DIALECT_PRECISION: Mapping[tuple[str, str], int] = {
    ("timestamp", "postgres"): 6,
    ("timestamptz", "postgres"): 6,
    ("timestamp", "clickhouse"): 3,
    ("timestamptz", "clickhouse"): 3,
}
"""Sub-second digits each dialect keeps, for the neutral types where dialects differ.

PostgreSQL's ``timestamptz`` is microsecond-resolution and ClickHouse's ``DateTime64(3)`` is
millisecond-resolution, which is a *stated* choice in :mod:`sde.layout` and a fine one for storage.
It is not fine for a copy: ``datetime.now()`` has microseconds, so the truncation affects
essentially every row, and it is silent - the insert succeeds and the value comes back changed. A
migration in that direction is refused before it copies anything rather than after, because
:func:`verify` would otherwise find every row mismatched at the end of a copy that took hours.
"""

PRECISION_INDEPENDENT: frozenset[str] = frozenset(
    {
        "bool",
        "int32",
        "int64",
        "float32",
        "float64",
        "string",
        "bytes",
        "uuid",
        "date",
        "json",
    }
)
"""Neutral types a copy between dialects does not silently change.

Not the same claim as "every value survives". PostgreSQL's ``date`` has a wider range than
ClickHouse's ``Date32``, so a date in the year 1800 does not survive that move - but it fails
*loudly*, on the insert or as a mismatch in :func:`verify`, and no ordinary business date is
anywhere near the boundary. The line drawn here is silent-and-universal loss, which is a much
smaller set than lossy, and it is the set worth a refusal that arrives before the work.

A neutral type in neither this set nor :data:`DIALECT_PRECISION` refuses the migration, so adding
one to the vocabulary forces a decision here instead of inheriting an answer nobody made.
"""


@runtime_checkable
class Migratable(Protocol):
    """What an engine adapter needs to offer for a group to be migrated into or out of it.

    A separate optional protocol, exactly like :class:`sde.watermark.WatermarkStore` and for the
    same reason: putting these on :class:`sde.session.Engine` would break every adapter anybody has
    written against it, including fakes in someone else's test suite, for a capability our own
    orderbook engine cannot provide. Its schema is fixed in its own source, so it has nowhere to
    keep a marker, and its write path is an update of N price levels rather than a row - neither of
    which can be papered over. Non-participation is therefore a **named refusal** rather than a
    silent skip, because a migration that quietly copies nothing is the worst outcome available
    here.
    """

    dialect: str

    def key_range(
        self,
        table: str,
        order: Sequence[str],
        *,
        after: Sequence[Any] | None = None,
        upto: Sequence[Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]: ...

    def nth_key(
        self, table: str, order: Sequence[str], *, position: int
    ) -> tuple[Any, ...] | None: ...

    def copy_in(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None: ...

    def count(self, table: str) -> int: ...

    def get(self, table: str, key: Mapping[str, Any]) -> dict[str, Any] | None: ...

    def backfill_marker(self, *, materialization: str, entity: str) -> int: ...

    def record_backfill_marker(
        self, *, materialization: str, entity: str, rows: int
    ) -> None: ...


def key_columns(order: Sequence[str], table: str) -> tuple[str, ...]:
    """The ordering columns for a keyset scan, refusing an empty one.

    Here rather than in each adapter so that the two cannot disagree about it, and public because
    an adapter written outside this repository has the same argument to check. An empty order is
    not a scan of everything in an unspecified order - it is a paginated scan with no pagination,
    which returns the same first page forever.
    """
    cols = tuple(str(c) for c in order)
    if not cols:
        raise EngineError(
            f"a keyset scan of {table} needs at least one ordering column. With none, every page "
            f"is the first page and a backfill would copy the same chunk until it was stopped."
        )
    return cols


def same_width(bound: Sequence[Any], cols: Sequence[str], name: str) -> None:
    """A bound has one value per ordering column, or the comparison is not the one intended."""
    if len(bound) != len(cols):
        raise EngineError(
            f"{name} has {len(bound)} values and the order has {len(cols)} columns {list(cols)}. A "
            f"row-value comparison of different widths is not a narrower comparison, it is a "
            f"different one."
        )


@dataclass(frozen=True)
class _Copy:
    """One entity, from one materialisation to one fan-out target. The unit both passes work in."""

    entity: str
    key: tuple[str, ...]
    source: Migratable
    source_engine: str
    source_table: str
    target: Migratable
    target_engine: str
    target_id: str
    target_table: str


@dataclass(frozen=True)
class EntityProgress:
    """How far one entity's copy into one target has got."""

    entity: str
    engine: str
    table: str
    rows_copied: int
    """The marker: rows of this entity copied into this target, across every run."""
    rows_this_run: int
    chunks: int
    complete: bool
    """Whether the last chunk came back short, which is what "the tail is the fan-out's now" means.
    """

    def as_record(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "engine": self.engine,
            "table": self.table,
            "rows_copied": self.rows_copied,
            "rows_this_run": self.rows_this_run,
            "chunks": self.chunks,
            "complete": self.complete,
        }


@dataclass(frozen=True)
class BackfillProgress:
    """What one call to :func:`backfill` did, per entity and per target."""

    group: str
    entities: tuple[EntityProgress, ...]

    @property
    def complete(self) -> bool:
        """Every entity of every target has reached the end of its table at least once."""
        return all(entity.complete for entity in self.entities)

    @property
    def rows_this_run(self) -> int:
        return sum(entity.rows_this_run for entity in self.entities)

    def as_record(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "complete": self.complete,
            "rows_this_run": self.rows_this_run,
            "entities": [entity.as_record() for entity in self.entities],
        }

    def for_a_human(self) -> str:
        lines = [
            f"backfill of {self.group}: "
            f"{'complete' if self.complete else 'more to do'}, "
            f"{self.rows_this_run} rows this run"
        ]
        for entity in self.entities:
            lines.append(
                f"  {entity.entity} -> {entity.engine}.{entity.table}: "
                f"{entity.rows_copied} rows copied "
                f"({entity.rows_this_run} this run, {entity.chunks} chunks)"
                f"{'' if entity.complete else ', more to do'}"
            )
        return "\n".join(lines)


@dataclass(frozen=True)
class Difference:
    """One source row the target does not have, or has differently.

    **This holds the client's own data and it is the reason ``as_record()`` does not.** The key is
    here because "which row" is the first thing an operator needs and a count cannot say it; it
    stays on their machine because a row is the one thing that must never travel to us. The two
    facts are the same decision seen from two sides.
    """

    entity: str
    table: str
    key: Mapping[str, Any]
    columns: tuple[str, ...]
    """Columns whose values differ, or empty when the row is absent from the target altogether."""

    @property
    def absent(self) -> bool:
        return not self.columns

    def for_a_human(self) -> str:
        where = "absent" if self.absent else f"differs in {list(self.columns)}"
        return f"{self.entity} {dict(self.key)} -> {where}"


_DIFFERENCES_KEPT = 20

_ABSENT = object()
"""Sentinel for "the target row has no such column", so that a stored ``None`` is not that."""


@dataclass(frozen=True)
class VerifyReport:
    """What the comparison found, in the shape the gate needs and nothing wider.

    ``as_record()`` is the boundary. It carries seven counts, and the control plane's
    ``VerifyResult`` reads exactly those - so there is no field in which a value of the client's
    could travel, and adding one would be a visible change to this method rather than an accident
    somewhere in a call chain. :attr:`differences` is the other half of that: the detail that makes
    a mismatch fixable, kept here and deliberately absent from the record.
    """

    at: str
    group: str
    chunks_compared: int
    chunks_mismatched: int
    tail_rows_read: int
    tail_rows_missing_in_target: int
    rows_source: int
    rows_target: int
    differences: tuple[Difference, ...] = ()
    differences_suppressed: int = 0

    @property
    def matched(self) -> bool:
        """Whether the target holds everything the source holds. Zero tolerance, both terms.

        Zero rather than a threshold, and the reason is arithmetic rather than principled: any
        non-zero threshold is an answer to "how many of your rows may we lose", and there is no
        number to say out loud there.
        """
        return self.chunks_mismatched == 0 and self.tail_rows_missing_in_target == 0

    def as_record(self) -> dict[str, Any]:
        """The seven counts the gate reads. **Numbers, never rows** - see the class docstring."""
        return {
            "at": self.at,
            "chunks_compared": self.chunks_compared,
            "chunks_mismatched": self.chunks_mismatched,
            "tail_rows_read": self.tail_rows_read,
            "tail_rows_missing_in_target": self.tail_rows_missing_in_target,
            "rows_source": self.rows_source,
            "rows_target": self.rows_target,
        }

    def for_a_human(self) -> str:
        lines = [
            f"verify of {self.group} at {self.at}: "
            f"{'matched' if self.matched else 'DID NOT MATCH'}",
            f"  below the marker: {self.chunks_compared} chunks compared, "
            f"{self.chunks_mismatched} mismatched"
            + ("" if self.chunks_mismatched == 0 else "  <- the backfill did not copy these"),
            f"  above the marker: {self.tail_rows_read} rows read, "
            f"{self.tail_rows_missing_in_target} missing in the copy"
            + (
                ""
                if self.tail_rows_missing_in_target == 0
                else "  <- the dual-write fan-out did not reach these"
            ),
            f"  rows: {self.rows_source} in the source, {self.rows_target} in the copy "
            f"(reported, not gated on: two counts of live tables are taken at different instants)",
        ]
        if self.differences:
            lines.append(
                "  the rows below are your own data. They are not part of what is reported to "
                "Smart Data Engines:"
            )
            lines.extend(f"    {d.for_a_human()}" for d in self.differences)
            if self.differences_suppressed:
                lines.append(f"    ... and {self.differences_suppressed} more")
        return "\n".join(lines)


def _group(session: Session, group: str) -> Group:
    for candidate in colocation_groups(session.model):
        if candidate.name == group:
            return candidate
    raise MigrationRefused(
        f"{group!r} is not a colocation group of this model. It has "
        f"{sorted(g.name for g in colocation_groups(session.model))}."
    )


def _migratable(session: Session, engine_name: str, role: str, group: str) -> Migratable:
    engine = session.engines[engine_name]
    # `satisfies` rather than `isinstance`: a runtime_checkable protocol ignores `__getattr__`, so
    # a client's wrapper around one of our adapters would be refused here for a property of their
    # wrapper. See `sde.capabilities`.
    if not satisfies(engine, Migratable):
        raise MigrationRefused(
            f"{engine_name!r} cannot act as the {role} of a migration of {group!r}: its adapter "
            f"does not offer the row-level operations a copy needs. An engine whose schema is "
            f"fixed in its own source has nowhere to keep a progress marker and no table to scan "
            f"in key order, so this is a property of the engine rather than a missing feature. "
            f"Refused here rather than skipped, because a migration that copies nothing and says "
            f"nothing is the worst thing this module could do."
        )
    # `satisfies` cannot be a `TypeGuard`, because the protocol it checks against is a runtime
    # argument. The narrowing is asserted here and the line above is what makes it true.
    return cast("Migratable", engine)


def _check_precision(
    *,
    group: str,
    entity: str,
    columns: Mapping[str, str],
    source: Migratable,
    target: Migratable,
) -> None:
    """Refuse a copy whose target column cannot hold what the source column can.

    Before anything is copied, which is the whole point. The alternative is finding out from
    :func:`verify` at the end of a copy that took hours, and the answer would be the same.
    """
    for column, neutral in sorted(columns.items()):
        if neutral.startswith("decimal(") or neutral in PRECISION_INDEPENDENT:
            continue
        here = DIALECT_PRECISION.get((neutral, source.dialect))
        there = DIALECT_PRECISION.get((neutral, target.dialect))
        if here is None or there is None:
            raise MigrationRefused(
                f"{group}.{entity}.{column} has neutral type {neutral!r}, and this library does "
                f"not know whether {source.dialect} and {target.dialect} store it to the same "
                f"precision. Refused rather than attempted: a type nobody classified is a type "
                f"nobody checked, and the failure mode of guessing here is a value that comes back "
                f"changed with no error anywhere."
            )
        if there < here:
            raise MigrationRefused(
                f"{group}.{entity}.{column} is {neutral!r}, which {source.dialect} stores to "
                f"{here} sub-second digits and {target.dialect} to {there}. Copying it would "
                f"truncate every value with more precision than that - silently, because the "
                f"insert succeeds and the value comes back changed - and `verify` would then find "
                f"every such row mismatched at the end of the copy rather than before it. Your "
                f"rows may all happen to be aligned to {there} digits, in which case this refusal "
                f"costs you a migration that would have worked; we cannot tell without reading "
                f"your data, and a copy that is faithful only for the values that happen to be "
                f"present is not something to build a gate on."
            )


def _plan(session: Session, group: str) -> tuple[_Copy, ...]:
    """Every refusal, before a single row moves.

    A migration is the operation with the least tolerance for a late discovery in this whole
    library: the cost of finding a problem at chunk four thousand is four thousand chunks of the
    client's I/O and an operator who now has to decide whether what has been copied is safe to
    leave. So the shapes, the engines and the types are all settled here.
    """
    members = _group(session, group)
    placement = session.placement.placement_of(group)
    if not placement.also_write:
        raise MigrationRefused(
            f"{group!r} has no fan-out target in this map, so there is nothing to backfill. A "
            f"migration reaches this library as a placement map with 'also_write' - there is no "
            f"phase name in the document and no second channel - so a map without that key is one "
            f"that says this group is not being migrated."
        )
    source_engine = _migratable(session, placement.source.engine, "source", group)
    columns = group_columns(session.model, members)

    copies: list[_Copy] = []
    for copy in placement.also_write:
        target_engine = _migratable(session, copy.engine, "target", group)
        for entity in members.members:
            key = tuple(session.model.entity(entity).key)
            if not key:
                raise MigrationRefused(
                    f"{group}.{entity} has no key, so its rows cannot be scanned in a stable order "
                    f"and a chunk boundary would not mean anything."
                )
            _shapes_agree(group, entity, placement.source, copy)
            _check_precision(
                group=group,
                entity=entity,
                columns=columns[entity],
                source=source_engine,
                target=target_engine,
            )
            copies.append(
                _Copy(
                    entity=entity,
                    key=key,
                    source=source_engine,
                    source_engine=placement.source.engine,
                    source_table=placement.source.layout.table_for(entity),
                    target=target_engine,
                    target_engine=copy.engine,
                    target_id=copy.id,
                    target_table=copy.layout.table_for(entity),
                )
            )
    return tuple(copies)


def _shapes_agree(
    group: str, entity: str, source: Materialization, target: Materialization
) -> None:
    """The target's table has the same columns as the source's, or this is not a move.

    A fan-out target is allowed to be any derived materialisation, and a derived materialisation is
    allowed to be a denormalised wide table - which is a useful thing and not a migration target.
    Filling one means reading the group's relations and assembling rows that exist in no single
    table, and this module copies rows. Refused by name, because the alternative is a copy that
    leaves the extra columns null and looks like it worked.
    """
    here = source.layout.columns.get(entity)
    there = target.layout.columns.get(entity)
    for label, cols, mat in (("source", here, source), ("target", there, target)):
        if not cols:
            raise MigrationRefused(
                f"the {label} materialisation {mat.id!r} of {group!r} does not describe the "
                f"columns of {entity}, so a copy cannot be checked for shape before it starts. "
                f"`ensure_schema` needs them too; a layout with tables and no columns is not one "
                f"this library can apply."
            )
    assert here is not None and there is not None  # for mypy; both proved non-empty above
    if set(here) != set(there):
        only_source = sorted(set(here) - set(there))
        only_target = sorted(set(there) - set(here))
        raise MigrationRefused(
            f"{group}.{entity} has different columns in {source.id!r} and {target.id!r} "
            f"(only in the source: {only_source}; only in the target: {only_target}). That is a "
            f"reshape rather than a move: filling a wide table means reading the group's relations "
            f"and assembling rows that exist in no single table, and this module copies rows. A "
            f"copy would leave the extra columns null and look like it had worked."
        )


def backfill(
    session: Session,
    group: str,
    *,
    chunk_rows: int = CHUNK_ROWS,
    stop_after: int | None = None,
) -> BackfillProgress:
    """Copy a group's existing rows into every fan-out target the map names. Resumable.

    Called again after an interruption it picks up from the marker, and called again after
    completion it does nothing - both because the marker is durable and lives in the target engine,
    next to the rows it describes. That is the correct coupling: a target dropped and recreated
    loses its marker with its data, and a marker kept anywhere else would claim work that no longer
    exists.

    ``stop_after`` bounds the work to that many chunks per entity, for an operator who wants to copy
    for a while and stop, and for a test that needs to interrupt at a known point. ``None`` runs
    each entity to the end of its table.
    """
    if chunk_rows < 1:
        raise MigrationRefused(f"a chunk of {chunk_rows} rows is not a chunk")
    progress: list[EntityProgress] = []
    for copy in _plan(session, group):
        progress.append(
            _backfill_one(copy, group=group, chunk_rows=chunk_rows, stop_after=stop_after)
        )
    return BackfillProgress(group=group, entities=tuple(progress))


def _backfill_one(
    copy: _Copy, *, group: str, chunk_rows: int, stop_after: int | None
) -> EntityProgress:
    marker = copy.target.backfill_marker(materialization=copy.target_id, entity=copy.entity)
    after = _resume_point(copy, marker)
    rows_this_run = 0
    chunks = 0
    complete = False
    while stop_after is None or chunks < stop_after:
        rows = copy.source.key_range(
            copy.source_table, copy.key, after=after, limit=chunk_rows
        )
        if not rows:
            complete = True
            break
        # The chunk first, then the marker. A crash between them costs a recopy, which the target's
        # key semantics absorb; the other order costs the chunk, permanently. See the module
        # docstring - this ordering is why the copy has to be idempotent, and the idempotence is
        # why this ordering is free.
        copy.target.copy_in(copy.target_table, rows)
        marker += len(rows)
        copy.target.record_backfill_marker(
            materialization=copy.target_id, entity=copy.entity, rows=marker
        )
        after = tuple(rows[-1][column] for column in copy.key)
        rows_this_run += len(rows)
        chunks += 1
        log(
            "sde.migration.backfill_progress",
            group=group,
            entity=copy.entity,
            engine=copy.target_engine,
            table=copy.target_table,
            chunk=chunks,
            rows=len(rows),
            rows_copied=marker,
        )
        if len(rows) < chunk_rows:
            # The end of the table, once. Rows arriving above this point from here on are the
            # fan-out's, which is why there is no ceiling and no second pass.
            complete = True
            break
    return EntityProgress(
        entity=copy.entity,
        engine=copy.target_engine,
        table=copy.target_table,
        rows_copied=marker,
        rows_this_run=rows_this_run,
        chunks=chunks,
        complete=complete,
    )


def _resume_point(copy: _Copy, marker: int) -> tuple[Any, ...] | None:
    """Turn a row count back into a key, or refuse if the source has lost rows.

    One ``OFFSET`` scan, paid once per resume rather than once per chunk, which is the trade that
    makes a row count an acceptable marker. If rows have been inserted below this point since the
    marker was written, row N is now an earlier row, so this key moves down and the backfill
    recopies. Rows inserted below the new key are stepped over - see the module docstring for why
    that is safe, and for why "nothing is ever stepped over" is the wrong thing to claim.

    A source with fewer rows than the marker claims were copied is the one case that refuses. It
    means rows left the source outside this library, and a backfill cannot resume against a table
    that has shrunk: the marker would be describing a table that no longer exists.
    """
    if marker <= 0:
        return None
    position = copy.source.nth_key(copy.source_table, copy.key, position=marker)
    if position is None:
        raise MigrationRefused(
            f"the marker for {copy.entity} in {copy.target_engine} says {marker} rows have been "
            f"copied, and {copy.source_engine}.{copy.source_table} does not have that many. Rows "
            f"have left the source outside this library, so the marker describes a table that no "
            f"longer exists and resuming from it would be guessing. Nothing has been copied by "
            f"this call."
        )
    return position


def verify(
    session: Session, group: str, *, chunk_rows: int = CHUNK_ROWS
) -> VerifyReport:
    """Compare both copies of a group and report counts. The gate reads the counts, not the rows.

    Reads the **source first and the target second**, always, and the order is load-bearing. A write
    is in the source before it is in the copy, so a row read from the source may not have reached
    the copy yet - but the copy is read after the whole source window, so the window has already
    elapsed. Anything still missing is then looked up once more by point read, on what is normally
    an empty set. Reversing the two reads would make this flaky in the direction that stops a
    healthy migration.
    """
    if chunk_rows < 1:
        raise MigrationRefused(f"a chunk of {chunk_rows} rows is not a chunk")
    chunks_compared = 0
    chunks_mismatched = 0
    tail_rows_read = 0
    tail_missing = 0
    rows_source = 0
    rows_target = 0
    differences: list[Difference] = []
    suppressed = 0

    for copy in _plan(session, group):
        marker = copy.target.backfill_marker(
            materialization=copy.target_id, entity=copy.entity
        )
        rows_source += copy.source.count(copy.source_table)
        rows_target += copy.target.count(copy.target_table)
        after: tuple[Any, ...] | None = None
        seen = 0
        while True:
            below = seen < marker
            want = min(chunk_rows, marker - seen) if below else chunk_rows
            rows = copy.source.key_range(
                copy.source_table, copy.key, after=after, limit=want
            )
            if not rows:
                break
            high = tuple(rows[-1][column] for column in copy.key)
            missing = _missing_in_target(copy, rows, low=after, high=high)
            if below:
                chunks_compared += 1
                if missing:
                    chunks_mismatched += 1
            else:
                tail_rows_read += len(rows)
                tail_missing += len(missing)
            for difference in missing:
                if len(differences) < _DIFFERENCES_KEPT:
                    differences.append(difference)
                else:
                    suppressed += 1
            after = high
            seen += len(rows)

    return VerifyReport(
        at=datetime.now(UTC).isoformat(),
        group=group,
        chunks_compared=chunks_compared,
        chunks_mismatched=chunks_mismatched,
        tail_rows_read=tail_rows_read,
        tail_rows_missing_in_target=tail_missing,
        rows_source=rows_source,
        rows_target=rows_target,
        differences=tuple(differences),
        differences_suppressed=suppressed,
    )


def _missing_in_target(
    copy: _Copy,
    rows: Sequence[Mapping[str, Any]],
    *,
    low: tuple[Any, ...] | None,
    high: tuple[Any, ...],
) -> tuple[Difference, ...]:
    """Which of these source rows the target does not have, or has differently.

    One windowed read of the target instead of one point read per row, and then point reads only
    for what the window says is missing - which is normally nothing. The second look is what
    absorbs a fan-out that was in flight during the first: it happens after the whole window has
    been read, so the write has had that long to land.
    """
    mirror = copy.target.key_range(copy.target_table, copy.key, after=low, upto=high)
    index = {tuple(row[column] for column in copy.key): row for row in mirror}
    out: list[Difference] = []
    for row in rows:
        key = tuple(row[column] for column in copy.key)
        there = index.get(key)
        if there is not None and not _differing_columns(row, there):
            continue
        # Not there, or there and different. Look once more, directly, before calling it a loss.
        named = {column: row[column] for column in copy.key}
        again = copy.target.get(copy.target_table, named)
        if again is not None:
            differs = _differing_columns(row, again)
            if not differs:
                continue
            out.append(
                Difference(
                    entity=copy.entity, table=copy.target_table, key=named, columns=differs
                )
            )
            continue
        out.append(
            Difference(entity=copy.entity, table=copy.target_table, key=named, columns=())
        )
    return tuple(out)


def _differing_columns(
    source: Mapping[str, Any], target: Mapping[str, Any]
) -> tuple[str, ...]:
    """Columns of the **source** row whose values the target does not match, by name.

    Compared value by value rather than by digest, and the reason is diagnostics. Both copies are
    read by one process on one machine, so a checksum would compress a comparison that costs
    nothing to do exactly - and an exact comparison can say *which column* differs, which is the
    difference between an operator who can fix a migration and one who can only stop it.
    Requirement 9.3 asks for checksums to agree; equal values are a stronger statement than equal
    checksums, so this satisfies it rather than departing from it.

    Over the source's columns and not over the union, which is a decision rather than an oversight.
    ``ensure_schema`` allows a table to have columns the map does not name - it logs
    ``sde.schema.extra_columns`` and permits the write, because a client may have added one outside
    SDE - so a copy table with an extra column is a supported state. Comparing the union would
    then report *every* row as differing, on a column that has nothing to do with the copy, and
    stop a healthy migration. What the map describes is what the copy is about.
    """
    return tuple(
        sorted(
            column
            for column in source
            if source[column] != target.get(column, _ABSENT)
        )
    )

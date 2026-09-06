"""An in-memory engine, for the ``migration/`` conformance vectors and for anybody's adapter tests.

**Why this is in the library rather than in a test file.** The ``migration/`` vectors pin behaviour
that only happens against an engine: the order of the calls a backfill makes, the arithmetic of the
resume marker, which side of the marker a verify counter lands on. Every implementation therefore
needs an engine to run them against, and the two options were for each runner to write its own or
for the fixture to be shared. A runner that writes its own is a runner whose *fixture* can be the
thing that differs, and then a red vector means "one of two tables disagreed" rather than "one of
two libraries disagreed" - which is the failure the whole suite exists to avoid, one level down.

**What it is not.** It is not an implementation of anything in the format contract. It stores rows
in a list and answers questions about them; every rule the vectors check lives in
:mod:`sde.migration` and in :mod:`sde.watermark`. The one property it does have to get right is a
keyset scan, and the vectors pin the *calls* as well as the results, so a fixture that scanned
differently would show up as a different call sequence rather than as a plausible wrong answer.

It is also genuinely useful outside the vectors, which is why it is public: an adapter written
against :class:`sde.migration.Migratable` has the same behaviour to check, and checking it against a
real server is slow and checking it against nothing is what leaves a backfill copying the same chunk
forever.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from ..errors import EngineError
from ..placement import PhysicalLayout

__all__ = ["MemoryEngine", "Recorded", "engines_from"]


class Recorded:
    """The calls a set of engines received, in one sequence, each entry naming its engine.

    Kept because the ``migration/`` vectors pin the sequence and not only the outcome. A library
    that reached the same progress record by scanning the whole table and filtering in memory would
    satisfy every count and be unusable on a real table; the calls are the part that says how the
    result was obtained.

    **One journal for the whole engine set, and that is a fix.** The first version kept a list per
    engine, which cannot express the guarantee the dual-write cases are about: a row reaches the
    source *before* anything is attempted against the copy. Reversing those two lines passed every
    vector, because each engine's own list was still in order - and the vector's own note claimed
    that ordering was what it pinned. A guarantee across two engines needs one sequence.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def note(self, engine: str, method: str, **arguments: Any) -> None:
        self.calls.append({"engine": engine, "call": method, **arguments})

    def as_list(self) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self.calls]


def _sortable(value: Any) -> tuple[int, Any]:
    """A total order over the value kinds a vector may use, with the kind first.

    Comparing a string to an integer raises in Python and coerces in JavaScript, and neither is a
    key order. Vectors use one kind per column, so this never has to decide *between* kinds for a
    real comparison - the tag is there so that a vector which accidentally mixed them fails loudly
    in both languages instead of one.
    """
    if value is None:
        return (0, 0)
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, (int, float)):
        return (2, value)
    return (3, str(value))


def _key(order: Sequence[str], row: Mapping[str, Any]) -> tuple[tuple[int, Any], ...]:
    return tuple(_sortable(row.get(column)) for column in order)


class MemoryEngine:
    """One dialect, a set of named tables, and the two optional protocols an adapter may offer.

    ``dialect`` matters: the migration gate refuses a copy between two dialects whose precision
    differs, so a fixture that reported one dialect for both ends could not reach that refusal.

    ``can_keep_bookkeeping`` and ``can_migrate`` exist so a vector can build an engine that
    **cannot** take part, which is the case both of those refusals are about. They remove the
    methods rather than making them fail, because that is what a real adapter without the capability
    looks like - and the check is "will this object answer these calls".
    """

    def __init__(
        self,
        dialect: str = "postgres",
        *,
        name: str = "engine",
        journal: Recorded | None = None,
        tables: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        can_keep_bookkeeping: bool = True,
        can_migrate: bool = True,
        watermark: int | None = None,
        markers: Mapping[tuple[str, str], int] | None = None,
        fail_inserts: Mapping[str, int] | None = None,
    ) -> None:
        self.dialect = dialect
        self.tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in (tables or {}).items()
        }
        self.name = name
        self.recorded = journal if journal is not None else Recorded()
        # How many of the next inserts into each table must fail. The one thing a fake has to be
        # able to do that a real engine does on its own: a fan-out that does not reach the copy is
        # the case the whole dual-write design is about, and it cannot be reached by writing
        # correct rows to a working table.
        self._fail_inserts: dict[str, int] = dict(fail_inserts or {})
        self._watermarks: list[int] = [] if watermark is None else [watermark]
        self._markers: dict[tuple[str, str], list[int]] = {
            key: [value] for key, value in (markers or {}).items()
        }
        # **Bound onto the instance when the capability is on, and genuinely absent when it is
        # off.** Setting them to ``None`` instead was the first attempt and it does not work:
        # ``satisfies`` asks ``hasattr``, deliberately - a member that exists and is not callable
        # fails at the call with a message naming it, which is a better failure than a capability
        # check that quietly answers "no". So an attribute set to None reads as *present*, and the
        # engine that was supposed to be unable to take part took part and crashed. Absence is also
        # what a real adapter without the capability looks like.
        if can_keep_bookkeeping:
            self.map_watermark = self._map_watermark
            self.record_map_version = self._record_map_version
        if can_migrate:
            self.key_range = self._key_range
            self.nth_key = self._nth_key
            self.copy_in = self._copy_in
            self.count = self._count
            self.backfill_marker = self._backfill_marker
            self.record_backfill_marker = self._record_backfill_marker

    # --- Engine ------------------------------------------------------------------------------

    def ensure_schema(self, layout: PhysicalLayout, *, keys: Mapping[str, Any]) -> None:
        self.recorded.note(self.name, "ensure_schema", tables=sorted(layout.tables.values()))
        for table in layout.tables.values():
            self.tables.setdefault(table, [])

    def insert(self, table: str, values: Mapping[str, Any]) -> None:
        self.recorded.note(self.name, "insert", table=table)
        remaining = self._fail_inserts.get(table, 0)
        if remaining > 0:
            self._fail_inserts[table] = remaining - 1
            raise EngineError(f"insert into {table} failed: this engine was told to refuse it")
        self.tables.setdefault(table, []).append(dict(values))

    def get(self, table: str, key: Mapping[str, Any]) -> dict[str, Any] | None:
        self.recorded.note(self.name, "get", table=table)
        for row in self.tables.get(table, []):
            if all(row.get(column) == value for column, value in key.items()):
                return dict(row)
        return None

    @contextmanager
    def transaction(self) -> Iterator[MemoryEngine]:
        """Snapshot, yield, and put the snapshot back on failure.

        Enough to make a rollback observable, which is what the dual-write tests need: rows written
        inside a transaction that raises must not reach the copy, and the only way to check that is
        for the source to forget them too.
        """
        self.recorded.note(self.name, "transaction")
        snapshot = {name: [dict(row) for row in rows] for name, rows in self.tables.items()}
        try:
            yield self
        except BaseException:
            self.tables = snapshot
            raise

    # --- WatermarkStore ----------------------------------------------------------------------

    def _map_watermark(self) -> int | None:
        self.recorded.note(self.name, "map_watermark")
        return max(self._watermarks) if self._watermarks else None

    def _record_map_version(self, version: int, *, model_version: str) -> None:
        self.recorded.note(self.name, "record_map_version", version=version)
        self._watermarks.append(version)

    # --- Migratable --------------------------------------------------------------------------

    def _key_range(
        self,
        table: str,
        order: Sequence[str],
        *,
        after: Sequence[Any] | None = None,
        upto: Sequence[Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        from ..migration import key_columns, same_width

        cols = key_columns(order, table)
        if after is not None:
            same_width(after, cols, "after")
        if upto is not None:
            same_width(upto, cols, "upto")
        self.recorded.note(
            self.name,
            "key_range",
            table=table,
            after=None if after is None else list(after),
            upto=None if upto is None else list(upto),
            limit=limit,
        )
        rows = sorted(self.tables.get(table, []), key=lambda row: _key(cols, row))
        if after is not None:
            low = tuple(_sortable(value) for value in after)
            rows = [row for row in rows if _key(cols, row) > low]
        if upto is not None:
            high = tuple(_sortable(value) for value in upto)
            rows = [row for row in rows if _key(cols, row) <= high]
        if limit is not None:
            rows = rows[:limit]
        return [dict(row) for row in rows]

    def _nth_key(
        self, table: str, order: Sequence[str], *, position: int
    ) -> tuple[Any, ...] | None:
        from ..migration import key_columns

        cols = key_columns(order, table)
        self.recorded.note(self.name, "nth_key", table=table, position=position)
        if position < 1:
            raise EngineError(f"position is one-based; {position} is not a row")
        rows = sorted(self.tables.get(table, []), key=lambda row: _key(cols, row))
        if position > len(rows):
            return None
        row = rows[position - 1]
        return tuple(row.get(column) for column in cols)

    def _copy_in(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        self.recorded.note(self.name, "copy_in", table=table, rows=len(rows))
        if not rows:
            return
        columns = sorted(rows[0])
        for row in rows:
            if sorted(row) != columns:
                raise EngineError(
                    f"copy_in into {table} was given rows with different columns "
                    f"({columns} and {sorted(row)}). A chunk comes from one table, so this is a "
                    f"caller assembling it from two."
                )
        existing = self.tables.setdefault(table, [])
        # Idempotent on the whole row's identity, which is what both real targets do by a different
        # mechanism: ON CONFLICT DO NOTHING in PostgreSQL and a ReplacingMergeTree collapse in
        # ClickHouse. Not the same mechanism, which is why the adapters have live tests in every
        # direction and this only has to absorb a recopy.
        for row in rows:
            same = (
                all(present.get(column) == row.get(column) for column in row)
                for present in existing
            )
            if not any(same):
                existing.append(dict(row))

    def _count(self, table: str) -> int:
        self.recorded.note(self.name, "count", table=table)
        return len(self.tables.get(table, []))

    def _backfill_marker(self, *, materialization: str, entity: str) -> int:
        self.recorded.note(
            self.name, "backfill_marker", materialization=materialization, entity=entity
        )
        seen = self._markers.get((materialization, entity))
        return max(seen) if seen else 0

    def _record_backfill_marker(self, *, materialization: str, entity: str, rows: int) -> None:
        self.recorded.note(
            self.name,
            "record_backfill_marker",
            materialization=materialization,
            entity=entity,
            rows=rows,
        )
        self._markers.setdefault((materialization, entity), []).append(rows)


def engines_from(
    spec: Mapping[str, Mapping[str, Any]], journal: Recorded | None = None
) -> dict[str, MemoryEngine]:
    """Build the engine set a ``migration/`` case describes.

    Here rather than in each runner for the same reason the engine itself is here: two runners that
    each read this document their own way can disagree about the *fixture*, and then a red vector
    says "one of two tables differed" instead of "one of two libraries differed". The generator uses
    this too, so the document a vector carries is read by exactly one piece of code per language.
    """
    shared = journal if journal is not None else Recorded()
    built: dict[str, MemoryEngine] = {}
    for name, body in spec.items():
        built[name] = MemoryEngine(
            dialect=str(body.get("dialect", "postgres")),
            name=name,
            journal=shared,
            tables={
                table: [dict(row) for row in rows]
                for table, rows in (body.get("tables") or {}).items()
            },
            can_keep_bookkeeping=bool(body.get("bookkeeping", True)),
            can_migrate=bool(body.get("migratable", True)),
            watermark=body.get("watermark"),
            markers={
                (key.split("|", 1)[0], key.split("|", 1)[1]): int(value)
                for key, value in (body.get("markers") or {}).items()
            },
            fail_inserts={
                table: int(count) for table, count in (body.get("fail_inserts") or {}).items()
            },
        )
    return built

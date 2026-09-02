"""Backfill and verify: the two halves of a migration that touch data, and therefore ours to write.

Tasks 12.4 and 12.5. The control plane holds the state machine and gates on what comes back from
here, and what comes back is counts - so the test that carries the most weight in this file is not
about copying at all. It is
:func:`test_the_record_carries_counts_and_the_difference_carries_the_row`, which asserts that a
client's own key value appears in the object their operator reads and **nowhere** in the record that
crosses to us. Everything else here is about not losing a row; that one is about not taking one.

The rest divides in two. The refusals all fire before a single chunk moves, because the cost of a
late discovery in a migration is measured in hours of the client's I/O. And the copy's correctness
rests on three properties that hold each other up - the marker is a row count, the chunk is written
before the marker moves, and the copy is idempotent - so each of the three has a test that fails if
it is removed.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

import pytest

import sde
from sde.layout import POSTGRES_TYPES


class Store:
    """An engine that keeps rows in memory. Satisfies Engine and Migratable.

    ``copy_in`` skips a row whose key is already present, which is what both real adapters do by
    different means - a conflict clause in PostgreSQL, a collapsing merge tree in ClickHouse. A
    fake that appended blindly would let an idempotence bug pass.
    """

    def __init__(
        self,
        name: str,
        *,
        dialect: str = "postgres",
        keys: Mapping[str, Sequence[str]] | None = None,
        calls: list[str] | None = None,
    ) -> None:
        self.name = name
        self.dialect = dialect
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.keys = {t: tuple(k) for t, k in (keys or {}).items()}
        self.markers: dict[tuple[str, str], list[int]] = {}
        self.seen: list[int] = []
        self.calls = calls if calls is not None else []
        self.range_sizes: list[int] = []
        self.fail_marker_writes = False
        self.hide_from_range: set[Any] = set()

    # --- Engine ---------------------------------------------------------------------------------

    def ensure_schema(self, layout: Any, *, keys: Mapping[str, Any]) -> None:
        return None

    def insert(self, table: str, values: Mapping[str, Any]) -> None:
        self.tables.setdefault(table, []).append(dict(values))

    def get(self, table: str, key: Mapping[str, Any]) -> dict[str, Any] | None:
        self.calls.append(f"{self.name}.get")
        for row in self.tables.get(table, []):
            if all(row.get(k) == v for k, v in key.items()):
                return dict(row)
        return None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield

    # --- Migratable -----------------------------------------------------------------------------

    def _sorted(self, table: str, order: Sequence[str]) -> list[dict[str, Any]]:
        return sorted(
            self.tables.get(table, []), key=lambda row: tuple(row[c] for c in order)
        )

    def key_range(
        self,
        table: str,
        order: Sequence[str],
        *,
        after: Sequence[Any] | None = None,
        upto: Sequence[Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append(f"{self.name}.key_range")
        cols = sde.migration.key_columns(order, table)
        if after is not None:
            sde.migration.same_width(after, cols, "after")
        if upto is not None:
            sde.migration.same_width(upto, cols, "upto")
        out = []
        for row in self._sorted(table, cols):
            key = tuple(row[c] for c in cols)
            if after is not None and key <= tuple(after):
                continue
            if upto is not None and key > tuple(upto):
                continue
            if key in self.hide_from_range:
                continue
            out.append(dict(row))
            if limit is not None and len(out) >= limit:
                break
        self.range_sizes.append(len(out))
        return out

    def nth_key(
        self, table: str, order: Sequence[str], *, position: int
    ) -> tuple[Any, ...] | None:
        cols = sde.migration.key_columns(order, table)
        rows = self._sorted(table, cols)
        if position < 1 or position > len(rows):
            return None
        row = rows[position - 1]
        return tuple(row[c] for c in cols)

    def copy_in(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        self.calls.append(f"{self.name}.copy_in")
        if not rows:
            return
        key = self.keys.get(table)
        if key is None:
            raise AssertionError(f"the fake was not told the key of {table}")
        present = {tuple(row[c] for c in key) for row in self.tables.get(table, [])}
        for row in rows:
            if tuple(row[c] for c in key) in present:
                continue
            self.tables.setdefault(table, []).append(dict(row))

    def count(self, table: str) -> int:
        return len(self.tables.get(table, []))

    # --- WatermarkStore ---------------------------------------------------------------------
    #
    # Both real adapters satisfy this protocol as well, so the fake does too - otherwise a test
    # about a wrapped adapter would be testing a fake that is unlike either engine.

    def map_watermark(self) -> int | None:
        return max(self.seen) if self.seen else None

    def record_map_version(self, version: int, *, model_version: str) -> None:
        self.seen.append(version)

    # --- Migratable -----------------------------------------------------------------------------

    def backfill_marker(self, *, materialization: str, entity: str) -> int:
        written = self.markers.get((materialization, entity))
        return max(written) if written else 0

    def record_backfill_marker(
        self, *, materialization: str, entity: str, rows: int
    ) -> None:
        if self.fail_marker_writes:
            raise sde.EngineError(f"{self.name} will not record progress")
        self.markers.setdefault((materialization, entity), []).append(rows)


class Plain:
    """An engine with no row-level operations at all - our orderbook engine, in miniature."""

    dialect = "orderbook"

    def ensure_schema(self, layout: Any, *, keys: Mapping[str, Any]) -> None:
        return None

    def insert(self, table: str, values: Mapping[str, Any]) -> None:
        return None

    def get(self, table: str, key: Mapping[str, Any]) -> dict[str, Any] | None:
        return None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield


@pytest.fixture(autouse=True)
def _isolate() -> None:
    sde.clear_registry()


def _model() -> sde.LogicalModel:
    @sde.entity
    class Reading:
        id: sde.Int32
        station: str

    return sde.build_model(Reading)


def _columns() -> dict[str, dict[str, str]]:
    return {"Reading": {"id": "integer", "station": "text"}}


def _map(
    model: sde.LogicalModel,
    *,
    fan_out: bool = True,
    target_columns: Mapping[str, Mapping[str, str]] | None = None,
    source_engine: str = "pg",
    target_engine: str = "ch",
) -> sde.PlacementMap:
    group = sde.colocation_groups(model)[0].name
    body: dict[str, Any] = {
        "source": {
            "id": "src@pg",
            "engine": source_engine,
            "layout": {"tables": {"Reading": "reading"}, "columns": _columns()},
        },
        "derived": [
            {
                "id": "copy@ch",
                "engine": target_engine,
                "layout": {
                    "tables": {"Reading": "reading_copy"},
                    "columns": dict(target_columns) if target_columns else _columns(),
                },
                "lag_budget_ms": 30_000,
            }
        ],
    }
    if fan_out:
        body["also_write"] = ["copy@ch"]
    return sde.load_map(
        {
            "contract": sde.MAP_CONTRACT,
            "model_version": model.version,
            "map_version": 4,
            "groups": {group: body},
        },
        model=model,
    )


def _session(
    *,
    rows: int = 0,
    fan_out: bool = True,
    target_columns: Mapping[str, Mapping[str, str]] | None = None,
    target_dialect: str = "postgres",
) -> tuple[sde.Session, Store, Store, list[str]]:
    model = _model()
    calls: list[str] = []
    source = Store("pg", keys={"reading": ("id",)}, calls=calls)
    target = Store(
        "ch",
        dialect=target_dialect,
        keys={"reading_copy": ("id",)},
        calls=calls,
    )
    for n in range(1, rows + 1):
        source.insert("reading", {"id": n, "station": f"s{n}"})
    session = sde.Session(
        model,
        _map(model, fan_out=fan_out, target_columns=target_columns),
        {"pg": source, "ch": target},
    )
    return session, source, target, calls


def _group(session: sde.Session) -> str:
    return sde.colocation_groups(session.model)[0].name


# ── the boundary: numbers cross, rows do not ─────────────────────────────────────────────────────


SECRET = "ORD-88213-would-be-a-leak"


def _keyed_by_a_string() -> tuple[sde.Session, Store, str]:
    """A model whose key is a string, so a key value is unmistakable in a serialised record."""

    @sde.entity
    class Ticket:
        reference: str
        note: str

        class Meta:
            key = ["reference"]

    model = sde.build_model(Ticket)
    group = sde.colocation_groups(model)[0].name
    columns = {"Ticket": {"reference": "text", "note": "text"}}
    source = Store("pg", keys={"ticket": ("reference",)})
    target = Store("ch", keys={"ticket_copy": ("reference",)})
    session = sde.Session(
        model,
        sde.load_map(
            {
                "contract": sde.MAP_CONTRACT,
                "model_version": model.version,
                "map_version": 1,
                "groups": {
                    group: {
                        "source": {
                            "id": "src@pg",
                            "engine": "pg",
                            "layout": {"tables": {"Ticket": "ticket"}, "columns": columns},
                        },
                        "derived": [
                            {
                                "id": "copy@ch",
                                "engine": "ch",
                                "layout": {
                                    "tables": {"Ticket": "ticket_copy"},
                                    "columns": columns,
                                },
                                "lag_budget_ms": 1000,
                            }
                        ],
                        "also_write": ["copy@ch"],
                    }
                },
            },
            model=model,
        ),
        {"pg": source, "ch": target},
    )
    return session, source, group


def test_the_record_carries_counts_and_the_difference_carries_the_row() -> None:
    """The one invariant this whole module is arranged around, made executable.

    A mismatch has to be diagnosable - "which row" is the first thing an operator needs - and it
    must not be reportable to us. Both halves are asserted here against the same object, because
    either one alone reads as satisfied while the other is broken.
    """
    session, source, group = _keyed_by_a_string()
    source.insert("ticket", {"reference": SECRET, "note": "n"})
    report = sde.verify(session, group)

    assert not report.matched
    assert report.differences[0].key == {"reference": SECRET}
    assert SECRET in report.differences[0].for_a_human()
    assert SECRET not in json.dumps(report.as_record())
    assert SECRET not in report.for_a_human().replace(
        report.differences[0].for_a_human(), ""
    )


def test_the_record_has_exactly_the_fields_the_gate_reads() -> None:
    """Seven counts and a timestamp. A field added here is a field a row could travel in, so the
    set is pinned rather than sampled."""
    session, _, _, _ = _session(rows=1)
    record = sde.verify(session, _group(session)).as_record()
    assert set(record) == {
        "at",
        "chunks_compared",
        "chunks_mismatched",
        "tail_rows_read",
        "tail_rows_missing_in_target",
        "rows_source",
        "rows_target",
    }
    assert json.loads(json.dumps(record)) == record


def test_the_human_rendering_says_the_detail_is_the_clients_own_data() -> None:
    """An operator pasting this into a support ticket is their act; saying so is ours."""
    session, source, _, _ = _session(rows=1)
    source.insert("reading", {"id": 9, "station": "s9"})
    text = sde.verify(session, _group(session)).for_a_human()
    assert "your own data" in text
    assert "not part of what is reported" in text


# ── refusals, all of them before a row moves ─────────────────────────────────────────────────────


def test_a_group_the_model_does_not_have_is_refused_by_name() -> None:
    session, _, _, _ = _session(rows=1)
    with pytest.raises(sde.MigrationRefused, match="not a colocation group"):
        sde.backfill(session, "Nonexistent")


def test_a_map_without_a_fan_out_target_is_a_map_that_says_no_migration() -> None:
    session, _, _, _ = _session(rows=1, fan_out=False)
    with pytest.raises(sde.MigrationRefused, match="no fan-out target"):
        sde.backfill(session, _group(session))


def test_an_engine_with_no_row_operations_refuses_rather_than_copying_nothing() -> None:
    """The orderbook engine's case. A silent skip here is the worst outcome available."""
    model = _model()
    source = Store("pg", keys={"reading": ("id",)})
    session = sde.Session(model, _map(model), {"pg": source, "ch": Plain()})
    with pytest.raises(sde.MigrationRefused, match="cannot act as the target"):
        sde.backfill(session, _group(session))


def test_a_source_with_no_row_operations_refuses_too_and_says_which_role() -> None:
    model = _model()
    target = Store("ch", keys={"reading_copy": ("id",)})
    session = sde.Session(model, _map(model), {"pg": Plain(), "ch": target})
    with pytest.raises(sde.MigrationRefused, match="cannot act as the source"):
        sde.backfill(session, _group(session))


def test_a_target_with_different_columns_is_a_reshape_and_not_a_move() -> None:
    """A wide denormalised copy is a legitimate materialisation and not a migration target."""
    session, _, _, _ = _session(
        rows=1,
        target_columns={
            "Reading": {"id": "integer", "station": "text", "station_country": "text"}
        },
    )
    with pytest.raises(sde.MigrationRefused, match="only in the target: \\['station_country'\\]"):
        sde.backfill(session, _group(session))


def test_a_layout_that_names_tables_but_no_columns_cannot_be_checked_for_shape() -> None:
    session, _, _, _ = _session(rows=1, target_columns={"Reading": {}})
    with pytest.raises(sde.MigrationRefused, match="does not describe the columns"):
        sde.backfill(session, _group(session))


def test_verify_makes_every_one_of_those_refusals_too() -> None:
    """One plan function, so a refusal cannot apply to the copy and not to the check."""
    session, _, _, _ = _session(rows=1, fan_out=False)
    with pytest.raises(sde.MigrationRefused, match="no fan-out target"):
        sde.verify(session, _group(session))


def test_a_chunk_of_no_rows_is_not_a_chunk() -> None:
    session, _, _, _ = _session(rows=1)
    for call in (sde.backfill, sde.verify):
        with pytest.raises(sde.MigrationRefused, match="is not a chunk"):
            call(session, _group(session), chunk_rows=0)


# ── precision: the loss that is silent and universal, refused before the work ────────────────────


def _timestamp_session(*, target_dialect: str) -> tuple[sde.Session, str]:
    @sde.entity
    class Event:
        id: sde.Int32
        at: sde.Timestamp

    model = sde.build_model(Event)
    columns = {"Event": {"id": "integer", "at": "timestamptz"}}
    group = sde.colocation_groups(model)[0].name
    raw = {
        "contract": sde.MAP_CONTRACT,
        "model_version": model.version,
        "map_version": 1,
        "groups": {
            group: {
                "source": {
                    "id": "src@pg",
                    "engine": "pg",
                    "layout": {"tables": {"Event": "event"}, "columns": columns},
                },
                "derived": [
                    {
                        "id": "copy@t",
                        "engine": "t",
                        "layout": {
                            "tables": {"Event": "event_copy"},
                            "columns": columns,
                        },
                        "lag_budget_ms": 1000,
                    }
                ],
                "also_write": ["copy@t"],
            }
        },
    }
    session = sde.Session(
        model,
        sde.load_map(raw, model=model),
        {
            "pg": Store("pg", dialect="postgres", keys={"event": ("id",)}),
            "t": Store("t", dialect=target_dialect, keys={"event_copy": ("id",)}),
        },
    )
    return session, group


def test_a_microsecond_column_moving_to_a_millisecond_engine_is_refused_before_the_copy() -> None:
    """PostgreSQL keeps six sub-second digits and ClickHouse three. `datetime.now()` has
    microseconds, so the truncation is silent and hits essentially every row - and `verify` would
    otherwise report every row mismatched at the end of a copy that took hours."""
    session, group = _timestamp_session(target_dialect="clickhouse")
    with pytest.raises(sde.MigrationRefused, match="6 sub-second digits and clickhouse to 3"):
        sde.backfill(session, group)


def test_the_same_column_moving_the_other_way_is_fine() -> None:
    """Widening is not narrowing. A refusal in both directions would be a refusal of migrations."""
    session, group = _timestamp_session(target_dialect="postgres")
    assert sde.backfill(session, group).complete


def test_a_neutral_type_nobody_classified_refuses_rather_than_being_guessed_at() -> None:
    session, group = _timestamp_session(target_dialect="postgres")
    saved = dict(sde.migration.DIALECT_PRECISION)
    try:
        sde.migration.DIALECT_PRECISION.clear()  # type: ignore[attr-defined]
        with pytest.raises(sde.MigrationRefused, match="does not know whether"):
            sde.backfill(session, group)
    finally:
        sde.migration.DIALECT_PRECISION.update(saved)  # type: ignore[attr-defined]


def test_every_neutral_type_is_either_precision_independent_or_has_a_number() -> None:
    """A frozen partition of the vocabulary, so adding a neutral type forces a decision here.

    Without this the default answer for a new type would be inherited rather than made - and the
    inherited answer is the refusal, which is safe but arrives as a mystery at somebody's migration.
    """
    vocabulary = set(POSTGRES_TYPES)
    classified = set(sde.PRECISION_INDEPENDENT) | {
        neutral for neutral, _ in sde.DIALECT_PRECISION
    }
    assert vocabulary == classified, sorted(vocabulary ^ classified)


# ── the backfill: three properties that hold each other up ───────────────────────────────────────


def test_a_backfill_copies_every_row_and_the_marker_is_the_row_count() -> None:
    session, source, target, _ = _session(rows=25)
    progress = sde.backfill(session, _group(session), chunk_rows=10)
    assert progress.complete
    assert progress.rows_this_run == 25
    assert target.count("reading_copy") == 25
    assert target.backfill_marker(materialization="copy@ch", entity="Reading") == 25
    assert progress.entities[0].chunks == 3
    # Three reads and not four: a short chunk is the end of the table, so there is no confirming
    # empty read. One saved round trip per run against the client's own engine.
    assert len(source.range_sizes) == 3


def test_a_run_that_stops_early_resumes_from_the_marker_and_copies_each_row_once() -> None:
    session, _, target, _ = _session(rows=25)
    group = _group(session)
    first = sde.backfill(session, group, chunk_rows=10, stop_after=1)
    assert not first.complete
    assert first.rows_this_run == 10

    second = sde.backfill(session, group, chunk_rows=10)
    assert second.complete
    assert second.rows_this_run == 15
    assert target.count("reading_copy") == 25
    assert sorted(row["id"] for row in target.tables["reading_copy"]) == list(range(1, 26))


def test_a_backfill_that_has_finished_does_nothing_when_called_again() -> None:
    session, _, target, _ = _session(rows=7)
    group = _group(session)
    assert sde.backfill(session, group, chunk_rows=3).complete
    again = sde.backfill(session, group, chunk_rows=3)
    assert again.complete
    assert again.rows_this_run == 0
    assert target.count("reading_copy") == 7


def test_the_chunk_is_written_before_the_marker_so_a_crash_between_them_costs_a_recopy() -> None:
    """The ordering, and the reason it is the safe one. With the marker first, this window would
    cost the chunk permanently instead of costing it twice."""
    session, _, target, calls = _session(rows=5)
    group = _group(session)
    target.fail_marker_writes = True
    with pytest.raises(sde.EngineError):
        sde.backfill(session, group, chunk_rows=2)

    assert calls.index("ch.copy_in") < len(calls)
    assert target.count("reading_copy") == 2, "the chunk landed"
    assert target.backfill_marker(materialization="copy@ch", entity="Reading") == 0

    target.fail_marker_writes = False
    resumed = sde.backfill(session, group, chunk_rows=2)
    assert resumed.complete
    assert target.count("reading_copy") == 5, "recopied, not duplicated, and nothing lost"


def test_a_recopied_chunk_leaves_one_row_and_not_two() -> None:
    """The idempotence the ordering above depends on. Both real adapters get it from the target's
    own key semantics; a fake that appended blindly would let this pass."""
    session, source, target, _ = _session(rows=4)
    group = _group(session)
    sde.backfill(session, group, chunk_rows=4)
    target.markers.clear()
    sde.backfill(session, group, chunk_rows=4)
    assert target.count("reading_copy") == 4
    assert source.count("reading") == 4


def test_a_source_that_has_lost_rows_refuses_rather_than_resuming_from_a_guess() -> None:
    session, source, _, _ = _session(rows=10)
    group = _group(session)
    sde.backfill(session, group, chunk_rows=10)
    source.tables["reading"] = source.tables["reading"][:4]
    with pytest.raises(sde.MigrationRefused, match="does not have that many"):
        sde.backfill(session, group)


def test_no_row_that_predates_the_backfill_is_ever_stepped_over() -> None:
    """The claim a row-count marker actually supports, which is narrower than the obvious one.

    Rows written *during* the migration are inserted below the resume key here on purpose, because
    a key is not required to increase with insertion time. The resume key moves down, the backfill
    recopies, and the new rows below it are stepped over - safe only because they reached the copy
    through the fan-out, which is the same premise the absence of a ceiling rests on. What must
    hold, and does, is that every row that existed when the backfill began is copied.
    """
    session, _, target, _ = _session(rows=10)
    group = _group(session)
    sde.backfill(session, group, chunk_rows=5, stop_after=1)
    assert target.backfill_marker(materialization="copy@ch", entity="Reading") == 5

    for n in (-3, -2, -1):
        session.save("Reading", {"id": n, "station": f"s{n}"})

    assert sde.backfill(session, group, chunk_rows=5).complete
    assert sorted(row["id"] for row in target.tables["reading_copy"]) == sorted(
        [-3, -2, -1, *range(1, 11)]
    )
    assert sde.verify(session, group, chunk_rows=5).matched


def test_a_lost_fan_out_below_the_final_marker_is_still_caught_one_mechanism_over() -> None:
    """The sharp edge of the paragraph above, asserted rather than left as prose.

    A row written during the migration can land below the marker in key order, so a fan-out that
    lost it shows up against the chunks instead of against the tail. The attribution is off by one
    mechanism; the row is still caught, which is the right way round for the two errors to be.
    """
    session, _, target, _ = _session(rows=10)
    group = _group(session)
    sde.backfill(session, group, chunk_rows=5, stop_after=1)
    session.save("Reading", {"id": -1, "station": "s-1"})
    target.tables["reading_copy"] = [
        row for row in target.tables["reading_copy"] if row["id"] != -1
    ]
    assert sde.backfill(session, group, chunk_rows=5).complete

    report = sde.verify(session, group, chunk_rows=5)
    assert not report.matched
    assert report.chunks_mismatched == 1
    assert report.tail_rows_missing_in_target == 0
    assert report.differences[0].key == {"id": -1}


def test_a_composite_key_paginates_as_one_ordering_and_not_as_two() -> None:
    @sde.entity
    class Slot:
        tenant: sde.Int32
        seq: sde.Int32

        class Meta:
            key = ["tenant", "seq"]

    model = sde.build_model(Slot)
    group = sde.colocation_groups(model)[0].name
    columns = {"Slot": {"tenant": "integer", "seq": "integer"}}
    source = Store("pg", keys={"slot": ("tenant", "seq")})
    target = Store("ch", keys={"slot_copy": ("tenant", "seq")})
    for tenant in (1, 2, 3):
        for seq in range(1, 5):
            source.insert("slot", {"tenant": tenant, "seq": seq})
    session = sde.Session(
        model,
        sde.load_map(
            {
                "contract": sde.MAP_CONTRACT,
                "model_version": model.version,
                "map_version": 1,
                "groups": {
                    group: {
                        "source": {
                            "id": "src@pg",
                            "engine": "pg",
                            "layout": {"tables": {"Slot": "slot"}, "columns": columns},
                        },
                        "derived": [
                            {
                                "id": "copy@ch",
                                "engine": "ch",
                                "layout": {
                                    "tables": {"Slot": "slot_copy"},
                                    "columns": columns,
                                },
                                "lag_budget_ms": 1000,
                            }
                        ],
                        "also_write": ["copy@ch"],
                    }
                },
            },
            model=model,
        ),
        {"pg": source, "ch": target},
    )
    assert sde.backfill(session, group, chunk_rows=5).complete
    assert target.count("slot_copy") == 12
    assert sde.verify(session, group).matched


def test_the_progress_log_fires_per_chunk_and_carries_no_value_of_the_clients(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session, _, _, _ = _session(rows=6)
    with caplog.at_level(logging.INFO, logger="sde"):
        sde.backfill(session, _group(session), chunk_rows=2)
    events = [
        record
        for record in caplog.records
        if getattr(record, "sde_event", None) == "sde.migration.backfill_progress"
    ]
    assert len(events) == 3
    fields = events[-1].sde_fields  # type: ignore[attr-defined]
    assert fields["chunk"] == 3
    assert fields["rows_copied"] == 6
    assert set(fields) == {"group", "entity", "engine", "table", "chunk", "rows", "rows_copied"}


# ── verify: which mechanism failed, and the reads in the order that makes it decidable ───────────


def test_a_complete_copy_matches_and_counts_the_chunks_below_the_marker() -> None:
    session, _, _, _ = _session(rows=25)
    group = _group(session)
    sde.backfill(session, group, chunk_rows=10)
    report = sde.verify(session, group, chunk_rows=10)
    assert report.matched
    assert report.chunks_compared == 3
    assert report.chunks_mismatched == 0
    assert report.tail_rows_read == 0
    assert report.rows_source == report.rows_target == 25


def test_a_row_missing_below_the_marker_says_the_backfill_did_not_copy_it() -> None:
    session, _, target, _ = _session(rows=20)
    group = _group(session)
    sde.backfill(session, group, chunk_rows=10)
    target.tables["reading_copy"] = [
        row for row in target.tables["reading_copy"] if row["id"] != 3
    ]
    report = sde.verify(session, group, chunk_rows=10)
    assert not report.matched
    assert report.chunks_mismatched == 1
    assert report.tail_rows_missing_in_target == 0
    assert "the backfill did not copy these" in report.for_a_human()


def test_a_write_lost_by_the_fan_out_is_caught_above_the_marker() -> None:
    """Task 12.11 in its smallest form: a row the dual write did not deliver, deliberately.

    The distinction between this test and the one above is the whole reason there are two pairs of
    counters. Same rule, same tolerance, different cause - and the operator needs the cause.
    """
    session, _, target, _ = _session(rows=10)
    group = _group(session)
    sde.backfill(session, group, chunk_rows=10)

    session.save("Reading", {"id": 11, "station": "s11"})
    assert target.count("reading_copy") == 11
    target.tables["reading_copy"] = [
        row for row in target.tables["reading_copy"] if row["id"] != 11
    ]

    report = sde.verify(session, group, chunk_rows=10)
    assert not report.matched
    assert report.chunks_mismatched == 0
    assert report.tail_rows_read == 1
    assert report.tail_rows_missing_in_target == 1
    assert report.differences[0].absent
    assert "the dual-write fan-out did not reach these" in report.for_a_human()


def test_a_row_present_with_a_different_value_names_the_column() -> None:
    """The reason the comparison is value by value rather than by digest: a checksum can say a
    chunk differs and cannot say a column does."""
    session, _, target, _ = _session(rows=3)
    group = _group(session)
    sde.backfill(session, group, chunk_rows=3)
    for row in target.tables["reading_copy"]:
        if row["id"] == 2:
            row["station"] = "somewhere-else"
    report = sde.verify(session, group, chunk_rows=3)
    assert not report.matched
    assert report.differences[0].columns == ("station",)
    assert not report.differences[0].absent


def test_the_source_window_is_read_before_the_target_window() -> None:
    """Load-bearing ordering. A write is in the source before it is in the copy, so reading the
    copy first would find rows legitimately in flight and stop a healthy migration."""
    session, _, target, calls = _session(rows=4)
    group = _group(session)
    sde.backfill(session, group, chunk_rows=2)
    calls.clear()
    target.range_sizes.clear()
    sde.verify(session, group, chunk_rows=2)
    windows = [call for call in calls if call.endswith(".key_range")]
    assert windows == ["pg.key_range", "ch.key_range"] * 2 + ["pg.key_range"]
    # Both sides of a comparison are held in memory at once, so both reads are bounded by the
    # chunk. A target window without an upper bound would read the whole remaining table per chunk.
    assert max(target.range_sizes) <= 2


def test_a_row_the_window_missed_is_looked_up_once_more_before_it_is_called_lost() -> None:
    """The second look absorbs a fan-out that was in flight during the first, and it is a point
    read on what is normally an empty set rather than a second scan."""
    session, _, target, _ = _session(rows=4)
    group = _group(session)
    sde.backfill(session, group, chunk_rows=4)
    target.hide_from_range = {(2,)}
    report = sde.verify(session, group, chunk_rows=4)
    assert report.matched, "present, just not in the window read"
    assert report.chunks_mismatched == 0


def test_extra_rows_in_the_target_are_reported_and_not_gated_on() -> None:
    """Two counts of live tables are taken at different instants, so gating on them would be
    flaky or would need the fudge this module refuses to choose."""
    session, _, target, _ = _session(rows=5)
    group = _group(session)
    sde.backfill(session, group, chunk_rows=5)
    target.insert("reading_copy", {"id": 99, "station": "left over"})
    report = sde.verify(session, group, chunk_rows=5)
    assert report.matched
    assert report.rows_target == 6
    assert report.rows_source == 5
    assert "reported, not gated on" in report.for_a_human()


def test_the_kept_differences_are_capped_and_the_rest_are_counted() -> None:
    session, source, _, _ = _session(rows=0)
    group = _group(session)
    for n in range(1, 40):
        source.insert("reading", {"id": n, "station": f"s{n}"})
    report = sde.verify(session, group, chunk_rows=50)
    assert len(report.differences) == 20
    assert report.differences_suppressed == 19
    assert "and 19 more" in report.for_a_human()


def test_a_backfill_marker_is_scoped_to_the_target_and_the_entity() -> None:
    """Two groups could name a materialisation the same way; an entity belongs to one group, so
    the pair cannot collide. Asserted because the alternative is a marker read across a migration
    that was never run."""
    session, _, target, _ = _session(rows=4)
    sde.backfill(session, _group(session), chunk_rows=4)
    assert target.backfill_marker(materialization="copy@ch", entity="Reading") == 4
    assert target.backfill_marker(materialization="copy@ch", entity="Other") == 0
    assert target.backfill_marker(materialization="elsewhere", entity="Reading") == 0


def test_the_chunk_boundary_lands_on_the_marker_and_not_past_it() -> None:
    """A partial marker with a wider chunk, which is the only shape that tells the two apart.

    With the marker at 5 and chunks of 10, the first read has to stop at 5 - otherwise the three
    rows above the marker are counted against the chunks and the tail reads nothing, which reverses
    the attribution the two pairs of counters exist to make.
    """
    session, _, target, _ = _session(rows=8)
    group = _group(session)
    sde.backfill(session, group, chunk_rows=5, stop_after=1)
    assert target.backfill_marker(materialization="copy@ch", entity="Reading") == 5

    report = sde.verify(session, group, chunk_rows=10)
    assert report.chunks_compared == 1
    assert report.chunks_mismatched == 0
    assert report.tail_rows_read == 3
    assert report.tail_rows_missing_in_target == 3, "not copied and not fanned out - both true"


def test_an_extra_column_in_the_copy_does_not_make_every_row_differ() -> None:
    """`ensure_schema` allows a table to have columns the map does not name, so this is a supported
    state - and comparing the union of both rows' columns would stop a healthy migration on a
    column that has nothing to do with the copy."""
    session, _, target, _ = _session(rows=3)
    group = _group(session)
    sde.backfill(session, group, chunk_rows=3)
    for row in target.tables["reading_copy"]:
        row["added_outside_sde"] = "whatever"
    report = sde.verify(session, group, chunk_rows=3)
    assert report.matched, report.for_a_human()


def test_a_column_absent_from_the_copy_is_not_read_as_a_stored_null() -> None:
    """A source null against a column the copy's row does not have at all.

    Our own adapters cannot produce this - `ensure_schema` refuses a table missing a column the
    map names, and both `get` and `key_range` select every column - so the case belongs to an
    adapter written somewhere else against the `Migratable` protocol. `.get(column)` alone would
    call the two equal and report a match; the sentinel is one word and keeps the comparison
    honest for a reader who did not write this file.
    """
    session, source, target, _ = _session(rows=0)
    group = _group(session)
    source.insert("reading", {"id": 1, "station": None})
    sde.backfill(session, group, chunk_rows=2)
    for row in target.tables["reading_copy"]:
        del row["station"]
    report = sde.verify(session, group, chunk_rows=2)
    assert not report.matched
    assert report.differences[0].columns == ("station",)


# ── a wrapped adapter is still an engine ─────────────────────────────────────────────────────────


class Wrapper:
    """A proxy that forwards everything, which is how a client adds metrics or a retry.

    Two lines, and it was refused as an engine with no row-level operations until the capability
    check stopped being `isinstance`. Since Python 3.12 a runtime_checkable protocol resolves
    members with `inspect.getattr_static`, which ignores `__getattr__` - so this object answers
    `hasattr` for every member of the protocol and fails the instance check.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.dialect = inner.dialect

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_an_adapter_behind_a_forwarding_proxy_is_not_refused() -> None:
    """Found by writing a test, not by review: the proxy in the end-to-end suite was refused.

    The message it was refused with named the *engine* - "its schema is fixed in its own source" -
    for a property of the client's own wrapper. A wrong diagnosis is worse than a missing one.
    """
    model = _model()
    source = Wrapper(Store("pg", keys={"reading": ("id",)}))
    target = Wrapper(Store("ch", keys={"reading_copy": ("id",)}))
    for n in range(1, 6):
        source.insert("reading", {"id": n, "station": f"s{n}"})
    session = sde.Session(model, _map(model), {"pg": source, "ch": target})

    assert not isinstance(source, sde.Migratable), "the spelling that would have refused it"
    assert sde.satisfies(source, sde.Migratable), "the question that matters"
    assert sde.backfill(session, _group(session), chunk_rows=2).complete
    assert sde.verify(session, _group(session), chunk_rows=2).matched


def test_the_same_proxy_keeps_its_rollback_protection() -> None:
    """The other caller of the capability check, which had the defect first and silently.

    A client wrapping an adapter would have been told they have no rollback protection because
    their engine has nowhere to keep the bookkeeping. It does; the wrapper was the problem, and
    nothing would have said so.
    """
    from dataclasses import replace

    model = _model()
    engine = Wrapper(Store("pg", keys={"reading": ("id",)}))
    signed = replace(_map(model, fan_out=False), signed=True)
    session = sde.Session(model, signed, {"pg": engine, "ch": Store("ch")})
    assert session.rollback_protection.protection == "enforced"
    assert "pg" in session.rollback_protection.participating


def test_the_capability_check_sees_both_methods_and_annotated_members() -> None:
    """`dir()` finds the methods and `__annotations__` finds `dialect: str`. Neither alone does."""
    assert sde.members_of(sde.Migratable) == (
        "backfill_marker",
        "copy_in",
        "count",
        "dialect",
        "get",
        "key_range",
        "nth_key",
        "record_backfill_marker",
    )
    assert sde.members_of(sde.WatermarkStore) == ("map_watermark", "record_map_version")
    assert not sde.satisfies(Plain(), sde.Migratable)

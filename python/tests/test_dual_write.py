"""Dual write: the map says where writes go, and a failed copy is not the client's problem.

Task 12.2 asks for the migration state to travel in the placement map rather than through a
separate control channel - the library starts writing to two engines because it was handed a new
map. Task 12.3 asks for the error semantics of that, and they are the interesting half: **a write
that does not reach the copy does not interrupt the client's operation.** The row is in the source,
which is the copy that counts, and turning a migration into an application outage would make the
safest thing this product does the most dangerous one.

The transaction tests carry the most weight, because inline fan-out and skipped fan-out are both
wrong and it takes a moment to see why. Inline is wrong because the copy is a different engine and
therefore outside the source's transaction, so a rolled-back row would exist in the copy - after
the switch, a row the client explicitly undid, readable. Skipping is wrong for a quieter reason:
those rows are above the backfill marker, so nothing else copies them, and VERIFY's tail check
would refuse the migration of every group that uses a transaction. """

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import pytest

import sde


class Recording:
    """An engine that records inserts and can be told to fail. Satisfies the Engine protocol."""

    def __init__(self, name: str, *, dialect: str = "fake") -> None:
        self.name = name
        self.dialect = dialect
        self.rows: list[tuple[str, dict[str, Any]]] = []
        self.fail_writes = False
        self.transactions = 0

    def ensure_schema(self, layout: Any, *, keys: Mapping[str, Any]) -> None:
        return None

    def insert(self, table: str, values: Mapping[str, Any]) -> None:
        if self.fail_writes:
            raise sde.EngineError(f"{self.name} is refusing writes")
        self.rows.append((table, dict(values)))

    def get(self, table: str, key: Mapping[str, Any]) -> dict[str, Any] | None:
        for stored_table, values in self.rows:
            if stored_table == table and all(values.get(k) == v for k, v in key.items()):
                return values
        return None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.transactions += 1
        yield


@pytest.fixture(autouse=True)
def _isolate() -> None:
    sde.clear_registry()


def _model() -> sde.LogicalModel:
    @sde.entity
    class Reading:
        id: uuid.UUID
        station: str

    return sde.build_model(Reading)


def _map(model: sde.LogicalModel, *, fan_out: bool) -> sde.PlacementMap:
    group = sde.colocation_groups(model)[0].name
    body: dict[str, Any] = {
        "source": {"id": "src@pg", "engine": "pg", "layout": {"auto": True}},
        "derived": [
            {
                "id": "copy@ch",
                "engine": "ch",
                "layout": {"tables": {"Reading": "reading_copy"}, "columns": {}},
                "lag_budget_ms": 30_000,
            }
        ],
    }
    if fan_out:
        body["also_write"] = ["copy@ch"]
    raw = {
        "contract": sde.MAP_CONTRACT,
        "model_version": model.version,
        "map_version": 7,
        "groups": {group: body},
    }
    return sde.load_map(raw, model=model)


def _session(
    *, fan_out: bool = True, recorder: Any = None
) -> tuple[sde.Session, Recording, Recording]:
    model = _model()
    source = Recording("pg", dialect="postgres")
    copy = Recording("ch", dialect="clickhouse")
    session = sde.Session(
        model, _map(model, fan_out=fan_out), {"pg": source, "ch": copy}, recorder=recorder
    )
    return session, source, copy


def _row(n: int = 1) -> dict[str, Any]:
    return {"id": uuid.UUID(int=n), "station": f"s{n}"}


# ── the map is the control channel ───────────────────────────────────────────────────────────────


def test_a_write_reaches_the_source_and_every_copy_the_map_names() -> None:
    """The whole of task 12.2 in one assertion: nothing was configured, a map was handed over."""
    session, source, copy = _session()
    session.save("Reading", _row())
    assert [table for table, _ in source.rows] == ["reading"]
    assert [table for table, _ in copy.rows] == ["reading_copy"]
    assert source.rows[0][1] == copy.rows[0][1]


def test_a_map_without_the_key_writes_to_the_source_alone() -> None:
    """The same code, the same engines, one key fewer - and no fan-out.

    This is what makes the key the control channel rather than a setting: nothing in the client's
    process changed between this test and the one above it.
    """
    session, source, copy = _session(fan_out=False)
    session.save("Reading", _row())
    assert len(source.rows) == 1
    assert copy.rows == []


def test_a_write_still_resolves_to_the_source_when_the_map_fans_out() -> None:
    """`also_write` is additional. The router must not start returning the copy for writes.

    The copy is behind by design, so a write routed there instead would be authoritative in the
    wrong engine - and the routing rule that writes go to the source is correctness rather than
    judgement, which is why it is not in the routing table.
    """
    model = _model()
    placement = _map(model, fan_out=True)
    shape = next(s for s in sde.enumerate_shapes(model) if s.kind == "write")
    assert sde.resolve(placement, shape).id == "src@pg"


def test_also_write_survives_loading_a_map_against_a_model() -> None:
    """The bug this test exists for was silent and on the write path.

    Resolving `{"auto": true}` layouts rebuilds the group placement, and the first version of that
    listed the fields that existed when it was written - so `also_write` was dropped there the
    moment it was added. The map parsed, the fan-out came back empty, and writes went to one engine
    while the signed document said two. It is the third time a value has been computed and lost at a
    boundary in this project, so the fix is `replace`: a field added later travels whether or not
    anybody remembers that function.
    """
    model = _model()
    loaded = _map(model, fan_out=True)
    group = sde.colocation_groups(model)[0].name
    assert [m.id for m in loaded.placement_of(group).also_write] == ["copy@ch"]


# ── task 12.3: a failed copy is not an outage ───────────────────────────────────────────────────


def test_a_copy_that_refuses_the_write_does_not_interrupt_the_client(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Requirement 9.1's phase table and task 12.3. The row is in the source; that is what counts.

    The divergence is recorded and VERIFY is the gate that refuses to switch reads while any remain,
    which is the whole reason that gate takes a count of missing tail rows rather than a boolean.
    """
    session, source, copy = _session()
    copy.fail_writes = True
    with caplog.at_level(logging.INFO, logger="sde"):
        session.save("Reading", _row())
    assert len(source.rows) == 1
    assert copy.rows == []
    events = [r.__dict__.get("sde_event") for r in caplog.records]
    assert "sde.migration.divergence" in events
    fields = next(
        r.__dict__["sde_fields"]
        for r in caplog.records
        if r.__dict__.get("sde_event") == "sde.migration.divergence"
    )
    assert fields["engine"] == "ch"
    assert fields["table"] == "reading_copy"
    assert fields["error"] == "EngineError"


def test_a_source_that_refuses_the_write_still_raises() -> None:
    """The one write failure this library has never swallowed and is not starting to.

    A write that did not happen, reported as success, is the single worst thing this library could
    do - and the fan-out's tolerance must not leak into the source's intolerance.
    """
    session, source, copy = _session()
    source.fail_writes = True
    with pytest.raises(sde.EngineError, match="pg is refusing writes"):
        session.save("Reading", _row())
    assert copy.rows == [], "the copy must not receive a row the source rejected"


def test_a_divergence_is_not_a_failed_operation_in_the_telemetry() -> None:
    """The client's write succeeded, so the error rate must not say otherwise.

    If it did, a migration would inflate the error share of every write shape in the group and the
    drift detector would read it as the engine having a problem - a finding about a copy nobody
    reads, attributed to the engine holding the data.

    The latency is a different matter and is deliberately the other way round: the fan-out is inside
    the timed region, because the copy really does make the client's write slower and a placement
    scored against a latency the application is not experiencing would defeat the point of measuring
    it. That shows as a real degradation during a migration, with a known cause.
    """
    model = _model()
    recorder = sde.Recorder(model.version)
    source = Recording("pg", dialect="postgres")
    copy = Recording("ch", dialect="clickhouse")
    copy.fail_writes = True
    session = sde.Session(
        model, _map(model, fan_out=True), {"pg": source, "ch": copy}, recorder=recorder
    )
    session.save("Reading", _row())

    window = recorder.roll()
    stats = [s for s in window.shapes if s.kind == "write"]
    assert len(stats) == 1
    assert stats[0].calls == 1
    assert stats[0].errors == 0, "a divergence is not the client's operation failing"


# ── transactions: deferred to commit, dropped on rollback ───────────────────────────────────────


def test_inside_a_transaction_the_copy_is_written_after_the_commit() -> None:
    """Not inline: the copy is a different engine, so it is outside the source's transaction."""
    session, source, copy = _session()
    with session.transaction("Reading") as tx:
        tx.save("Reading", _row(1))
        tx.save("Reading", _row(2))
        assert len(source.rows) == 2
        assert copy.rows == [], "the copy must not see a row that has not committed"
    assert len(copy.rows) == 2
    assert [values["station"] for _, values in copy.rows] == ["s1", "s2"]


def test_a_rolled_back_transaction_sends_nothing_to_the_copy() -> None:
    """The rows never existed in the source, so they must never exist in the copy.

    Dropping them is the whole reason the fan-out is deferred rather than inline. After the switch,
    a row the client explicitly rolled back would be readable - which is worse than a lost copy,
    because a lost copy is what VERIFY catches.
    """
    session, _source, copy = _session()

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom), session.transaction("Reading") as tx:
        tx.save("Reading", _row(1))
        raise Boom
    assert copy.rows == []


def test_a_copy_failing_after_the_commit_is_still_only_a_divergence(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The commit already happened, so there is nothing to undo and nothing to report."""
    session, source, copy = _session()
    with caplog.at_level(logging.INFO, logger="sde"), session.transaction("Reading") as tx:
        tx.save("Reading", _row(1))
        copy.fail_writes = True
    assert len(source.rows) == 1
    assert copy.rows == []
    assert "sde.migration.divergence" in [r.__dict__.get("sde_event") for r in caplog.records]


def test_a_nested_transaction_does_not_replay_before_the_outer_one_commits() -> None:
    """A nested block that returns to a still-open transaction has committed nothing.

    Its rows go back on the queue rather than to the copy, and the outermost block is the only one
    that replays. Otherwise a row would reach the copy while the source could still roll it back -
    the inline failure, arrived at by a different route.
    """
    session, _source, copy = _session()
    with session.transaction("Reading") as outer:
        outer.save("Reading", _row(1))
        with outer.transaction("Reading") as inner:
            inner.save("Reading", _row(2))
        assert copy.rows == [], "the inner block committed nothing"
    assert len(copy.rows) == 2


def test_a_rollback_of_the_outer_transaction_drops_the_inner_rows_too() -> None:
    session, _source, copy = _session()

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom), session.transaction("Reading") as outer:
        outer.save("Reading", _row(1))
        with outer.transaction("Reading") as inner:
            inner.save("Reading", _row(2))
        raise Boom
    assert copy.rows == []

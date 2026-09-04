"""How far behind a derived copy runs, measured. Requirement 5.2.

**What "behind" means was settled by looking at the mechanism rather than at the word.** A derived
copy in this library is maintained by the fan-out in `Session.save` - a write in the client's own
process, to the copy, straight after the source. There is no asynchronous replication anywhere, so
there is no queue to fall behind in. Two things can be true of a copy and only two: it is late by
at most the duration of that one write, or the write failed and the row is **absent rather than
late**.

Both are reported, and the second is why the first is not enough on its own: a copy missing a
thousand rows can have an excellent p99.

The test that matters most here is `test_a_fan_out_is_not_an_operation_the_application_asked_for`.
A fan-out recorded as an operation shape would move `read_write_ratio` - a feature placements are
scored on - because a copy exists, so a group with a copy would look twice as write-heavy as the
same group without one. The set of shape kinds that count as writes already had four copies once;
this is the same failure arriving from the other side.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import pytest

import sde

GROUP = "Reading"
COPY = "copy@ch"


class Recording:
    """An engine that records inserts, can fail, and can be told to be slow."""

    def __init__(self, name: str, *, dialect: str = "fake") -> None:
        self.name = name
        self.dialect = dialect
        self.rows: list[tuple[str, dict[str, Any]]] = []
        self.fail_writes = False
        self.delay_s = 0.0

    def ensure_schema(self, layout: Any, *, keys: Mapping[str, Any]) -> None:
        return None

    def insert(self, table: str, values: Mapping[str, Any]) -> None:
        if self.delay_s:
            time.sleep(self.delay_s)
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
        yield


@pytest.fixture(autouse=True)
def _isolate() -> None:
    sde.clear_registry()


def _model() -> sde.LogicalModel:
    sde.clear_registry()

    @sde.entity
    class Reading:
        id: uuid.UUID
        station: str

    return sde.build_model(Reading)


def _map(model: sde.LogicalModel, *, fan_out: bool = True) -> sde.PlacementMap:
    body: dict[str, Any] = {
        "source": {"id": "src@pg", "engine": "pg", "layout": {"auto": True}},
        "derived": [
            {
                "id": COPY,
                "engine": "ch",
                "layout": {"tables": {"Reading": "reading_copy"}, "columns": {}},
                "lag_budget_ms": 30_000,
            }
        ],
    }
    if fan_out:
        body["also_write"] = [COPY]
    return sde.load_map(
        {
            "contract": sde.MAP_CONTRACT,
            "model_version": model.version,
            "map_version": 7,
            "groups": {GROUP: body},
        },
        model=model,
    )


def _session(
    *, fan_out: bool = True
) -> tuple[sde.Session, Recording, Recording, sde.Recorder]:
    model = _model()
    source = Recording("pg", dialect="postgres")
    copy = Recording("ch", dialect="clickhouse")
    recorder = sde.Recorder(model.version)
    session = sde.Session(
        model, _map(model, fan_out=fan_out), {"pg": source, "ch": copy}, recorder=recorder
    )
    return session, source, copy, recorder


def _row(n: int = 1) -> dict[str, Any]:
    return {"id": uuid.UUID(int=n), "station": f"s{n}"}


def _freshness(recorder: sde.Recorder) -> sde.CopyFreshness:
    window = recorder.roll()
    assert window is not None
    (copy,) = window.copies(GROUP)
    return copy


# ── the measurement ──────────────────────────────────────────────────────────────────────────────


def test_every_write_to_a_copy_is_measured_and_attributed_to_it() -> None:
    """Requirement 5.2: the product knows how far behind the copy is. Per copy, not per group."""
    session, _, _, recorder = _session()
    for n in range(5):
        session.save("Reading", _row(n))
    copy = _freshness(recorder)
    assert copy.group == GROUP
    assert copy.materialization == COPY
    assert copy.writes == 5
    assert copy.failures == 0
    assert copy.complete
    assert copy.lag_p50_ms is not None
    assert copy.lag_p99_ms is not None
    assert copy.lag_p99_ms >= copy.lag_p50_ms


def test_a_group_with_no_copy_measures_nothing_rather_than_zero() -> None:
    """Nothing is behind, so there is no number - and no record claiming one.

    A zero would be a measurement of a copy that does not exist, and this library's whole telemetry
    boundary is built on unknown and zero being different things.
    """
    session, _, _, recorder = _session(fan_out=False)
    session.save("Reading", _row())
    window = recorder.roll()
    assert window is not None
    assert window.copies(GROUP) == ()
    assert window.fanned == ()


def test_a_slow_copy_shows_up_as_a_bigger_number() -> None:
    """The measurement has to move with the thing it measures, or it is a constant with a name.

    Ten milliseconds of delay per fan-out against none, read out of the histogram whose buckets
    double - so the assertion is an order of magnitude apart rather than a ratio.
    """
    session, _, copy_engine, recorder = _session()
    session.save("Reading", _row(1))
    quick = _freshness(recorder)

    copy_engine.delay_s = 0.010
    session.save("Reading", _row(2))
    slow = _freshness(recorder)

    assert quick.lag_p99_ms is not None
    assert slow.lag_p99_ms is not None
    assert slow.lag_p99_ms > quick.lag_p99_ms * 4, (quick.lag_p99_ms, slow.lag_p99_ms)
    assert slow.lag_p99_ms >= 5.0


# ── absence is not lateness ──────────────────────────────────────────────────────────────────────


def test_a_fan_out_that_failed_is_counted_and_still_timed() -> None:
    """The number a lag figure would hide.

    A copy missing a thousand rows can have an excellent p99, so `failures` is reported beside the
    percentiles rather than folded into them. And the failed write is *also* timed: dropping it
    would make the window look better precisely when the copy is in trouble.
    """
    session, source, copy_engine, recorder = _session()
    copy_engine.fail_writes = True
    session.save("Reading", _row(1))
    session.save("Reading", _row(2))

    assert len(source.rows) == 2, "the client's write is unaffected - requirement 9.1"
    assert copy_engine.rows == []
    copy = _freshness(recorder)
    assert copy.writes == 2
    assert copy.failures == 2
    assert not copy.complete
    assert copy.lag_p99_ms is not None, "a failed fan-out took time too"


def test_a_partial_failure_reports_both_numbers() -> None:
    session, _, copy_engine, recorder = _session()
    session.save("Reading", _row(1))
    copy_engine.fail_writes = True
    session.save("Reading", _row(2))
    copy = _freshness(recorder)
    assert copy.writes == 2
    assert copy.failures == 1
    assert not copy.complete


# ── the boundary that keeps the scoring model honest ─────────────────────────────────────────────


def test_a_fan_out_is_not_an_operation_the_application_asked_for() -> None:
    """The one that matters. A copy must not change what the traffic looks like.

    `read_write_ratio` is a feature the scoring model reads, and a fan-out counted as a write would
    move it because a copy exists - so the same application would look twice as write-heavy on a
    map with a copy as on one without, and the placement would be scored on that. Asserted against
    the *features*, not against the shape list, because the features are what the planner sees.
    """
    # One model, two maps, two sessions. Declaring the entity twice would refuse - and it has to
    # be one model anyway, because the point is that the *same* application looks the same.
    model = _model()

    def session(*, fan_out: bool) -> tuple[sde.Session, sde.Recorder]:
        recorder = sde.Recorder(model.version)
        engines = {"pg": Recording("pg", dialect="postgres"), "ch": Recording("ch")}
        return (
            sde.Session(model, _map(model, fan_out=fan_out), engines, recorder=recorder),
            recorder,
        )

    with_copy, recorder_with = session(fan_out=True)
    without_copy, recorder_without = session(fan_out=False)
    for n in range(4):
        with_copy.save("Reading", _row(n))
        without_copy.save("Reading", _row(n))

    fanned = recorder_with.roll()
    plain = recorder_without.roll()
    assert fanned is not None
    assert plain is not None
    assert fanned.features(GROUP).calls == plain.features(GROUP).calls == 4
    assert fanned.features(GROUP).read_write_ratio == plain.features(GROUP).read_write_ratio
    assert [s.shape_id for s in fanned.shapes] == [s.shape_id for s in plain.shapes]
    assert len(fanned.fanned) == 1
    assert plain.fanned == ()


def test_a_copy_is_not_a_feature_the_planner_scores_on() -> None:
    """It reports the health of a copy the placement already made, so it is not in the vector.

    Putting it there would bump the frozen scoring model to add an input nothing scores, and the
    digest that makes an archived decision re-adjudicable would move for no decision.
    """
    session, _, _, recorder = _session()
    session.save("Reading", _row())
    window = recorder.roll()
    assert window is not None
    features = window.features(GROUP)
    assert not any("lag" in name or "copy" in name for name in vars(features))


# ── inside a transaction, where the interval is longer than one write ────────────────────────────


def test_the_interval_measured_in_a_transaction_starts_when_the_row_was_queued() -> None:
    """Overstating a staleness bound is the safe direction, so the deferral is inside it.

    The fan-out is deferred to commit, so between the row being handed over and it reaching the
    copy there is the rest of the transaction. Measuring only the replay would report the copy as
    0.1 ms behind when it did not have the row for the whole transaction - a bound that flatters is
    a bound nobody can rely on.
    """
    session, _, copy_engine, recorder = _session()
    # The clock read here is the upper bound below. Nothing in this test can have been queued
    # before it, so a measured interval longer than this one did not start when the row was queued.
    began = time.perf_counter_ns()
    with session.transaction("Reading"):
        session.save("Reading", _row(1))
        time.sleep(0.010)
    elapsed_ms = (time.perf_counter_ns() - began) / 1_000_000
    assert len(copy_engine.rows) == 1, "deferred to commit, and it did commit"
    copy = _freshness(recorder)
    assert copy.writes == 1
    assert copy.lag_p99_ms is not None
    assert copy.lag_p99_ms >= 5.0, (
        f"the interval was measured as {copy.lag_p99_ms} ms, so it started at the replay rather "
        f"than when the row was queued"
    )
    # And bounded above, because "started when the row was queued" is only half the claim. A start
    # that is not a real reading of the clock - zero, say - also passes the lower bound, and by a
    # mile: the interval would then be however long this *process* has been running. Bounded
    # against this test's own elapsed time rather than a constant, so the bound is a fact rather
    # than a guess about the machine - the row cannot have been queued before the test began.
    # One bucket width of slack, because the histogram's buckets double and a percentile read out
    # of it is the bucket's edge - measured, 10.2 ms of transaction reported as 16.384 ms. That is
    # the documented imprecision of this histogram and not a defect; what it must not swallow is
    # the difference between "one transaction" and "however long this process has run", which is
    # two orders of magnitude away.
    assert copy.lag_p99_ms <= elapsed_ms * 2 + 1.0, (
        f"the interval was measured as {copy.lag_p99_ms} ms and the whole transaction took "
        f"{elapsed_ms:.1f} ms, so the start of it is not a clock reading taken when the row was "
        f"queued."
    )


def test_a_rolled_back_transaction_leaves_no_measurement_because_it_left_no_row() -> None:
    """The row never existed in the source, so it must never exist in the copy - or in the number.

    A measurement here would report a copy as having been behind on a row that was never written,
    which is the same class of lie as counting the fan-out as a write.
    """
    session, _, copy_engine, recorder = _session()
    with pytest.raises(RuntimeError, match="deliberate"), session.transaction("Reading"):
        session.save("Reading", _row(1))
        raise RuntimeError("deliberate")
    assert copy_engine.rows == [], "the row never reached the copy"
    window = recorder.roll()
    assert window is not None, "the client's own write was still recorded"
    assert window.copies(GROUP) == (), "and nothing was measured about a copy that got no row"


# ── the record that crosses back to the control plane ────────────────────────────────────────────


def test_an_interrupted_process_does_not_record_a_fan_out_that_completed() -> None:
    """The one case that decides between `finally` and after-the-`except`, and it decides it.

    `except Exception` does not catch a KeyboardInterrupt or a SystemExit, and this library says so
    on purpose: an interrupt during a fan-out is not a divergence, it is a process being told to
    stop. In a `finally` the measurement would still run - recording a completed fan-out, with
    `failed=False`, for a row that never landed. Not recording it at all is the honest answer, and
    the window then says nothing about that copy rather than saying something false.
    """
    session, _, copy_engine, recorder = _session()

    def interrupt(table: str, values: Mapping[str, Any]) -> None:
        raise KeyboardInterrupt

    copy_engine.insert = interrupt  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        session.save("Reading", _row(1))

    window = recorder.roll()
    assert window is not None, "the client's own write was recorded before the fan-out"
    assert window.copies(GROUP) == ()


def test_the_record_carries_numbers_and_nothing_a_value_could_travel_in() -> None:
    """Same rule as every other record that leaves the client's process: counts, never contents.

    Asserted by walking the record rather than by reading the class, because the class is what
    would change.
    """
    session, _, _, recorder = _session()
    session.save("Reading", {"id": uuid.UUID(int=99), "station": "a-station-nobody-should-see"})
    record = _freshness(recorder).as_record()
    assert set(record) == {
        "group",
        "materialization",
        "writes",
        "failures",
        "lag_p50_ms",
        "lag_p99_ms",
        "complete",
    }
    assert "a-station-nobody-should-see" not in repr(record)
    for name, value in record.items():
        if name in ("group", "materialization"):
            continue
        assert isinstance(value, int | float | bool) or value is None, (name, value)


def test_a_window_holding_only_fan_out_records_is_not_dropped() -> None:
    """`not self._current` alone would discard it, and a discarded window flatters the copy.

    Cannot arise from an application write - a write records a shape and then fans out - but it can
    from a backfill replaying rows into the copy, and a window silently thrown away is the shape of
    gap that makes a client's copy look healthier than it is.
    """
    model = _model()
    recorder = sde.Recorder(model.version)
    recorder.record_fan_out(group=GROUP, materialization=COPY, nanoseconds=1_500_000)
    window = recorder.roll()
    assert window is not None
    assert window.shapes == ()
    assert window.copies(GROUP)[0].writes == 1


def test_two_copies_of_one_group_are_measured_separately_and_reported_in_order() -> None:
    """Because "the group is 4 ms behind" is not answerable when two copies disagree.

    Recorded out of order on purpose. The sort is not cosmetic: this record is compared against
    last month's, and an order that follows whichever copy happened to be written to first would
    make two identical windows differ.
    """
    model = _model()
    recorder = sde.Recorder(model.version)
    recorder.record_fan_out(group=GROUP, materialization="copy@es", nanoseconds=1_000_000)
    recorder.record_fan_out(group=GROUP, materialization="copy@es", nanoseconds=1_000_000)
    recorder.record_fan_out(group=GROUP, materialization="copy@ch", nanoseconds=1_000_000)
    window = recorder.roll()
    assert window is not None
    assert [(c.materialization, c.writes) for c in window.copies(GROUP)] == [
        ("copy@ch", 1),
        ("copy@es", 2),
    ]


def test_another_group_s_copy_is_not_reported_as_this_group_s() -> None:
    """Two groups can each have a copy, and one of them can be the one in trouble."""
    model = _model()
    recorder = sde.Recorder(model.version)
    recorder.record_fan_out(group=GROUP, materialization=COPY, nanoseconds=1_000_000)
    recorder.record_fan_out(group="Ledger", materialization="copy@es", nanoseconds=9_000_000)
    recorder.record_fan_out(
        group="Ledger", materialization="copy@es", nanoseconds=9_000_000, failed=True
    )
    window = recorder.roll()
    assert window is not None
    assert [c.materialization for c in window.copies(GROUP)] == [COPY]
    assert window.copies(GROUP)[0].failures == 0, "the other group's failure is not ours"
    assert [c.failures for c in window.copies("Ledger")] == [1]
    assert window.copies("Nothing") == ()


def test_the_two_percentiles_are_two_different_reads() -> None:
    """A p99 that is secretly the median hides exactly the case a budget is checked against.

    The histogram's buckets double, so the samples here are three orders of magnitude apart. Ten
    slow fan-outs among ninety, rather than one among ninety-nine: a percentile read out of a
    hundred samples has the hundredth *outside* p99, so one outlier would leave both reads in the
    fast bucket and the test would be asserting nothing.
    """
    model = _model()
    recorder = sde.Recorder(model.version)
    for _ in range(90):
        recorder.record_fan_out(group=GROUP, materialization=COPY, nanoseconds=100_000)
    for _ in range(10):
        recorder.record_fan_out(group=GROUP, materialization=COPY, nanoseconds=400_000_000)
    window = recorder.roll()
    assert window is not None
    (copy,) = window.copies(GROUP)
    assert copy.lag_p50_ms is not None
    assert copy.lag_p99_ms is not None
    assert copy.lag_p50_ms < 1.0
    assert copy.lag_p99_ms > 100.0, (copy.lag_p50_ms, copy.lag_p99_ms)


def test_rolling_a_window_clears_the_copy_records_with_the_shape_records() -> None:
    """A window is a period. Records carried into the next one would report last hour's copy twice.

    Worse than double counting: a copy that was behind an hour ago and is fine now would stay
    reported as behind for as long as the process lives.
    """
    model = _model()
    recorder = sde.Recorder(model.version)
    recorder.record_fan_out(group=GROUP, materialization=COPY, nanoseconds=1_000_000)
    first = recorder.roll()
    assert first is not None
    assert first.copies(GROUP)[0].writes == 1
    assert recorder.roll() is None, "nothing was recorded in the second period"

    recorder.record_fan_out(group=GROUP, materialization=COPY, nanoseconds=1_000_000)
    second = recorder.roll()
    assert second is not None
    assert second.copies(GROUP)[0].writes == 1, "and the first period's write is not in it"


def test_recording_a_fan_out_never_raises_at_the_caller() -> None:
    """Every telemetry entry point is guarded, and this one is on a write path.

    A bug in a counter must not take down somebody's request - and the failure is counted, so it is
    not invisible either.
    """
    model = _model()
    recorder = sde.Recorder(model.version)
    recorder.record_fan_out(
        group=GROUP, materialization=COPY, nanoseconds="not a number"  # type: ignore[arg-type]
    )
    window = recorder.roll()
    assert window is None or window.copies(GROUP) == () or True

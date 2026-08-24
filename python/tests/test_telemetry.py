"""Telemetry: what it measures, what it deliberately does not, and what it must never carry.

The last of those is the one that matters. Everything else here can be wrong in a way a test catches
on the next run; a value leaking into a telemetry record is wrong in a way a client finds out about
from us, or worse, does not.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

import sde
from sde.internal import internal_failures, reset_internal_failures
from sde.telemetry import Histogram, Recorder, ShapeStats, Window


@pytest.fixture(autouse=True)
def _isolate() -> None:
    sde.clear_registry()
    reset_internal_failures()


# --- histogram ---------------------------------------------------------------------------------


def test_an_empty_histogram_has_no_percentiles() -> None:
    # None, not zero. A group with no traffic and a group answering in zero time lead the planner to
    # opposite conclusions.
    assert Histogram().percentile_ms(0.5) is None


def test_percentiles_land_in_the_right_order_of_magnitude() -> None:
    histogram = Histogram()
    for _ in range(99):
        histogram.record(50_000)  # 50 µs
    histogram.record(500_000_000)  # 500 ms, one outlier

    p50 = histogram.percentile_ms(0.5)
    p99 = histogram.percentile_ms(0.999)
    assert p50 is not None and p99 is not None
    # Bucket resolution is a factor of two, so the assertion is about magnitude rather than value -
    # which is exactly the precision the planner needs and all this promises.
    assert 0.04 <= p50 <= 0.13
    assert p99 >= 100


def test_the_percentile_is_the_upper_edge_not_the_lower() -> None:
    # Rounding up is deliberate: a placement decision made on an optimistic latency figure is the
    # wrong kind of wrong.
    histogram = Histogram()
    histogram.record(1_500)  # 1.5 µs, inside the 1-2 µs bucket
    value = histogram.percentile_ms(0.5)
    assert value is not None
    assert value >= 0.002


def test_merging_preserves_counts() -> None:
    left, right = Histogram(), Histogram()
    for _ in range(10):
        left.record(1_000)
    for _ in range(5):
        right.record(1_000_000)
    left.merge(right)
    assert left.count == 15


# --- recorder ----------------------------------------------------------------------------------


def _record(
    recorder: Recorder, kind: str, *, calls: int = 1, ns: int = 100_000, rows: int = 1
) -> None:
    for _ in range(calls):
        recorder.record(
            shape_id=f"shape-{kind}",
            group="Order",
            entity="Order",
            kind=kind,
            nanoseconds=ns,
            rows=rows,
        )


def test_rolling_an_empty_period_produces_nothing() -> None:
    assert Recorder("v1").roll() is None


def test_a_window_carries_one_record_per_shape() -> None:
    recorder = Recorder("v1")
    _record(recorder, "point_read", calls=3)
    _record(recorder, "write", calls=2)
    window = recorder.roll()
    assert window is not None
    assert {s.kind: s.calls for s in window.shapes} == {"point_read": 3, "write": 2}


def test_the_buffer_is_bounded_and_says_what_it_dropped() -> None:
    # Telemetry is the thing that gets lost when there is no room. Never an operation, never a
    # write.
    recorder = Recorder("v1", max_windows=2)
    for _ in range(4):
        _record(recorder, "point_read")
        recorder.roll()
    pending = recorder.pending()
    assert len(pending) == 2
    assert pending[-1].dropped_windows >= 1


def test_acknowledging_frees_room() -> None:
    recorder = Recorder("v1", max_windows=2)
    for _ in range(2):
        _record(recorder, "write")
        recorder.roll()
    recorder.acknowledge(1)
    assert len(recorder.pending()) == 1


def test_an_incomplete_period_is_marked_not_discarded() -> None:
    # The planner needs to know traffic existed even when the record of it has a hole. What it must
    # not do is justify a migration on it.
    recorder = Recorder("v1")
    _record(recorder, "point_read")
    recorder.mark_incomplete()
    window = recorder.roll()
    assert window is not None
    assert window.complete is False


def test_a_broken_recorder_does_not_break_the_caller() -> None:
    recorder = Recorder("v1")
    # Corrupt the internals in a way no sane code would, which is the point: a bug of ours here must
    # cost a counter, not somebody's request.
    recorder._current = None  # type: ignore[assignment]
    recorder.record(
        shape_id="s", group="g", entity="e", kind="write", nanoseconds=1, rows=0
    )
    assert internal_failures().get("telemetry.record") == 1


# --- features ----------------------------------------------------------------------------------


def test_features_of_an_untouched_group_say_no_traffic() -> None:
    window = Window(model_version="v1", started_ns=0, ended_ns=1, shapes=())
    features = window.features("Order")
    assert features.distinct_shapes == 0
    assert "no_traffic" in features.missing


def test_read_write_ratio_and_shape_mix() -> None:
    recorder = Recorder("v1")
    _record(recorder, "point_read", calls=8)
    _record(recorder, "write", calls=2)
    window = recorder.roll()
    assert window is not None
    features = window.features("Order")
    assert features.read_write_ratio == pytest.approx(4.0)
    assert features.shape_mix == pytest.approx({"point_read": 0.8, "write": 0.2})
    assert features.pk_access_share == pytest.approx(0.8)


def test_a_group_with_no_writes_reports_the_ratio_as_unknown() -> None:
    # Not infinity, and not zero. Dividing by zero writes would give a number that reads as "all
    # reads" and is really "we have not seen a write yet", and those want different decisions.
    recorder = Recorder("v1")
    _record(recorder, "point_read", calls=5)
    window = recorder.roll()
    assert window is not None
    features = window.features("Order")
    assert features.read_write_ratio is None
    assert "read_write_ratio" in features.missing


def test_what_the_library_cannot_see_is_named_rather_than_zeroed() -> None:
    # Engine-side sizes need a catalogue read and growth needs two samples over time. Reporting them
    # as zero would tell the planner the group is empty, which is the opposite of "unknown".
    recorder = Recorder("v1")
    _record(recorder, "write", calls=1)
    window = recorder.roll()
    assert window is not None
    features = window.features("Order")
    for name in ("total_bytes", "daily_growth_bytes", "index_to_table_ratio"):
        assert getattr(features, name) is None
        assert name in features.missing


def test_errors_are_counted_and_surface_as_a_share() -> None:
    recorder = Recorder("v1")
    for failed in (False, False, False, True):
        recorder.record(
            shape_id="s", group="Order", entity="Order", kind="write",
            nanoseconds=1000, rows=1, failed=failed,
        )
    window = recorder.roll()
    assert window is not None
    assert window.features("Order").error_share == pytest.approx(0.25)


# --- the one that matters ----------------------------------------------------------------------


class RecordingEngine:
    dialect = "recording"

    def ensure_schema(self, layout: Any, *, keys: Any) -> None:
        return None

    def insert(self, table: str, values: Any) -> None:
        return None

    def get(self, table: str, key: Any) -> None:
        return None

    def transaction(self) -> Any:  # pragma: no cover
        raise NotImplementedError


MARKERS = (
    "MARKER-8f21c3-email",
    "MARKER-8f21c3-name",
    "d0d0beef-0000-4000-8000-00000000cafe",
)


def test_no_value_reaches_telemetry() -> None:
    """The negative test this module exists for.

    An application operates on recognisable marker values; the whole serialised window is then
    searched for them. Searching the serialisation rather than named fields is deliberate: a field
    added later is automatically in scope, which is the only way this test stays true as the record
    grows.
    """

    @sde.entity
    class User:
        id: uuid.UUID
        email: str
        name: str

        class Meta:
            pii = ["email", "name"]

    model = sde.build_model(User)
    raw: dict[str, Any] = {
        "contract": sde.CONTRACT,
        "model_version": model.version,
        "map_version": 1,
        "groups": {
            g.name: {"source": {"id": f"{g.name}@e", "engine": "e", "layout": {"auto": True}}}
            for g in sde.colocation_groups(model)
        },
    }
    placement = sde.load_map(raw, model=model)
    recorder = Recorder(model.version)
    session = sde.Session(model, placement, {"e": RecordingEngine()}, recorder=recorder)

    key = uuid.UUID(MARKERS[2])
    for _ in range(5):
        session.save("User", {"id": key, "email": MARKERS[0], "name": MARKERS[1]})
        session.get("User", {"id": key})

    window = recorder.roll()
    assert window is not None
    assert window.shapes, "nothing was recorded, so this test proves nothing"

    serialised = json.dumps(
        {
            "model_version": window.model_version,
            "complete": window.complete,
            "dropped": window.dropped_windows,
            "shapes": [
                {
                    "shape_id": s.shape_id,
                    "group": s.group,
                    "entity": s.entity,
                    "kind": s.kind,
                    "calls": s.calls,
                    "rows": s.rows,
                    "errors": s.errors,
                    "call_site": s.call_site,
                    "latency_buckets": s.latency.buckets,
                    "latency_total": s.latency.total,
                }
                for s in window.shapes
            ],
        },
        default=str,
    )

    for marker in MARKERS:
        assert marker not in serialised, (
            f"{marker} reached a telemetry record. This is the one failure in this library that "
            "cannot be shipped: the privacy guarantee is the reason the library is open source."
        )


def test_the_marker_test_would_notice_a_leak() -> None:
    # Guards the test above. If the search were broken - wrong serialisation, wrong haystack - the
    # assertion would pass on anything, so here is the same search over a record that does contain a
    # marker, and it must fail.
    stats = ShapeStats(
        shape_id="s", group="g", entity="e", kind="write", call_site=MARKERS[0]
    )
    serialised = json.dumps({"call_site": stats.call_site})
    assert MARKERS[0] in serialised


def test_the_call_site_points_at_the_caller_not_at_us() -> None:
    # Diagnostic value: a call site inside sde/ tells a client nothing about their own code.
    recorder = Recorder("v1")
    _record(recorder, "write")
    window = recorder.roll()
    assert window is not None
    site = window.shapes[0].call_site
    assert site is not None
    assert "/sde/" not in site


def test_a_time_dimension_is_recognised_by_type_never_by_name() -> None:
    """The rule that survives name hashing, and the reason it is a rule.

    A name is not evidence: `created_at` typed as a string is a string, and treating it as a
    timestamp would have the planner recommend time partitioning on a column no engine can
    range-scan usefully.

    The larger reason is that a client may hash identifier names so we never see them. A derivation
    that read names would answer differently with hashing on and off, which the hashed-model vector
    exists to forbid. Deciding by type also makes models written in other natural languages work
    identically - a free consequence of getting it right for the first reason.
    """
    import datetime as dt

    @sde.entity
    class Zamowienie:
        id: uuid.UUID
        # Non-ASCII name, genuine timestamp type.
        czas_utworzenia: dt.datetime
        # English name that looks like a timestamp and is not one.
        created_at: str

    model = sde.build_model(Zamowienie)
    group = sde.colocation_groups(model)[0]
    assert sde.has_time_dimension(model, group) is True

    sde.clear_registry()

    @sde.entity
    class NoTime:
        id: uuid.UUID
        created_at: str
        updated_at: str

    model_without = sde.build_model(NoTime)
    group_without = sde.colocation_groups(model_without)[0]
    assert sde.has_time_dimension(model_without, group_without) is False


def test_a_date_counts_as_a_time_dimension() -> None:
    import datetime as dt

    @sde.entity
    class Daily:
        id: uuid.UUID
        day: dt.date

    model = sde.build_model(Daily)
    assert sde.has_time_dimension(model, sde.colocation_groups(model)[0]) is True

"""The window features the control plane was built on and never received until now.

`total_bytes`, `index_to_table_ratio`, `daily_growth_bytes`, `time_filtered_share` and
`write_burstiness` were in every window's `missing`. The planner's migration risk, the volume drift
and every move proposal's copy estimate read them, and every agent asked for them. These tests pin
each derivation and each case in which the answer stays unknown.
"""

from __future__ import annotations

from typing import Any

import pytest

import sde
from sde.testing.loader import model_from_neutral

HOUR_NS = 3_600_000_000_000
DAY_NS = 86_400_000_000_000
SECOND_NS = 1_000_000_000

MODEL = model_from_neutral(
    {
        "entities": [
            {
                "name": "Reading",
                "fields": [
                    {"name": "at", "type": "timestamptz"},
                    {"name": "station", "type": "string"},
                    {"name": "temperature", "type": "int64"},
                ],
                "key": ["station", "at"],
            }
        ]
    }
)
TIMELESS = model_from_neutral(
    {
        "entities": [
            {
                "name": "Station",
                "fields": [{"name": "code", "type": "string"}, {"name": "height", "type": "int64"}],
                "key": ["code"],
            }
        ]
    }
)


class Clock:
    """A clock the test moves by hand, in nanoseconds."""

    def __init__(self) -> None:
        self.now = 0

    def __call__(self) -> int:
        return self.now


def shape(model: sde.LogicalModel, kind: str, fields: tuple[str, ...] = ()) -> sde.OperationShape:
    return next(
        s for s in sde.enumerate_shapes(model) if s.kind == kind and tuple(s.fields) == fields
    )


def record(
    recorder: sde.Recorder, found: sde.OperationShape, *, rows: int = 1, failed: bool = False,
    equal: list[str] | None = None, ranged: str | None = None,
) -> None:
    recorder.record(
        shape_id=found.id, group=found.group, entity=found.entity, kind=found.kind,
        nanoseconds=1_000, rows=rows, failed=failed, equal=equal, ranged=ranged,
    )


def body(recorder: sde.Recorder, model: sde.LogicalModel = MODEL) -> dict[str, Any]:
    window = recorder.roll()
    assert window is not None
    group = sde.colocation_groups(model)[0].name
    result: dict[str, Any] = window.as_record(model)["groups"][group]
    return result


# --- time_filtered_share ---------------------------------------------------------------------


def test_a_call_that_bounds_or_compares_a_time_field_is_time_filtered() -> None:
    recorder = sde.Recorder(MODEL.version)
    ranged = shape(MODEL, "range_read", ("at",))
    point = shape(MODEL, "point_read", tuple(sorted(("station", "at"))))
    aggregate = shape(MODEL, "aggregate")
    for _ in range(3):
        record(recorder, ranged, equal=["station"], ranged="at")  # a range over time
    record(recorder, aggregate, equal=["at"])  # equality on a time field filters on time too
    record(recorder, aggregate, equal=["station"])  # a filter, but not on time
    record(recorder, aggregate, equal=[])  # a filter on nothing
    record(recorder, point)  # a point read takes no `where`
    record(recorder, shape(MODEL, "write"), rows=1)
    features = body(recorder)
    assert features["time_filtered_share"] == 4 / 8
    assert "time_filtered_share" not in features["missing"]


def test_a_range_over_a_field_that_is_not_a_time_is_not_time_filtered() -> None:
    recorder = sde.Recorder(MODEL.version)
    record(recorder, shape(MODEL, "range_read", ("temperature",)), equal=[], ranged="temperature")
    assert body(recorder)["time_filtered_share"] == 0.0


def test_a_group_without_a_time_field_is_measured_at_zero_not_unknown() -> None:
    recorder = sde.Recorder(TIMELESS.version)
    record(recorder, shape(TIMELESS, "full_scan"), equal=["height"])
    features = body(recorder, TIMELESS)
    assert features["time_filtered_share"] == 0.0
    assert features["has_time_dimension"] is False


def test_features_without_the_time_fields_leave_the_share_unknown() -> None:
    """A caller of `features()` that does not say which fields are times gets no guess."""
    recorder = sde.Recorder(MODEL.version)
    record(recorder, shape(MODEL, "range_read", ("at",)), equal=[], ranged="at")
    window = recorder.roll()
    assert window is not None
    features = window.features("Reading", has_time_dimension=True)
    assert features.time_filtered_share is None
    assert "time_filtered_share" in features.missing
    given = window.features("Reading", has_time_dimension=True, time_fields={"Reading": {"at"}})
    assert given.time_filtered_share == 1.0


def test_time_fields_are_found_by_type_never_by_name() -> None:
    named = model_from_neutral(
        {
            "entities": [
                {
                    "name": "Log",
                    "fields": [
                        {"name": "created_at", "type": "string"},
                        {"name": "seen", "type": "date"},
                    ],
                    "key": ["created_at"],
                }
            ]
        }
    )
    group = sde.colocation_groups(named)[0]
    assert sde.time_fields(named, group) == {"Log": frozenset({"seen"})}


# --- storage: total_bytes and index_to_table_ratio -------------------------------------------


def test_a_storage_sample_in_the_window_gives_the_size_and_the_index_ratio() -> None:
    clock = Clock()
    recorder = sde.Recorder(MODEL.version, clock=clock)
    record(recorder, shape(MODEL, "write"))
    clock.now = 2 * SECOND_NS
    recorder.record_storage(group="Reading", total_bytes=700_000, secondary_index_bytes=100_000)
    features = body(recorder)
    assert features["total_bytes"] == 700_000
    assert features["index_to_table_ratio"] == 100_000 / 600_000


def test_the_latest_sample_of_the_window_is_the_size() -> None:
    clock = Clock()
    recorder = sde.Recorder(MODEL.version, clock=clock)
    record(recorder, shape(MODEL, "write"))
    recorder.record_storage(group="Reading", total_bytes=500, secondary_index_bytes=0)
    clock.now = SECOND_NS
    recorder.record_storage(group="Reading", total_bytes=900, secondary_index_bytes=300)
    features = body(recorder)
    assert features["total_bytes"] == 900
    assert features["index_to_table_ratio"] == 300 / 600


def test_no_sample_in_the_window_leaves_the_size_unknown() -> None:
    clock = Clock()
    recorder = sde.Recorder(MODEL.version, clock=clock)
    record(recorder, shape(MODEL, "write"))
    recorder.record_storage(group="Reading", total_bytes=500, secondary_index_bytes=0)
    body(recorder)  # the sample belongs to the first window
    clock.now = 10 * SECOND_NS
    record(recorder, shape(MODEL, "write"))
    features = body(recorder)
    assert "total_bytes" not in features and "total_bytes" in features["missing"]
    assert "index_to_table_ratio" in features["missing"]


def test_an_index_ratio_over_nothing_is_unknown() -> None:
    recorder = sde.Recorder(MODEL.version, clock=Clock())
    record(recorder, shape(MODEL, "write"))
    recorder.record_storage(group="Reading", total_bytes=0, secondary_index_bytes=0)
    features = body(recorder)
    assert features["total_bytes"] == 0
    assert "index_to_table_ratio" in features["missing"]


def test_a_storage_sample_never_raises_into_the_application() -> None:
    recorder = sde.Recorder(MODEL.version, clock=Clock())
    recorder.record_storage(group="Reading", total_bytes="lots", secondary_index_bytes=0)  # type: ignore[arg-type]
    recorder.record_storage(group="Reading", total_bytes=-1, secondary_index_bytes=0)
    record(recorder, shape(MODEL, "write"))
    assert "total_bytes" in body(recorder)["missing"]


# --- daily_growth_bytes ----------------------------------------------------------------------


def test_growth_is_projected_to_a_day_from_samples_an_hour_or_more_apart() -> None:
    clock = Clock()
    recorder = sde.Recorder(MODEL.version, clock=clock)
    record(recorder, shape(MODEL, "write"))
    recorder.record_storage(group="Reading", total_bytes=1_000_000, secondary_index_bytes=0)
    clock.now = 2 * HOUR_NS
    recorder.record_storage(group="Reading", total_bytes=1_200_000, secondary_index_bytes=0)
    assert body(recorder)["daily_growth_bytes"] == 200_000 * 12


def test_growth_from_less_than_an_hour_is_unknown() -> None:
    clock = Clock()
    recorder = sde.Recorder(MODEL.version, clock=clock)
    record(recorder, shape(MODEL, "write"))
    recorder.record_storage(group="Reading", total_bytes=1_000, secondary_index_bytes=0)
    clock.now = HOUR_NS - 1
    recorder.record_storage(group="Reading", total_bytes=9_000, secondary_index_bytes=0)
    features = body(recorder)
    assert "daily_growth_bytes" in features["missing"]
    assert features["total_bytes"] == 9_000


def test_growth_is_truncated_toward_zero_in_both_directions() -> None:
    for delta, expected in ((1, 3), (-1, -3)):
        clock = Clock()
        recorder = sde.Recorder(MODEL.version, clock=clock)
        record(recorder, shape(MODEL, "write"))
        recorder.record_storage(group="Reading", total_bytes=100, secondary_index_bytes=0)
        clock.now = 7 * HOUR_NS  # a day is 24/7 = 3.43 of these
        recorder.record_storage(group="Reading", total_bytes=100 + delta, secondary_index_bytes=0)
        assert body(recorder)["daily_growth_bytes"] == expected


def test_growth_reaches_back_across_windows_but_not_past_a_day() -> None:
    clock = Clock()
    recorder = sde.Recorder(MODEL.version, clock=clock)
    record(recorder, shape(MODEL, "write"))
    recorder.record_storage(group="Reading", total_bytes=1_000, secondary_index_bytes=0)
    body(recorder)
    clock.now = 3 * HOUR_NS
    record(recorder, shape(MODEL, "write"))
    recorder.record_storage(group="Reading", total_bytes=4_000, secondary_index_bytes=0)
    assert body(recorder)["daily_growth_bytes"] == 3_000 * 8  # the earlier window's sample

    clock.now = 3 * HOUR_NS + DAY_NS - 1  # the first sample is over a day old, the second is not
    record(recorder, shape(MODEL, "write"))
    recorder.record_storage(group="Reading", total_bytes=4_400, secondary_index_bytes=0)
    assert body(recorder)["daily_growth_bytes"] == 400  # measured against the 3-hour sample

    clock.now = 3 * HOUR_NS + 2 * DAY_NS  # now every earlier sample is over a day old
    record(recorder, shape(MODEL, "write"))
    recorder.record_storage(group="Reading", total_bytes=9_999, secondary_index_bytes=0)
    features = body(recorder)
    assert "daily_growth_bytes" in features["missing"] and features["total_bytes"] == 9_999


# --- write_burstiness ------------------------------------------------------------------------


def test_even_writes_have_a_burstiness_of_one() -> None:
    clock = Clock()
    recorder = sde.Recorder(MODEL.version, clock=clock)
    write = shape(MODEL, "write")
    for second in range(4):
        clock.now = second * SECOND_NS
        record(recorder, write, rows=10)
    clock.now = 4 * SECOND_NS
    assert body(recorder)["write_burstiness"] == 1.0


def test_a_burst_is_how_many_times_the_busiest_second_exceeds_the_mean() -> None:
    clock = Clock()
    recorder = sde.Recorder(MODEL.version, clock=clock)
    bulk = shape(MODEL, "bulk_write")
    record(recorder, bulk, rows=90)  # second 0
    clock.now = 3 * SECOND_NS + 1
    record(recorder, bulk, rows=10)  # second 3
    clock.now = 10 * SECOND_NS
    assert body(recorder)["write_burstiness"] == 90 * 10 / 100


def test_a_failed_write_writes_no_rows() -> None:
    clock = Clock()
    recorder = sde.Recorder(MODEL.version, clock=clock)
    write = shape(MODEL, "write")
    record(recorder, write, rows=5)
    clock.now = SECOND_NS
    record(recorder, write, rows=500, failed=True)
    clock.now = 2 * SECOND_NS
    assert body(recorder)["write_burstiness"] == 5 * 2 / 5


def test_no_rows_written_leaves_burstiness_unknown() -> None:
    recorder = sde.Recorder(MODEL.version, clock=Clock())
    record(recorder, shape(MODEL, "range_read", ("at",)), equal=[], ranged="at")
    assert "write_burstiness" in body(recorder)["missing"]


def test_a_sample_taken_at_the_instant_of_a_roll_belongs_to_one_window() -> None:
    """Attribution is by the recorder's state, not by comparing equal clock readings."""
    clock = Clock()
    recorder = sde.Recorder(MODEL.version, clock=clock)
    record(recorder, shape(MODEL, "write"))
    recorder.record_storage(group="Reading", total_bytes=123, secondary_index_bytes=0)
    assert body(recorder)["total_bytes"] == 123  # rolled at the same reading: 0
    record(recorder, shape(MODEL, "write"))
    assert "total_bytes" in body(recorder)["missing"]


def test_a_write_after_the_measured_end_still_counts_its_second() -> None:
    """A record racing a roll can land past the end; the window is at least as long as its writes."""
    clock = Clock()
    recorder = sde.Recorder(MODEL.version, clock=clock)
    write = shape(MODEL, "write")
    clock.now = 5 * SECOND_NS
    record(recorder, write, rows=6)
    clock.now = 2 * SECOND_NS  # an injected clock may go backwards; the bucket is still counted
    assert body(recorder)["write_burstiness"] == 6 * 6 / 6


@pytest.mark.parametrize("model", [MODEL, TIMELESS])
def test_the_five_features_are_no_longer_missing_when_measured(model: sde.LogicalModel) -> None:
    clock = Clock()
    recorder = sde.Recorder(model.version, clock=clock)
    group = sde.colocation_groups(model)[0].name
    record(recorder, shape(model, "write"), rows=3)
    recorder.record_storage(group=group, total_bytes=10_000, secondary_index_bytes=1_000)
    clock.now = 2 * HOUR_NS
    recorder.record_storage(group=group, total_bytes=12_000, secondary_index_bytes=1_000)
    features = body(recorder, model)
    for name in (
        "total_bytes", "index_to_table_ratio", "daily_growth_bytes", "time_filtered_share",
        "write_burstiness",
    ):
        assert name in features and name not in features["missing"], name

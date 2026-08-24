"""What this library costs per operation, measured on the mechanism rather than on the noise.

Requirement 3.5 says the library must add no more than one percent to the p99 of an instrumented
operation, and that the number is release-blocking. The naive way to check that is to time an
operation through the session, time the same operation against the driver directly, and compare the
two p99s. On this hardware that measurement cannot resolve one percent: a PostgreSQL round trip over
loopback is on the order of a hundred microseconds and its own run-to-run spread is several percent,
so the difference being looked for is smaller than the noise it is buried in.

The engine repository learned this the expensive way, and the lesson transfers: **measure the thing
you changed.** What the library adds per operation is a shape lookup, three conditions and a table
name lookup. That is directly measurable, in isolation, with no database involved, and it can be
compared against a separately measured round trip to produce a ratio that means something.

So there are two tests here and they do different jobs:

* `test_library_overhead_per_operation_is_under_one_percent` measures the added work directly and
  divides by a measured round trip. This is the one that gates a release.
* `test_end_to_end_comparison_is_reported_but_not_asserted` runs the A/B anyway and prints both
  numbers with their spread, because a wildly wrong end-to-end result would mean the first test is
  measuring the wrong thing. It deliberately asserts almost nothing: an assertion that fails on
  noise teaches a team to rerun the suite until it passes, which is worse than no assertion.
"""

from __future__ import annotations

import os
import statistics
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest

import sde
from sde.engines.postgres import PostgresEngine

DSN = os.environ.get("SDE_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set SDE_POSTGRES_DSN to measure overhead against a real round trip"
)

# Enough samples that a p99 means something, few enough that the suite stays fast. The library-side
# measurement is nanoseconds per call, so it gets far more.
ROUND_TRIPS = 300
LIBRARY_CALLS = 200_000
BUDGET = 0.01  # one percent, from requirement 3.5


def _percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


def _time(operation: Callable[[], object], repeats: int) -> list[float]:
    samples: list[float] = []
    for _ in range(repeats):
        start = time.perf_counter_ns()
        operation()
        samples.append(time.perf_counter_ns() - start)
    return samples


@pytest.fixture()
def model() -> sde.LogicalModel:
    sde.clear_registry()

    @sde.entity
    class Reading:
        id: uuid.UUID
        sensor: str
        value: float

    return sde.build_model(Reading)


@pytest.fixture()
def placement(model: sde.LogicalModel) -> sde.PlacementMap:
    raw: dict[str, Any] = {
        "contract": sde.CONTRACT,
        "model_version": model.version,
        "map_version": 1,
        "groups": {
            g.name: {"source": {"id": f"{g.name}@pg", "engine": "pg", "layout": {"auto": True}}}
            for g in sde.colocation_groups(model)
        },
    }
    return sde.load_map(raw, model=model)


@pytest.fixture()
def engine(model: sde.LogicalModel, placement: sde.PlacementMap) -> Iterator[PostgresEngine]:
    assert DSN
    with PostgresEngine(DSN) as eng:
        table = placement.placement_of("Reading").source.layout.table_for("Reading")
        with eng._cx.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
        for group in sde.colocation_groups(model):
            layout = placement.placement_of(group.name).source.layout
            eng.ensure_schema(layout, keys={n: model.entity(n).key for n in group.members})
        yield eng


def test_library_overhead_per_operation_is_under_one_percent(
    model: sde.LogicalModel, placement: sde.PlacementMap, engine: PostgresEngine
) -> None:
    """The release gate. Measures the added work, not the difference between two noisy totals."""
    table = placement.placement_of("Reading").source.layout.table_for("Reading")

    # What a round trip costs. Measured through the driver, so the library is not in it at all.
    key = uuid.uuid4()
    engine.insert(table, {"id": key, "sensor": "s", "value": 1.0})
    round_trips = _time(lambda: engine.get(table, {"id": key}), ROUND_TRIPS)
    round_trip_p99 = _percentile(round_trips, 0.99)

    # What the library adds: resolving a shape to a materialisation, and finding the table name.
    # Everything else in session.get is the driver call above.
    shape = next(s for s in sde.enumerate_shapes(model) if s.kind == "point_read")
    router = sde.Router(placement)

    def added_work() -> object:
        materialization = router.resolve(shape)
        return materialization.layout.table_for("Reading")

    added = _time(added_work, LIBRARY_CALLS)
    added_p99 = _percentile(added, 0.99)
    added_median = statistics.median(added)

    ratio = added_p99 / round_trip_p99

    print(
        f"\n  round trip p99      {round_trip_p99 / 1000:9.1f} µs  (n={ROUND_TRIPS})"
        f"\n  library added p99   {added_p99 / 1000:9.3f} µs  (n={LIBRARY_CALLS})"
        f"\n  library added p50   {added_median / 1000:9.3f} µs"
        f"\n  ratio               {ratio * 100:9.3f} %  budget {BUDGET * 100:.0f} %"
    )

    assert ratio < BUDGET, (
        f"the library adds {ratio * 100:.2f}% of a round trip at p99, over the "
        f"{BUDGET * 100:.0f}% budget in requirement 3.5. Look at what routing is doing per call: "
        "it is supposed to be a dictionary lookup and three conditions, and anything that turned "
        "it into more than that is the regression."
    )

    # And a floor on the round trip, so that a broken fixture cannot make the ratio look good by
    # making the denominator enormous.
    assert round_trip_p99 > 10_000, (
        "the round trip measured under 10 µs, which is not a PostgreSQL round trip. The comparison "
        "is meaningless if the denominator is wrong."
    )


def test_end_to_end_comparison_is_reported_but_not_asserted(
    model: sde.LogicalModel, placement: sde.PlacementMap, engine: PostgresEngine
) -> None:
    """The sanity check. Reports both numbers and their spread, asserts only the obvious.

    Deliberately not a threshold test. On this hardware a one percent difference in end-to-end p99
    is well inside the run-to-run spread, so a strict assertion here would fail on noise - and a
    test that fails on noise teaches a team to rerun until it goes green, which is worse than
    having no test.
    """
    session = sde.Session(model, placement, {"pg": engine})
    table = placement.placement_of("Reading").source.layout.table_for("Reading")

    key = uuid.uuid4()
    engine.insert(table, {"id": key, "sensor": "s", "value": 1.0})

    direct = _time(lambda: engine.get(table, {"id": key}), ROUND_TRIPS)
    through = _time(lambda: session.get("Reading", {"id": key}), ROUND_TRIPS)

    def spread(samples: list[float]) -> float:
        return (_percentile(samples, 0.99) - _percentile(samples, 0.5)) / _percentile(samples, 0.5)

    direct_p99 = _percentile(direct, 0.99)
    through_p99 = _percentile(through, 0.99)

    print(
        f"\n  direct p99          {direct_p99 / 1000:9.1f} µs"
        f"   spread p50->p99 {spread(direct):6.1%}"
        f"\n  through session p99 {through_p99 / 1000:9.1f} µs"
        f"   spread p50->p99 {spread(through):6.1%}"
        f"\n  difference          {(through_p99 / direct_p99 - 1) * 100:9.1f} %"
        "\n  (reported, not asserted: this difference is inside the noise on this hardware)"
    )

    # The only thing worth asserting: the session did not make the operation an order of magnitude
    # slower. That would not be noise, it would be a bug - a second round trip, or a connection
    # opened per call.
    assert through_p99 < direct_p99 * 3, (
        f"going through the session made a point read {through_p99 / direct_p99:.1f}x slower at "
        "p99. "
        "That is far outside measurement noise; look for a second round trip or a connection being "
        "opened per operation."
    )

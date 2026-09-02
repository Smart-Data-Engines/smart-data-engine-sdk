"""Measuring what the application actually does, without ever seeing what it does it to.

This is the input the placement decision is made from, so its shape matters more than its precision.
Three constraints shaped everything here.

**It carries no values.** A record is keyed by an operation shape, which is assembled from the
structure of a call and never sees its arguments. There is no code path by which a customer's row
reaches a telemetry record, which is why this file can be read by a client and believed.

**It cannot cost anything.** Routing already has a one percent budget for the whole library, and
recording happens on the same path. So: no locks on the hot path per record, no string formatting,
no stack walking except once per shape, and a histogram rather than a list of samples.

**It cannot fail the caller.** Every entry point is wrapped in :func:`~sde.internal.guard`. A bug in
an aggregation counter must not take down somebody's request - and the failure is counted, so it is
not invisible either.

The histogram deserves a word, because it is the one deliberate loss of precision. Latency lands in
exponential buckets, so a percentile read out of it is approximate - within one bucket width, which
is a factor of two at the extremes. That is ample for the decision it feeds: the planner cares
whether a group's reads are microseconds or milliseconds, not whether p99 is 412 or 431
microseconds. Keeping exact samples would mean either unbounded memory or reservoir sampling, and
reservoir sampling gets the tail wrong in exactly the region the planner looks at.
"""

from __future__ import annotations

import math
import sys
import threading
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .internal import guard
from .logging import log
from .shapes import WRITE_KINDS

__all__ = [
    "GroupFeatures",
    "Histogram",
    "Recorder",
    "ShapeStats",
    "Window",
    "has_time_dimension",
]

# 1 µs to about 17 s, doubling. Twenty-five buckets is enough to tell a cache hit from a full scan,
# which is the distinction the planner actually acts on.
BUCKET_COUNT = 25
BUCKET_BASE_NS = 1_000


class Histogram:
    """Exponential-bucket histogram. Fixed memory, O(1) record, approximate percentiles."""

    __slots__ = ("buckets", "count", "total")

    def __init__(self) -> None:
        self.buckets = [0] * BUCKET_COUNT
        self.count = 0
        self.total = 0

    def record(self, nanoseconds: int) -> None:
        self.count += 1
        self.total += nanoseconds
        if nanoseconds < BUCKET_BASE_NS:
            self.buckets[0] += 1
            return
        index: int = min(BUCKET_COUNT - 1, int(math.log2(nanoseconds / BUCKET_BASE_NS)) + 1)
        self.buckets[index] += 1

    def percentile_ms(self, fraction: float) -> float | None:
        """Approximate percentile in milliseconds, or None if nothing was recorded.

        Returns the *upper* edge of the bucket the percentile falls in. Rounding up rather than
        interpolating is deliberate: a placement decision made on an optimistic latency figure is
        the wrong kind of wrong.
        """
        if self.count == 0:
            return None
        target = fraction * self.count
        seen = 0
        for index, hits in enumerate(self.buckets):
            seen += hits
            if seen >= target:
                upper_ns: int = BUCKET_BASE_NS * (2**index)
                return upper_ns / 1_000_000
        return None

    def merge(self, other: Histogram) -> None:
        for index, hits in enumerate(other.buckets):
            self.buckets[index] += hits
        self.count += other.count
        self.total += other.total


@dataclass
class ShapeStats:
    """What was observed for one operation shape. No values, by construction."""

    shape_id: str
    group: str
    entity: str
    kind: str
    calls: int = 0
    rows: int = 0
    errors: int = 0
    latency: Histogram = field(default_factory=Histogram)
    call_site: str | None = None

    def record(self, nanoseconds: int, rows: int, failed: bool) -> None:
        self.calls += 1
        self.rows += rows
        if failed:
            self.errors += 1
        self.latency.record(nanoseconds)


@dataclass(frozen=True)
class GroupFeatures:
    """The contract between telemetry and the planner.

    ``None`` means *unknown*, which is not zero and is treated differently by the planner. Anything
    unknown also appears in ``missing``, so a reader never has to infer absence from a null.
    """

    calls: int = 0
    """How many operations were observed. A count, never a value.

    Present because a planner comparing two groups has to know which one carries the traffic:
    without it, an idle group and the group serving every request are equally important, and one
    dead group can outvote the one that matters. It is also evidence about the features themselves -
    a read/write ratio derived from twelve calls is arithmetic, not a measurement, and the planner
    should be able to see that rather than being told a confident number.
    """

    read_write_ratio: float | None = None
    shape_mix: Mapping[str, float] = field(default_factory=dict)
    latency_p50_ms: float | None = None
    latency_p99_ms: float | None = None
    result_cardinality_p50: float | None = None
    result_cardinality_p99: float | None = None
    total_bytes: int | None = None
    daily_growth_bytes: int | None = None
    index_to_table_ratio: float | None = None
    pk_access_share: float | None = None
    has_time_dimension: bool = False
    time_filtered_share: float | None = None
    distinct_shapes: int = 0
    write_burstiness: float | None = None
    error_share: float | None = None
    missing: frozenset[str] = frozenset()
    complete: bool = True


@dataclass(frozen=True)
class Window:
    """One aggregation period, ready to send.

    ``complete`` is false when the library could not reach the control plane for part of the period,
    or when the buffer dropped windows. A window that is not complete is still sent - the planner
    needs to know traffic existed - but it may not be used to justify a migration.
    """

    model_version: str
    started_ns: int
    ended_ns: int
    shapes: Sequence[ShapeStats]
    complete: bool = True
    dropped_windows: int = 0

    def features(self, group: str, *, has_time_dimension: bool = False) -> GroupFeatures:
        """Fold this window's records for one group into the planner's feature vector."""
        records = [s for s in self.shapes if s.group == group]
        if not records:
            return GroupFeatures(
                missing=frozenset({"no_traffic"}), complete=self.complete, distinct_shapes=0
            )

        writes = sum(s.calls for s in records if s.kind in WRITE_KINDS)
        reads = sum(s.calls for s in records if s.kind not in WRITE_KINDS)
        calls = writes + reads

        latency = Histogram()
        for record in records:
            latency.merge(record.latency)

        mix: dict[str, float] = {}
        for record in records:
            mix[record.kind] = mix.get(record.kind, 0.0) + record.calls
        mix = {kind: hits / calls for kind, hits in sorted(mix.items())} if calls else {}

        read_records = [s for s in records if s.kind not in WRITE_KINDS and s.calls]
        cardinalities = sorted(s.rows / s.calls for s in read_records)

        pk_calls = sum(s.calls for s in records if s.kind == "point_read")
        errors = sum(s.errors for s in records)

        # Everything the library cannot see from inside the application. Engine-side sizes need a
        # catalogue read, which is an adapter capability, and growth needs two samples over time.
        # Named rather than silently zero, because zero bytes and unknown bytes lead the planner to
        # opposite conclusions.
        missing = {"total_bytes", "daily_growth_bytes", "index_to_table_ratio", "write_burstiness"}
        if not read_records:
            missing |= {"result_cardinality_p50", "result_cardinality_p99"}
        if not writes:
            missing.add("read_write_ratio")

        return GroupFeatures(
            calls=calls,
            read_write_ratio=(reads / writes) if writes else None,
            shape_mix=mix,
            latency_p50_ms=latency.percentile_ms(0.5),
            latency_p99_ms=latency.percentile_ms(0.99),
            result_cardinality_p50=_at(cardinalities, 0.5),
            result_cardinality_p99=_at(cardinalities, 0.99),
            pk_access_share=(pk_calls / calls) if calls else None,
            has_time_dimension=has_time_dimension,
            distinct_shapes=len(records),
            error_share=(errors / calls) if calls else None,
            missing=frozenset(missing),
            complete=self.complete,
        )




def _at(ordered: list[float], fraction: float) -> float | None:
    if not ordered:
        return None
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


# Types that make a field a time dimension. Recognised by **type**, never by name - see below.
_TIME_TYPES = frozenset({"date", "timestamp", "timestamptz"})


def has_time_dimension(model: Any, group: Any) -> bool:
    """Does any entity in this group carry a time dimension?

    Decided by the declared type and never by the field's name. Two reasons, and the second is the
    one that made it a rule rather than a preference.

    A name is not evidence: `created_at` typed as a string is a string, and treating it as a
    timestamp would have the planner recommend time partitioning on a column no engine can
    range-scan usefully.

    And a client may hash identifier names (requirement 11.3) so that we never see them. A
    derivation that reads names would silently produce different answers with hashing on and off -
    which is exactly the property the conformance vector for hashed models exists to forbid.
    Deciding by type also means models written in other natural languages work identically, which is
    a free consequence of getting this right for the other reason.
    """
    for member in group.members:
        spec = model.entity(member)
        for spec_field in spec.fields:
            if spec_field.type in _TIME_TYPES:
                return True
    return False


class Recorder:
    """Accumulates records, rolls windows, and drops telemetry rather than anything else.

    Thread safety is a single lock taken only when a shape is first seen or a window rolls - not on
    every record. Counters for an existing shape are updated without it: two threads racing on the
    same shape can lose a call from the count, and that is the right trade. Telemetry informs a
    placement decision made over days; a lock on the hot path would cost every operation forever.
    """

    def __init__(self, model_version: str, *, max_windows: int = 64) -> None:
        self._model_version = model_version
        self._lock = threading.Lock()
        self._current: dict[str, ShapeStats] = {}
        self._started_ns = _now()
        self._windows: deque[Window] = deque(maxlen=max_windows)
        self._dropped = 0
        self._incomplete = False

    # --- recording ---------------------------------------------------------------------------

    def record(
        self,
        *,
        shape_id: str,
        group: str,
        entity: str,
        kind: str,
        nanoseconds: int,
        rows: int = 0,
        failed: bool = False,
    ) -> None:
        """Record one operation. Never raises, never blocks on the common path."""
        guard(
            "telemetry.record",
            lambda: self._record(shape_id, group, entity, kind, nanoseconds, rows, failed),
        )

    def _record(
        self,
        shape_id: str,
        group: str,
        entity: str,
        kind: str,
        nanoseconds: int,
        rows: int,
        failed: bool,
    ) -> None:
        stats = self._current.get(shape_id)
        if stats is None:
            with self._lock:
                stats = self._current.get(shape_id)
                if stats is None:
                    stats = ShapeStats(
                        shape_id=shape_id, group=group, entity=entity, kind=kind,
                        call_site=_call_site(),
                    )
                    self._current[shape_id] = stats
        stats.record(nanoseconds, rows, failed)

    # --- windows -----------------------------------------------------------------------------

    def roll(self) -> Window | None:
        """Close the current period and queue it.

        Returns the window, or None if nothing was recorded in it.
        """
        return guard("telemetry.roll", self._roll)

    def _roll(self) -> Window | None:
        with self._lock:
            if not self._current:
                return None
            window = Window(
                model_version=self._model_version,
                started_ns=self._started_ns,
                ended_ns=_now(),
                shapes=tuple(self._current.values()),
                complete=not self._incomplete,
                dropped_windows=self._dropped,
            )
            self._current = {}
            self._started_ns = _now()
            self._incomplete = False

            # A full buffer drops the oldest window and says so in the next one. Telemetry is the
            # thing that gets lost when we run out of room - never a write, never an operation.
            if len(self._windows) == self._windows.maxlen:
                self._dropped += 1
                log("sde.telemetry.dropped", dropped=self._dropped)
            self._windows.append(window)
        log(
            "sde.telemetry.window_sent",
            model_version=window.model_version,
            shapes=len(window.shapes),
            complete=window.complete,
        )
        return window

    def pending(self) -> tuple[Window, ...]:
        with self._lock:
            return tuple(self._windows)

    def mark_incomplete(self) -> None:
        """Called when the library could not reach us for part of the current period."""
        self._incomplete = True

    def acknowledge(self, count: int) -> None:
        """Drop the oldest ``count`` windows after they were delivered."""
        with self._lock:
            for _ in range(min(count, len(self._windows))):
                self._windows.popleft()


def _now() -> int:
    return int(__import__("time").perf_counter_ns())


def _call_site() -> str | None:
    """Where in the client's code this shape is used. Walked once per shape, never per call.

    Best effort by design: a frame walk is not free and a missing call site costs a little
    diagnostic value, while a frame walk on every operation would cost the latency budget.
    """

    def walk() -> str | None:
        frame: Any = sys._getframe(1)
        while frame is not None:
            name = frame.f_globals.get("__name__", "")
            if not name.startswith("sde"):
                return f"{frame.f_code.co_filename}:{frame.f_lineno}"
            frame = frame.f_back
        return None

    return guard("telemetry.call_site", walk)

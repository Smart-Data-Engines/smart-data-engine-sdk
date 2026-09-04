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
    "CopyFreshness",
    "FanOutStats",
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


@dataclass
class FanOutStats:
    """What was observed writing one row to one derived copy. No values, by construction.

    Deliberately **not** a :class:`ShapeStats`, and that is the load-bearing decision. A fan-out is
    not an operation the application asked for - it is the library keeping a copy current - so
    recording it as a shape would add a write to the very counters a placement is scored on:
    ``read_write_ratio`` would move because a copy exists, and a group with one copy would look
    twice as write-heavy as the same group without one. The set of shape kinds that count as writes
    already had four copies once (:data:`sde.shapes.WRITE_KINDS`); this is the same failure
    arriving from the other side.

    It is also not in :class:`GroupFeatures`, for a reason worth stating: a copy's freshness does
    not score a placement. It reports the health of a copy the placement already made. Putting it
    in the feature vector would bump the frozen scoring model to add an input nothing scores.
    """

    group: str
    materialization: str
    writes: int = 0
    failures: int = 0
    """Rows that did not reach the copy. Absence, not lateness - see :class:`CopyFreshness`."""
    latency: Histogram = field(default_factory=Histogram)

    def record(self, nanoseconds: int, failed: bool) -> None:
        self.writes += 1
        if failed:
            self.failures += 1
        # Recorded either way. A failed fan-out took time too, and dropping it would make the
        # measured window look better precisely when the copy is in trouble.
        self.latency.record(nanoseconds)


@dataclass(frozen=True)
class CopyFreshness:
    """How far behind one derived copy is, measured. Requirement 5.2.

    **What "behind" means here was settled by looking at the mechanism rather than at the word.**
    A derived copy in this library is maintained by the fan-out in
    :meth:`sde.session.Session.save` - a write in the client's own process, to the copy, straight
    after the source. There is no asynchronous replication anywhere, so there is no queue to fall
    behind in. Two things can therefore be true of a copy, and only two:

    - it is **late** by at most the duration of that one write, which is what ``lag_p50_ms`` and
      ``lag_p99_ms`` measure;
    - or the write **failed**, and the row is absent rather than late. That is ``failures``, and it
      is the number a lag figure would hide: a copy missing a thousand rows can have an excellent
      p99.

    Both are reported because they are different problems with different fixes, and a client told
    only the first would read "0.9 ms behind" off a copy that is missing yesterday.

    Inside a write transaction the fan-out is deferred to commit, so the window measured there
    starts when the row was queued - inside the transaction - rather than at the commit. That
    **overstates** the staleness by the rest of the transaction, and overstating a staleness bound
    is the safe direction: the number is used to ask whether a copy is inside the budget a map
    declared, and a bound that flatters is a bound nobody can rely on.
    """

    group: str
    materialization: str
    writes: int
    failures: int
    lag_p50_ms: float | None
    lag_p99_ms: float | None

    @property
    def complete(self) -> bool:
        """Whether every write reached the copy in this window."""
        return self.failures == 0

    def as_record(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "materialization": self.materialization,
            "writes": self.writes,
            "failures": self.failures,
            "lag_p50_ms": self.lag_p50_ms,
            "lag_p99_ms": self.lag_p99_ms,
            "complete": self.complete,
        }


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
    fanned: Sequence[FanOutStats] = ()
    """What the fan-out to each derived copy did. Empty when the group has no copy, which is the
    ordinary case - a copy exists during a migration and while a derived materialisation is in the
    map, not otherwise."""

    def copies(self, group: str) -> tuple[CopyFreshness, ...]:
        """How far behind each of this group's derived copies ran, sorted by materialisation.

        Read out of the histogram rather than stored, like every other percentile here. A stored
        percentile is a second copy of a fact that changes when the samples do.
        """
        return tuple(
            CopyFreshness(
                group=stats.group,
                materialization=stats.materialization,
                writes=stats.writes,
                failures=stats.failures,
                lag_p50_ms=stats.latency.percentile_ms(0.50),
                lag_p99_ms=stats.latency.percentile_ms(0.99),
            )
            for stats in sorted(
                (s for s in self.fanned if s.group == group),
                key=lambda s: s.materialization,
            )
        )

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
        self._fanned: dict[tuple[str, str], FanOutStats] = {}
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

    def record_fan_out(
        self, *, group: str, materialization: str, nanoseconds: int, failed: bool = False
    ) -> None:
        """Record one write to one derived copy. Never raises, never blocks on the common path.

        A separate entry point from :meth:`record` rather than a shape kind, because a fan-out is
        not an operation the application asked for - see :class:`FanOutStats`.
        """
        guard(
            "telemetry.record_fan_out",
            lambda: self._record_fan_out(group, materialization, nanoseconds, failed),
        )

    def _record_fan_out(
        self, group: str, materialization: str, nanoseconds: int, failed: bool
    ) -> None:
        key = (group, materialization)
        stats = self._fanned.get(key)
        if stats is None:
            with self._lock:
                stats = self._fanned.get(key)
                if stats is None:
                    stats = FanOutStats(group=group, materialization=materialization)
                    self._fanned[key] = stats
        stats.record(nanoseconds, failed)

    # --- windows -----------------------------------------------------------------------------

    def roll(self) -> Window | None:
        """Close the current period and queue it.

        Returns the window, or None if nothing was recorded in it.
        """
        return guard("telemetry.roll", self._roll)

    def _roll(self) -> Window | None:
        with self._lock:
            if not self._current and not self._fanned:
                # `not self._current` alone would drop a window holding only fan-out records. That
                # cannot arise from an application write - a write records a shape and then fans
                # out - but it can from a backfill replaying rows, and a window silently discarded
                # is the shape of gap that makes a client's copy look healthier than it is.
                return None
            window = Window(
                model_version=self._model_version,
                started_ns=self._started_ns,
                ended_ns=_now(),
                shapes=tuple(self._current.values()),
                complete=not self._incomplete,
                dropped_windows=self._dropped,
                fanned=tuple(self._fanned.values()),
            )
            self._current = {}
            self._fanned = {}
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

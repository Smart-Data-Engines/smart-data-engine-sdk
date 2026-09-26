"""Generate the ``telemetry/`` vectors: operations in, the window document out.

Tier 1, and the second half of the precondition section 10 of the format contract states: the
vectors for a tier are written before a second library claims it. What they pin is the artefact that
crosses the boundary from a client's process to ours - the file the control plane's ``observe``
reads - and every derivation behind it.

Three things are worth knowing before reading a case.

**The window document is deliberately not canonical.** Section 1 of the contract rejects floating
point outright, because a float's textual form differs between languages, and almost every number
here is a float. This document is not signed, not hashed and never compared for equality, so that
rule does not apply to it - but the property that makes this family checkable at all is narrower:
every number in a window is either **a ratio of two integers** or **a bucket edge divided by a
million**, and IEEE 754 requires division to be correctly rounded. Two languages therefore compute
the same double even where they would print it differently, so these expectations are compared as
**numbers** rather than as bytes. That is the one place in this suite where that is true, and it is
an argument rather than a convenience.

**The document carries no clock, and each case brings its own.** A window's start and end are
readings of a monotonic clock with no meaning outside the process that took them, so the recorder
takes an injected clock here: a case entry may carry ``at_ms`` (milliseconds since the recorder was
created), ``clock.json`` sets the reading at the final roll, and an entry may be an ``event`` -
``storage`` (a size sample, as :meth:`Session.measure_storage` records it) or ``roll`` (an earlier
window, closed and discarded; the document pinned is the last window's). Without ``at_ms`` the clock
stands at zero, so every earlier case stays deterministic: its writes land in second zero.

**Buffer eviction is not in here.** ``dropped_windows`` is part of the document and is zero in every
case, because a full buffer dropping its oldest window is behaviour with no artefact: each library
tests it directly. These vectors pin what two libraries have to agree about in the *document*.

    python conformance/tools/telemetry_vectors.py --i-am-changing-the-contract
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

import sde  # noqa: E402
from sde.telemetry import BUCKET_BASE_NS, BUCKET_COUNT  # noqa: E402
from sde.testing.loader import model_from_neutral  # noqa: E402

VECTORS = ROOT / "conformance" / "vectors" / "telemetry"


def _write(name: str, files: dict[str, Any]) -> None:
    out = VECTORS / name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for filename, body in files.items():
        (out / filename).write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    print(f"  wrote telemetry/{name}")


def _shop() -> sde.LogicalModel:
    """Two groups, a relation, and one entity whose time-looking field is a string.

    Every part is load-bearing. **Two groups**, because a document has to keep them apart and a
    one-group model cannot show that. **A relation**, because ``relation_walk`` is a shape kind and
    a kind no vector exercises is a classification nothing shared checks - which is how removing
    ``bulk_write`` from the set of write kinds survived its first mutation here. **A string called
    ``created_at``**, because ``has_time_dimension`` is decided by declared type and never by a
    field's name, and a model where every entity has a real timestamp cannot tell the two rules
    apart.

    A relation unions its two ends into one colocation group, which is why the relation is
    Detail -> Event and Label stands alone.
    """
    sde.clear_registry()
    return model_from_neutral(
        {
            "entities": [
                {
                    "name": "Event",
                    "fields": [
                        {"name": "id", "type": "uuid"},
                        {"name": "at", "type": "timestamptz"},
                        {"name": "name", "type": "string"},
                    ],
                    "key": ["id"],
                },
                {
                    "name": "Detail",
                    "fields": [
                        {"name": "id", "type": "uuid"},
                        {"name": "body", "type": "string"},
                    ],
                    "key": ["id"],
                },
                {
                    "name": "Label",
                    "fields": [
                        {"name": "id", "type": "uuid"},
                        {"name": "created_at", "type": "string"},
                    ],
                    "key": ["id"],
                    "pii": [],
                },
            ],
            "relations": [{"name": "event", "from": "Detail", "to": "Event"}],
            "atomic": [],
        }
    )


def _by_kind(model: sde.LogicalModel) -> dict[tuple[str, str], sde.OperationShape]:
    return {(s.entity, s.kind): s for s in sde.enumerate_shapes(model)}


class _Clock:
    """The case's clock, in nanoseconds; entries move it with ``at_ms``."""

    def __init__(self) -> None:
        self.now = 0

    def __call__(self) -> int:
        return self.now


def _replay(
    recorder: sde.Recorder, model: sde.LogicalModel, entries: list[dict[str, Any]], clock: _Clock
) -> None:
    """Every entry of a case in order: operations, storage samples and earlier rolls."""
    by_id = {s.id: s for s in sde.enumerate_shapes(model)}
    for entry in entries:
        if "at_ms" in entry:
            clock.now = int(entry["at_ms"]) * 1_000_000
        event = entry.get("event")
        if event == "storage":
            recorder.record_storage(
                group=str(entry["group"]),
                total_bytes=int(entry["total_bytes"]),
                secondary_index_bytes=int(entry["secondary_index_bytes"]),
            )
        elif event == "roll":
            assert recorder.roll() is not None, "an earlier window of the case recorded nothing"
        else:
            shape = by_id[entry["shape"]]
            recorder.record(
                shape_id=shape.id,
                group=shape.group,
                entity=shape.entity,
                kind=shape.kind,
                nanoseconds=int(entry["ns"]),
                rows=int(entry.get("rows", 0)),
                failed=bool(entry.get("failed", False)),
                equal=entry.get("equal"),
                ranged=entry.get("range"),
            )


def _record(
    model: sde.LogicalModel,
    operations: list[dict[str, Any]],
    fan_out: list[dict[str, Any]] | None = None,
    window_ms: int | None = None,
) -> dict[str, Any]:
    """Feed the reference implementation and take the document it produces."""
    clock = _Clock()
    recorder = sde.Recorder(model.version, clock=clock)
    _replay(recorder, model, operations, clock)
    for entry in fan_out or ():
        recorder.record_fan_out(
            group=str(entry["group"]),
            materialization=str(entry["materialization"]),
            nanoseconds=int(entry["ns"]),
            failed=bool(entry.get("failed", False)),
        )
    if window_ms is not None:
        clock.now = window_ms * 1_000_000
    window = recorder.roll()
    assert window is not None, "the case recorded nothing, so it would pin nothing"
    return window.as_record(model)


def _features(model: sde.LogicalModel, operations: list[dict[str, Any]], group: str) -> Any:
    by_id = {s.id: s for s in sde.enumerate_shapes(model)}
    recorder = sde.Recorder(model.version, clock=_Clock())
    for operation in operations:
        shape = by_id[operation["shape"]]
        recorder.record(
            shape_id=shape.id,
            group=shape.group,
            entity=shape.entity,
            kind=shape.kind,
            nanoseconds=int(operation["ns"]),
            rows=int(operation.get("rows", 0)),
            failed=bool(operation.get("failed", False)),
        )
    window = recorder.roll()
    assert window is not None
    members = next(g for g in sde.colocation_groups(model) if g.name == group)
    return window.features(
        group, has_time_dimension=sde.has_time_dimension(model, members)
    ).as_record()


def _one() -> None:
    model = _shop()
    by_kind = _by_kind(model)
    operations = [
        # Six point reads, one of them failing, each returning one row.
        *[{"shape": by_kind[("Event", "point_read")].id, "ns": 1_200, "rows": 1} for _ in range(5)],
        {"shape": by_kind[("Event", "point_read")].id, "ns": 40_000, "rows": 0, "failed": True},
        # Two full scans returning a lot, and one of each remaining read kind.
        {"shape": by_kind[("Event", "full_scan")].id, "ns": 900_000, "rows": 4_000},
        {"shape": by_kind[("Event", "full_scan")].id, "ns": 1_100_000, "rows": 4_200},
        {"shape": by_kind[("Event", "range_read")].id, "ns": 8_000, "rows": 12},
        {"shape": by_kind[("Event", "aggregate")].id, "ns": 60_000, "rows": 1},
        {"shape": by_kind[("Detail", "relation_walk")].id, "ns": 4_000, "rows": 3},
        # Both write kinds. `bulk_write` is here because removing it from the set of write kinds
        # survived its first mutation against this family: a classification no vector exercises is
        # one nothing shared checks, and the consequence is a group scored as read-heavy while it
        # takes every bulk load the application sends.
        *[{"shape": by_kind[("Event", "write")].id, "ns": 2_500, "rows": 1} for _ in range(3)],
        {"shape": by_kind[("Event", "bulk_write")].id, "ns": 250_000, "rows": 500},
        # And a different group, so the document has to keep them apart.
        {"shape": by_kind[("Label", "point_read")].id, "ns": 800, "rows": 1},
    ]
    _write(
        "001-one-window-of-mixed-traffic",
        {
            "model.json": sde.neutral_declaration(model),
            "operations.json": operations,
            "window.json": _record(model, operations),
            "why.json": {
                "why": (
                    "The whole document, once, and every shape kind the model admits. Everything "
                    "in it is a count or a ratio of two counts: read_write_ratio is reads over "
                    "writes as integers - with both write kinds counting as writes, which is what "
                    "this case pins - shape_mix is each kind's share, pk_access_share counts point "
                    "reads only, error_share counts failures, and distinct_shapes is how many "
                    "shapes were seen rather than how many calls. `missing` is derived from which "
                    "values came out unknown, so it cannot disagree with them; it used to be a "
                    "hand-written list of four names and left `time_filtered_share` null and "
                    "unnamed, which breaks the one promise the set makes. Event carries a "
                    "`timestamptz` so has_time_dimension is true for its group; Label's "
                    "`created_at` is a *string*, so it is false - decided by type and never by a "
                    "name, because a client may hash every identifier and a derivation that read "
                    "names would answer differently with hashing on."
                )
            },
        },
    )


def _two() -> None:
    """Every bucket boundary, fed straight to the histogram.

    The same idea as the ``canonical/`` family: a value handed to one function, with the answer
    written next to it. The reference computes the index as the **bit length of the integer
    quotient** and not as a logarithm, because ``log2`` is not required by IEEE 754 to be correctly
    rounded and one bit at a power-of-two boundary is a different bucket - a different p99 for
    identical traffic, in a number a placement decision is made from.
    """
    boundaries: list[list[int]] = []
    for value in (0, 1, 999, BUCKET_BASE_NS):
        boundaries.append([value, 0 if value < BUCKET_BASE_NS else 1])
    for power in range(0, BUCKET_COUNT + 2):
        edge = BUCKET_BASE_NS * (2**power)
        for value in (edge - 1, edge, edge + 1):
            if value < 0:
                continue
            if value < BUCKET_BASE_NS:
                index = 0
            else:
                index = min(BUCKET_COUNT - 1, (value // BUCKET_BASE_NS).bit_length())
            boundaries.append([value, index])
    # Deduplicated and ordered, so the file reads as a table rather than as a log.
    seen: dict[int, int] = {}
    for value, index in boundaries:
        seen[value] = index
    _write(
        "002-histogram-bucket-boundaries",
        {
            "buckets.json": [[value, seen[value]] for value in sorted(seen)],
            "percentiles.json": {
                "edges_ms": [
                    (BUCKET_BASE_NS * (2**index)) / 1_000_000 for index in range(BUCKET_COUNT)
                ]
            },
            "why.json": {
                "why": (
                    "A duration below one microsecond is bucket 0 and everything above 16.8 "
                    "seconds is bucket 24, clamped. The interesting rows are the three around each "
                    "power of two: an implementation using a logarithm agrees with these until a "
                    "libm rounds the last bit differently, and then disagrees on exactly the "
                    "boundary rows. `edges_ms` is the upper edge each bucket reports as a "
                    "percentile - the *upper* edge, because a placement decision made on an "
                    "optimistic latency figure is the wrong kind of wrong."
                )
            },
        },
    )


def _three() -> None:
    model = _shop()
    by_kind = _by_kind(model)
    # Two read shapes whose mean cardinalities are 2 and 10. Sorted as numbers that is [2, 10] and
    # the median is 10; sorted as strings it is ["10", "2"] and the median is 2.
    operations = [
        {"shape": by_kind[("Event", "point_read")].id, "ns": 1_000, "rows": 2},
        {"shape": by_kind[("Event", "range_read")].id, "ns": 1_000, "rows": 10},
        {"shape": by_kind[("Event", "full_scan")].id, "ns": 1_000, "rows": 300},
    ]
    _write(
        "003-cardinalities-are-sorted-as-numbers",
        {
            "model.json": sde.neutral_declaration(model),
            "operations.json": operations,
            "window.json": _record(model, operations),
            "why.json": {
                "why": (
                    "Mean rows per call come out as 2, 10 and 300. A language whose default sort "
                    "is lexicographic - JavaScript's is - orders them 10, 2, 300 and reports a "
                    "different median and a different p99 for the same traffic. Nothing about the "
                    "shape of the document changes, which is why this needs a vector rather than "
                    "a review: the numbers are plausible either way."
                )
            },
        },
    )


def _four() -> None:
    model = _shop()
    by_kind = _by_kind(model)
    operations = [{"shape": by_kind[("Event", "point_read")].id, "ns": 1_000, "rows": 1}]
    _write(
        "004-a-group-with-no-traffic",
        {
            "model.json": sde.neutral_declaration(model),
            "operations.json": operations,
            "features_for.json": {"Label": _features(model, operations, "Label")},
            "why.json": {
                "why": (
                    "An idle group is not in the window document at all - a document lists the "
                    "groups that were observed - so this case asks for its features directly, "
                    "which is what a caller reporting on every group does. `no_traffic` is the "
                    "*reason* and the unknown fields are named alongside it, by the same "
                    "derivation as every other case: two branches of one function computing "
                    "`missing` by different rules is the defect that set exists to prevent. Note "
                    "what is **not** missing: calls is 0, shape_mix is empty and distinct_shapes "
                    "is 0, because those are measurements and their answer is zero."
                )
            },
        },
    )


def _five() -> None:
    model = _shop()
    by_kind = _by_kind(model)
    operations = [
        {"shape": by_kind[("Event", "write")].id, "ns": 2_000, "rows": 1} for _ in range(4)
    ]
    # The group's name, not the entity's. A relation unions its ends, so the group holding Event is
    # named after the first of its members - which is exactly the sort of thing a hand-written
    # vector gets wrong and a generated one cannot.
    group = sde.group_of(sde.colocation_groups(model), "Event").name
    fan_out = [
        {"group": group, "materialization": f"{group}@ch", "ns": 3_000},
        {"group": group, "materialization": f"{group}@ch", "ns": 5_000},
        {"group": group, "materialization": f"{group}@ch", "ns": 900_000, "failed": True},
        {"group": group, "materialization": f"{group}@arch", "ns": 1_500},
    ]
    _write(
        "005-fan-out-freshness",
        {
            "model.json": sde.neutral_declaration(model),
            "operations.json": operations,
            "fan_out.json": fan_out,
            "window.json": _record(model, operations, fan_out),
            "why.json": {
                "why": (
                    "Two things at once. A fan-out is **not** a shape: read_write_ratio and "
                    "shape_mix here are exactly what they would be without any copy, because "
                    "recording a fan-out as a write would make a group look twice as write-heavy "
                    "for the reason that a copy exists - and the set of kinds that count as writes "
                    "had four copies in one process once. And a failed fan-out is counted in both "
                    "`failures` and the latency histogram: a failed write took time too, and "
                    "dropping it would make the window look better precisely when the copy is in "
                    "trouble. `complete` on a copy is what a lag figure hides - a copy missing a "
                    "thousand rows can have an excellent p99. Copies come out sorted by "
                    "materialisation, so the archive copy precedes the ClickHouse one."
                )
            },
        },
    )


def _six() -> None:
    model = _shop()
    other = model_from_neutral(
        {
            "entities": [
                {
                    "name": "Event",
                    "fields": [{"name": "id", "type": "uuid"}],
                    "key": ["id"],
                }
            ],
            "relations": [],
            "atomic": [],
        }
    )
    by_kind = _by_kind(model)
    operations = [{"shape": by_kind[("Event", "point_read")].id, "ns": 1_000, "rows": 1}]
    _write(
        "006-a-window-refuses-a-model-it-did-not-measure",
        {
            "model.json": sde.neutral_declaration(model),
            "operations.json": operations,
            "against.json": sde.neutral_declaration(other),
            "expected.json": {
                "match": "is being serialised against",
                "why": (
                    "Serialising a window needs exactly one fact from a model - whether a group "
                    "carries a time dimension - and reading it from a different model would attach "
                    "it to the wrong groups and claim `has_time_dimension: false` for a group that "
                    "has one. False is a claim. Refused rather than defaulted, because this "
                    "document is what a placement decision is adjudicated against years later. The "
                    "error class is not pinned: this is a caller mistake rather than a document "
                    "this library was handed, so each language raises whatever it raises for a bad "
                    "argument - the *message* is what a reader needs."
                ),
            },
        },
    )


def _seven() -> None:
    """Two samples at p50, where nearest-rank and the floor of ``p * n`` pick different rows."""
    model = _shop()
    by_kind = _by_kind(model)
    operations = [
        {"shape": by_kind[("Event", "point_read")].id, "ns": 1_200, "rows": 3},
        {"shape": by_kind[("Event", "full_scan")].id, "ns": 5_000, "rows": 300},
    ]
    _write(
        "007-a-percentile-on-an-even-sample",
        {
            "model.json": sde.neutral_declaration(model),
            "operations.json": operations,
            "window.json": _record(model, operations),
            "why.json": {
                "why": (
                    "Which of two samples a percentile picks. Every other case in this family has "
                    "an odd sample count or every sample in one bucket, so nearest-rank - the "
                    "smallest value at least p of the data is below - and floor(p*n) choose the "
                    "same row and the rule was undetermined: a third implementation swapped one "
                    "for the other and the whole family stayed green. They differ exactly when p*n "
                    "is an integer, which is what two samples at p50 is. Nearest-rank reports the "
                    "lower of the two, 0.002 rather than 0.008, and the same rank rule picks 3 "
                    "rather than 300 for the cardinality. Two libraries disagreeing here would "
                    "report a different p50 for identical traffic, which is a number a placement "
                    "decision is made on."
                )
            },
        },
    )


def _eight() -> None:
    """A failed read counts in the histogram and not in the result cardinality."""
    model = _shop()
    by_kind = _by_kind(model)
    operations = [
        {"shape": by_kind[("Event", "point_read")].id, "ns": 1_000, "rows": 10},
        {"shape": by_kind[("Event", "point_read")].id, "ns": 1_000, "rows": 0, "failed": True},
    ]
    _write(
        "008-a-failed-read-is-not-a-cardinality-measurement",
        {
            "model.json": sde.neutral_declaration(model),
            "operations.json": operations,
            "window.json": _record(model, operations),
            "why.json": {
                "why": (
                    "A failed call counts in the latency histogram and not in the result "
                    "cardinality, and the two answers have different reasons rather than one "
                    "convention. A failure took time, so dropping it from the histogram would make "
                    "a window look better precisely when the engine is in trouble - which is the "
                    "rule 005 states for a failed fan-out. It returned no rows because it failed, "
                    "not because the data is sparse, so averaging that zero in understates how "
                    "many rows a read of this shape actually returns, and error_share already "
                    "carries the failure. Ten, not five. Neither answer was pinned before: both "
                    "passed every case in this family."
                )
            },
        },
    )


def _nine() -> None:
    """Which field a range read ranged over: the one fact only the ``shapes`` section carries.

    Two ordered fields in one entity, ranged over at different rates. The group's features see one
    number for both - ``shape_mix.range_read`` - so a key order or a partition chosen from them
    would be chosen blind; the per-shape entries tell the two fields apart. The relation walk is
    here for ``target``, which is emitted on that kind only.
    """
    sde.clear_registry()
    model = model_from_neutral(
        {
            "entities": [
                {
                    "name": "Reading",
                    "fields": [
                        {"name": "at", "type": "timestamptz"},
                        {"name": "seq", "type": "int64"},
                        {"name": "station", "type": "string"},
                        {"name": "temperature", "type": "float64"},
                    ],
                    "key": ["station", "at"],
                },
                {
                    "name": "Station",
                    "fields": [
                        {"name": "id", "type": "string"},
                        {"name": "name", "type": "string"},
                    ],
                    "key": ["id"],
                },
            ],
            "relations": [{"name": "station", "from": "Reading", "to": "Station"}],
            "atomic": [],
        }
    )
    shapes = {(s.entity, s.kind, s.fields): s for s in sde.enumerate_shapes(model)}

    def on(entity: str, kind: str, *fields: str) -> str:
        return shapes[(entity, kind, tuple(fields))].id

    operations = [
        *[{"shape": on("Reading", "range_read", "at"), "ns": 3_000, "rows": 40} for _ in range(5)],
        {"shape": on("Reading", "range_read", "at"), "ns": 90_000, "rows": 0, "failed": True},
        {"shape": on("Reading", "range_read", "seq"), "ns": 7_000, "rows": 2},
        *[
            {"shape": on("Reading", "point_read", "at", "station"), "ns": 900, "rows": 1}
            for _ in range(3)
        ],
        {"shape": on("Reading", "relation_walk", "station"), "ns": 2_000, "rows": 1},
        {"shape": on("Reading", "bulk_write"), "ns": 150_000, "rows": 250},
    ]
    _write(
        "009-which-field-a-range-read-ranged-over",
        {
            "model.json": sde.neutral_declaration(model),
            "operations.json": operations,
            "window.json": _record(model, operations),
            "why.json": {
                "why": (
                    "Per-shape measurements, in the model's enumeration order: identifier, entity, "
                    "kind and fields from the model's own enumeration - never from what the "
                    "recorder was told - and calls, errors, rows and the two latency edges from "
                    "the recording. The group's features report one range-read share for two "
                    "fields; only these entries say that six range reads went over `at` and one "
                    "over `seq`, which is the fact a key order or a partition is chosen from. "
                    "`rows` is the total the calls returned or wrote - a bulk write counts its "
                    "rows - and a failed call is counted in `calls`, `errors` and the latency, as "
                    "everywhere else. `target` appears on the relation walk only, absent rather "
                    "than null elsewhere, for the reason `copies` is absent rather than empty."
                )
            },
        },
    )


def _ten() -> None:
    """What a read filtered on, by name: the evidence a key order is chosen from.

    One range read over ``at`` is the same shape whether or not the query also fixed ``station`` by
    equality, and the two want different key orders - ``(station, at)`` serves the first and
    ``(at, station)`` the second. An aggregate over a time range and one over the whole table are
    one shape too. Each read now says what it filtered on - the equality fields and the bounded
    field - so an agent can tell them apart instead of guessing.
    """
    sde.clear_registry()
    model = model_from_neutral(
        {
            "entities": [
                {
                    "name": "Reading",
                    "fields": [
                        {"name": "at", "type": "timestamptz"},
                        {"name": "station", "type": "string"},
                        {"name": "temperature", "type": "float64"},
                    ],
                    "key": ["station", "at"],
                }
            ],
            "relations": [],
            "atomic": [],
        }
    )
    shapes = {(s.kind, s.fields): s for s in sde.enumerate_shapes(model)}
    ranged = shapes[("range_read", ("at",))].id
    aggregate = shapes[("aggregate", ())].id
    scan = shapes[("full_scan", ())].id
    operations = [
        *[
            {"shape": ranged, "equal": ["station"], "range": "at", "ns": 3_000, "rows": 12}
            for _ in range(4)
        ],
        {"shape": ranged, "equal": [], "range": "at", "ns": 90_000, "rows": 900},
        # Recorded out of order on purpose: the entries sort by the equality fields' contents,
        # then by the bounded field, and a field set recorded first does not come first.
        {"shape": aggregate, "equal": ["temperature"], "range": "at", "ns": 7_000, "rows": 1},
        {"shape": aggregate, "equal": [], "range": "at", "ns": 60_000, "rows": 1},
        {"shape": aggregate, "equal": ["station"], "range": "at", "ns": 8_000, "rows": 1},
        {"shape": aggregate, "equal": [], "ns": 250_000, "rows": 1},
        {"shape": scan, "equal": [], "ns": 400_000, "rows": 5000},
        # A write takes no filter and reports none.
        {"shape": shapes[("write", ())].id, "ns": 2_000, "rows": 1},
    ]
    _write(
        "010-what-a-read-filtered-on",
        {
            "model.json": sde.neutral_declaration(model),
            "operations.json": operations,
            "window.json": _record(model, operations),
            "why.json": {
                "why": (
                    "Each read shape reports what its calls filtered on: `filtered_on` has one "
                    "entry per combination of equality fields (`equal`, sorted) and bounded field "
                    "(`range`, absent when the call bounded nothing), with the number of calls, in "
                    "code point order. Four range reads over `at` also fixed `station` and one did "
                    "not; four aggregates - over the whole table, over a time range, and over a "
                    "time range fixing `station` or `temperature` - are one shape with four "
                    "entries, "
                    "sorted by the equality fields' contents and then by the bounded field rather "
                    "than in the order they were recorded; a full scan that filtered on nothing "
                    "says so with an empty "
                    "`equal`. Names only, never a value. A write takes no filter and has no "
                    "`filtered_on` at all - absent, not empty, because it was never asked."
                )
            },
        },
    )


def _stations() -> sde.LogicalModel:
    """Three groups: readings with a time and a number, stations and labels with neither."""
    sde.clear_registry()
    return model_from_neutral(
        {
            "entities": [
                {
                    "name": "Label",
                    "fields": [
                        {"name": "name", "type": "string"},
                        {"name": "weight", "type": "int64"},
                    ],
                    "key": ["name"],
                },
                {
                    "name": "Reading",
                    "fields": [
                        {"name": "at", "type": "timestamptz"},
                        {"name": "station", "type": "string"},
                        {"name": "temperature", "type": "int64"},
                    ],
                    "key": ["station", "at"],
                },
                {
                    "name": "Station",
                    "fields": [
                        {"name": "code", "type": "string"},
                        {"name": "height", "type": "int64"},
                    ],
                    "key": ["code"],
                },
            ]
        }
    )


def _on(model: sde.LogicalModel, entity: str, kind: str, *fields: str) -> str:
    return next(
        s.id
        for s in sde.enumerate_shapes(model)
        if s.entity == entity and s.kind == kind and tuple(s.fields) == fields
    )


def _eleven() -> None:
    """Which calls filtered on time: a range or an equality on a field of a time *type*."""
    model = _stations()
    ranged = _on(model, "Reading", "range_read", "at")
    by_number = _on(model, "Reading", "range_read", "temperature")
    aggregate = _on(model, "Reading", "aggregate")
    operations = [
        *[{"shape": ranged, "equal": ["station"], "range": "at", "ns": 3_000, "rows": 12}] * 3,
        {"shape": ranged, "equal": [], "range": "at", "ns": 9_000, "rows": 90},
        {"shape": aggregate, "equal": ["at"], "ns": 5_000, "rows": 1},
        {"shape": aggregate, "equal": ["station"], "ns": 5_000, "rows": 1},
        *[{"shape": by_number, "equal": [], "range": "temperature", "ns": 4_000, "rows": 7}] * 2,
        {"shape": _on(model, "Reading", "full_scan"), "equal": [], "ns": 90_000, "rows": 500},
        *[{"shape": _on(model, "Reading", "point_read", "at", "station"), "ns": 900, "rows": 1}]
        * 2,
        *[{"shape": _on(model, "Reading", "write"), "ns": 2_000, "rows": 1}] * 2,
        {"shape": _on(model, "Station", "full_scan"), "equal": ["height"], "ns": 7_000, "rows": 3},
        {"shape": _on(model, "Station", "point_read", "code"), "ns": 800, "rows": 1},
    ]
    _write(
        "011-what-filtered-on-time",
        {
            "model.json": sde.neutral_declaration(model),
            "operations.json": operations,
            "window.json": _record(model, operations),
            "why.json": {
                "why": (
                    "time_filtered_share counts the calls whose filter bounded a field of a time "
                    "type by a range or compared one by equality - names only, from what "
                    "`filtered_on` already reports - over every call of the group, as "
                    "pk_access_share does. Reading: three ranges over `at` fixing `station`, one "
                    "without it, and an aggregate comparing `at` - 5 of 13. A range over "
                    "`temperature` is a range over a number; an aggregate fixing only `station`, "
                    "a scan filtering on nothing, point reads and writes did not filter on time. "
                    "Station has no field of a time type, so its share is a measured zero, not an "
                    "unknown. The clock stands still here, so every write lands in second zero "
                    "and write_burstiness is 1."
                )
            },
        },
    )


def _twelve() -> None:
    """What a group occupies: the latest storage sample of the window, and its index share."""
    model = _stations()
    operations = [
        {"shape": _on(model, "Reading", "write"), "ns": 2_000, "rows": 1},
        {"shape": _on(model, "Station", "write"), "ns": 2_000, "rows": 1},
        {"shape": _on(model, "Label", "write"), "ns": 2_000, "rows": 1},
        {"event": "storage", "at_ms": 1_000, "group": "Reading", "total_bytes": 900_000,
         "secondary_index_bytes": 300_000},
        {"event": "storage", "at_ms": 2_000, "group": "Reading", "total_bytes": 1_000_000,
         "secondary_index_bytes": 250_000},
        {"event": "storage", "at_ms": 2_000, "group": "Station", "total_bytes": 0,
         "secondary_index_bytes": 0},
    ]
    _write(
        "012-what-a-group-occupies",
        {
            "model.json": sde.neutral_declaration(model),
            "operations.json": operations,
            "clock.json": {"window_ms": 3_000},
            "window.json": _record(model, operations, window_ms=3_000),
            "why.json": {
                "why": (
                    "total_bytes is the latest storage sample the group took in the window - "
                    "Reading's second one, 1 000 000 bytes - and index_to_table_ratio its "
                    "secondary index bytes over the rest: 250 000 / 750 000. An empty group is "
                    "a measured 0 bytes whose ratio divides by nothing, so the ratio is unknown "
                    "and the size is not. Label took no sample: both unknown. Two samples a "
                    "second apart project no growth - an hour is the shortest span a day is "
                    "projected from."
                )
            },
        },
    )


def _thirteen() -> None:
    """Growth projected to a day: across windows, never past a day, truncated toward zero."""
    model = _stations()
    hour = 3_600_000
    writes = [
        {"shape": _on(model, entity, "write"), "ns": 2_000, "rows": 1}
        for entity in ("Label", "Reading", "Station")
    ]
    operations = [
        {"at_ms": 0, **writes[1]},
        {"event": "storage", "at_ms": 0, "group": "Reading", "total_bytes": 1_000_000,
         "secondary_index_bytes": 0},
        {"event": "roll", "at_ms": 600_000},
        {"at_ms": 2 * hour, **writes[1]},
        {"event": "storage", "at_ms": 2 * hour, "group": "Reading", "total_bytes": 1_100_000,
         "secondary_index_bytes": 0},
        {"event": "roll", "at_ms": 3 * hour},
        {"at_ms": 18 * hour, **writes[2]},
        {"event": "storage", "at_ms": 18 * hour, "group": "Station", "total_bytes": 100,
         "secondary_index_bytes": 0},
        {"at_ms": 24 * hour + 1_800_000, **writes[0]},
        {"event": "storage", "at_ms": 24 * hour + 1_800_000, "group": "Label",
         "total_bytes": 5_000, "secondary_index_bytes": 0},
        {"at_ms": 25 * hour, **writes[1]},
        {"event": "storage", "at_ms": 25 * hour, "group": "Reading", "total_bytes": 2_000_000,
         "secondary_index_bytes": 0},
        {"event": "storage", "at_ms": 25 * hour, "group": "Station", "total_bytes": 99,
         "secondary_index_bytes": 0},
        {"event": "storage", "at_ms": 25 * hour, "group": "Label", "total_bytes": 6_000,
         "secondary_index_bytes": 0},
    ]
    window_ms = 25 * hour + 60_000
    _write(
        "013-growth-projected-to-a-day",
        {
            "model.json": sde.neutral_declaration(model),
            "operations.json": operations,
            "clock.json": {"window_ms": window_ms},
            "window.json": _record(model, operations, window_ms=window_ms),
            "why.json": {
                "why": (
                    "daily_growth_bytes projects the change between the oldest storage sample "
                    "kept and the window's latest to a day, in integers, truncated toward zero. "
                    "The recorder keeps a group's samples for a day across windows: Reading's "
                    "first sample, 25 hours before the last, has been dropped, so its growth is "
                    "measured from the 2-hour sample of an earlier window - 900 000 bytes in 23 "
                    "hours is 939 130.43 a day, pinned as 939 130. Station shrank by one byte in "
                    "7 hours: -3.43 a day truncates to -3, not -4 (ClickHouse merges do shrink "
                    "parts). Label's two samples are 30 minutes apart, too short a span to "
                    "project a day from, so its growth is unknown while its size is not. Each "
                    "group wrote one row in a last window of 22 hours and a minute, so its "
                    "write_burstiness is 79 260: one busy second in that many."
                )
            },
        },
    )


def _fourteen() -> None:
    """How writes arrive: the busiest second's rows against the window's mean rate."""
    model = _stations()
    reading_write = _on(model, "Reading", "write")
    reading_bulk = _on(model, "Reading", "bulk_write")
    label_write = _on(model, "Label", "write")
    operations = [
        {"at_ms": 0, "shape": reading_write, "ns": 2_000, "rows": 1},
        {"at_ms": 0, "shape": reading_bulk, "ns": 90_000, "rows": 49},
        {"at_ms": 1_500, "shape": reading_write, "ns": 2_000, "rows": 1},
        {"at_ms": 1_800, "shape": reading_bulk, "ns": 90_000, "rows": 100, "failed": True},
        {"at_ms": 4_200, "shape": reading_write, "ns": 2_000, "rows": 2},
        *[{"at_ms": 1_000 * second, "shape": label_write, "ns": 2_000, "rows": 5}
          for second in range(5)],
        {"at_ms": 5_000, "shape": _on(model, "Station", "point_read", "code"), "ns": 800,
         "rows": 1},
    ]
    _write(
        "014-how-writes-arrive",
        {
            "model.json": sde.neutral_declaration(model),
            "operations.json": operations,
            "clock.json": {"window_ms": 9_500},
            "window.json": _record(model, operations, window_ms=9_500),
            "why.json": {
                "why": (
                    "write_burstiness is M * S / W: the rows of the busiest second, the window's "
                    "whole seconds and every row written by a successful write. The window lasts "
                    "9.5 s, so S is 10. Reading wrote 50 rows in second 0, 1 in second 1 and 2 in "
                    "second 4 - 50 * 10 / 53; the failed bulk write at 1.8 s wrote nothing, "
                    "whatever rows it was recorded with. Label wrote 5 rows in each of its first "
                    "five seconds and nothing in the other five, which reads 2, not 1: idle time "
                    "is part of how writes arrive. Station only read, so its burstiness is "
                    "unknown, not zero."
                )
            },
        },
    )


def _fifteen() -> None:
    """A read that does not report its filters: unknown, unless its shape says what it bounded."""
    model = _stations()
    operations = [
        # Reading: two range reads over `at` and one over `temperature`, none reporting filters.
        *[{"shape": _on(model, "Reading", "range_read", "at"), "ns": 3_000, "rows": 10}] * 2,
        {"shape": _on(model, "Reading", "range_read", "temperature"), "ns": 3_000, "rows": 10},
        {"shape": _on(model, "Reading", "point_read", "at", "station"), "ns": 900, "rows": 1},
        # Station: a scan without reported filters, in an entity with no field of a time type.
        {"shape": _on(model, "Station", "full_scan"), "ns": 50_000, "rows": 30},
        # Label: an aggregate without reported filters - Label has no time field either.
        {"shape": _on(model, "Label", "aggregate"), "ns": 20_000, "rows": 1},
    ]
    _write(
        "015-reads-that-did-not-report-their-filters",
        {
            "model.json": sde.neutral_declaration(model),
            "operations.json": operations,
            "window.json": _record(model, operations),
            "why.json": {
                "why": (
                    "A read that takes a `where` and records no filters - another producer, or a "
                    "recorder driven by hand - leaves time_filtered_share unknown, because zero "
                    "would claim to know what it filtered on. A range read is the exception: its "
                    "shape names the field it bounded, so Reading's two unreported range reads "
                    "over `at` count and the one over `temperature` does not - 2 of 4. Station "
                    "and Label have no field of a time type, so their unreported scan and "
                    "aggregate could not have filtered on time: a measured 0. Case 016 is the "
                    "unknown."
                )
            },
        },
    )


def _sixteen() -> None:
    """An unreported aggregate in an entity with a time field: the share is unknown."""
    model = _stations()
    operations = [
        {"shape": _on(model, "Reading", "range_read", "at"), "equal": ["station"], "range": "at",
         "ns": 3_000, "rows": 10},
        {"shape": _on(model, "Reading", "aggregate"), "ns": 20_000, "rows": 1},
    ]
    _write(
        "016-an-unreported-aggregate-over-a-time-entity",
        {
            "model.json": sde.neutral_declaration(model),
            "operations.json": operations,
            "window.json": _record(model, operations),
            "why.json": {
                "why": (
                    "Reading has a field of a time type and an aggregate whose filters were not "
                    "reported: it may have bounded `at` or not, and its shape does not say. So "
                    "time_filtered_share is unknown - in `missing` - even though the one reported "
                    "range read did filter on time. Half, or one, would be a guess."
                )
            },
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-am-changing-the-contract", action="store_true")
    args = parser.parse_args()
    if not args.i_am_changing_the_contract:
        print(__doc__)
        print("Refusing to run without --i-am-changing-the-contract.")
        return 1
    VECTORS.mkdir(parents=True, exist_ok=True)
    print("telemetry vectors:")
    _one()
    _two()
    _three()
    _four()
    _five()
    _six()
    _seven()
    _eight()
    _nine()
    _ten()
    _eleven()
    _twelve()
    _thirteen()
    _fourteen()
    _fifteen()
    _sixteen()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

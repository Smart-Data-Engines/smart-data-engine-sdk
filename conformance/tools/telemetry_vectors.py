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

**The document carries no clock.** A window's start and end are readings of a monotonic clock with
no meaning outside the process that took them, and the two features that would need a duration -
growth per day and write burstiness - are declared unmeasurable anyway. So the record is
deterministic, which is what lets these vectors exist.

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


def _record(
    model: sde.LogicalModel,
    operations: list[dict[str, Any]],
    fan_out: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Feed the reference implementation and take the document it produces."""
    by_id = {s.id: s for s in sde.enumerate_shapes(model)}
    recorder = sde.Recorder(model.version)
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
    for entry in fan_out or ():
        recorder.record_fan_out(
            group=str(entry["group"]),
            materialization=str(entry["materialization"]),
            nanoseconds=int(entry["ns"]),
            failed=bool(entry.get("failed", False)),
        )
    window = recorder.roll()
    assert window is not None, "the case recorded nothing, so it would pin nothing"
    return window.as_record(model)


def _features(model: sde.LogicalModel, operations: list[dict[str, Any]], group: str) -> Any:
    by_id = {s.id: s for s in sde.enumerate_shapes(model)}
    recorder = sde.Recorder(model.version)
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
        *[
            {"shape": by_kind[("Event", "point_read")].id, "ns": 1_200, "rows": 1}
            for _ in range(5)
        ],
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

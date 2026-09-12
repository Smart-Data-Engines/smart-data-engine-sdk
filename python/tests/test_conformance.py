"""The conformance runner.

This is the test that every SDE library has, in its own runner, over the same vectors. It is short
on purpose: a runner with logic of its own is a second implementation, and the whole point is that
all four implementations are compared against one fixed set of expected bytes rather than against
each other.

One rule worth defending, because it looks pedantic and is not: ``ir.json`` is compared as
**bytes**. Parsing it and comparing structures would pass for two libraries that agree on the
structure while disagreeing on key order or Unicode normalisation - which is precisely the failure
these vectors exist to catch, and it is invisible the moment you parse.
"""

from __future__ import annotations

import json
import re
from base64 import b64decode
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import sde
import sde.telemetry
from sde.canonical import CanonicalError, canonical_bytes
from sde.errors import (
    DeclarationError,
    EngineError,
    MapError,
    MapRolledBack,
    MigrationRefused,
)
from sde.hashing import hash_identifiers
from sde.testing.loader import model_from_neutral
from sde.testing.memory import engines_from

VECTORS = Path(__file__).resolve().parents[2] / "conformance" / "vectors"
CONTRACT_FILE = Path(__file__).resolve().parents[2] / "conformance" / "contract-version.txt"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _cases(kind: str) -> list[Path]:
    root = VECTORS / kind
    return sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []


def _ident(path: Path) -> str:
    return f"{path.parent.name}/{path.name}"


def test_the_library_implements_the_vectors_contract_version() -> None:
    # A library running vectors from a different contract version is comparing itself against rules
    # it does not implement, and every failure after that is noise.
    assert int(CONTRACT_FILE.read_text().strip()) == sde.CONTRACT


def test_there_are_vectors_at_all() -> None:
    # Guards against the runner silently passing because a path moved. A green suite that ran zero
    # vectors is worse than a red one.
    assert _cases("model"), "no model vectors found"
    assert _cases("routing"), "no routing vectors found"
    assert _cases("errors"), "no error vectors found"


@pytest.mark.parametrize("case", _cases("model"), ids=_ident)
def test_model_vector(case: Path) -> None:
    model = model_from_neutral(_read_json(case / "model.json"))

    expected_ir = (case / "ir.json").read_bytes()
    assert canonical_bytes(model.ir) == expected_ir, (
        "canonical IR differs from the vector. Compare the two byte strings, not the parsed "
        "documents: the difference is usually key order or Unicode normalisation."
    )

    assert model.version == (case / "version.txt").read_text().strip()

    groups = [
        {"name": g.name, "members": list(g.members)} for g in sde.colocation_groups(model)
    ]
    assert groups == _read_json(case / "groups.json")

    shapes_file = case / "shapes.json"
    if shapes_file.exists():
        shapes = [{**s.as_ir(), "id": s.id} for s in sde.enumerate_shapes(model)]
        assert shapes == _read_json(shapes_file)


@pytest.mark.parametrize("case", _cases("routing"), ids=_ident)
def test_routing_vector(case: Path) -> None:
    model = model_from_neutral(_read_json(case / "model.json"))
    placement = sde.load_map(_read_json(case / "map.json"), model=model)
    by_id = {s.id: s for s in sde.enumerate_shapes(model)}

    for expectation in _read_json(case / "cases.json"):
        shape = by_id.get(expectation["shape"])
        assert shape is not None, (
            f"the vector refers to shape {expectation['shape']} which this library does not "
            "enumerate. Either the enumeration diverged or the vector is stale."
        )
        got = sde.resolve(
            placement,
            shape,
            in_write_transaction=bool(expectation.get("in_write_transaction")),
            fresh=bool(expectation.get("fresh")),
        )
        assert got.id == expectation["expect"], (
            f"{shape.entity}.{shape.kind} resolved to {got.id}, vector expects "
            f"{expectation['expect']}"
        )

    # Optional, and present only for a map that fans writes out. Asserted here rather than in a
    # Python-only test because a fan-out target read differently in two languages is a row written
    # to one copy and not the other - the divergence class this whole suite exists for, and the one
    # that produces no error at the time it happens.
    fan_out = case / "also_write.json"
    if fan_out.is_file():
        for group, expected in sorted(_read_json(fan_out).items()):
            got_ids = [m.id for m in placement.placement_of(group).also_write]
            assert got_ids == expected, (
                f"group {group} fans writes out to {got_ids}, vector expects {expected}"
            )


def _materialization(placement: sde.PlacementMap, mat_id: str) -> tuple[str, Any]:
    """The materialisation with this id, and the group it belongs to.

    Searched across every group rather than taken from a field in the case, because a map's ids are
    unique across the whole document - ``errors/013`` is the vector that pins that - so a case
    naming the group as well would carry a fact the map already carries.
    """
    for name, group in sorted(placement.groups.items()):
        for materialization in group.all():
            if materialization.id == mat_id:
                return name, materialization
    raise AssertionError(f"the vector names materialisation {mat_id!r}, which this map has no")


@pytest.mark.parametrize("case", _cases("schema"), ids=_ident)
def test_schema_vector(case: Path) -> None:
    """Tier 2, first half: the DDL a layout renders to, byte for byte.

    The input reaches the renderer through the same map loader production uses - a whole
    ``map.json`` rather than a bare layout document - so a case can only pin DDL for a layout this
    library would accept in the first place, and there is no second parser written for the vectors.

    Statements are compared **exactly**, because they are bytes an engine receives. Refusals are
    compared by substring, because they are diagnostics, exactly as the ``errors/`` family does.
    """
    model = model_from_neutral(_read_json(case / "model.json"))
    placement = sde.load_map(_read_json(case / "map.json"), model=model)

    for expectation in _read_json(case / "cases.json"):
        group, materialization = _materialization(placement, expectation["materialization"])
        layout = materialization.layout
        if "layout_columns" in expectation:
            # The one place a case edits its own input. `columns` is optional in the document, so a
            # map naming tables alone loads cleanly - and once it has loaded there is no other way
            # to express "the map said nothing about columns".
            layout = replace(layout, columns=expectation["layout_columns"])
        members = next(g for g in sde.colocation_groups(model) if g.name == group)
        keys: Mapping[str, Sequence[str]] = expectation.get("keys") or {
            name: list(model.entity(name).key) for name in members.members
        }
        dialect = expectation["dialect"]

        if "fixed" in expectation:
            assert sde.schema_is_fixed(dialect) is expectation["fixed"], (
                f"schema_is_fixed({dialect!r}) disagrees with the vector. An engine that imposes "
                f"its own schema renders no DDL, and 'no statements' has to be distinguishable "
                f"from 'no tables in this layout'."
            )
        else:
            want = expectation["fixed_error"]
            with pytest.raises(_ERRORS[want["error"]], match=re.escape(want["match"])):
                sde.schema_is_fixed(dialect)

        if "error" in expectation:
            with pytest.raises(
                _ERRORS[expectation["error"]], match=re.escape(expectation["match"])
            ):
                sde.schema_statements(layout, keys=keys, dialect=dialect)
            continue

        statements = list(sde.schema_statements(layout, keys=keys, dialect=dialect))
        assert statements == expectation["statements"], (
            f"{_ident(case)} [{materialization.id} as {dialect}]: the DDL differs from the "
            f"vector. Two libraries that agree on a map and disagree here place one entity in "
            f"tables with different columns, and each of them created a table successfully."
        )

        views = expectation.get("views")
        if views is not None:
            rendered = sde.compatibility_views(layout, was=views["was"], dialect=dialect)
            assert list(rendered.create) == views["create"]
            assert list(rendered.drop) == views["drop"]
            assert rendered.complete is views["complete"]
            assert [entity for entity, _ in rendered.not_possible] == [
                entry["entity"] for entry in views["not_possible"]
            ]
            for (entity, why), entry in zip(
                rendered.not_possible, views["not_possible"], strict=True
            ):
                for fragment in entry["match"]:
                    assert fragment in why, (
                        f"the reason {entity} cannot have a view does not contain "
                        f"{fragment!r}. A reason may say more than the vector, in any language, "
                        f"and may not say less."
                    )


def _recorded(model: sde.LogicalModel, operations: list[dict[str, Any]], case: Path) -> sde.Window:
    """Feed a case's operations to a recorder and close the window.

    Shapes are named by **identifier**, like the ``routing/`` cases, so the runner has to enumerate
    the model and look them up - which means the group, the entity and the kind reach the recorder
    from this library's own shape enumeration rather than from the vector. A case cannot therefore
    pin a classification by asserting it in its own input.
    """
    by_id = {shape.id: shape for shape in sde.enumerate_shapes(model)}
    recorder = sde.Recorder(model.version)
    for operation in operations:
        shape = by_id.get(operation["shape"])
        assert shape is not None, (
            f"{_ident(case)} refers to shape {operation['shape']}, which this library does not "
            "enumerate. Either the enumeration diverged or the vector is stale."
        )
        recorder.record(
            shape_id=shape.id,
            group=shape.group,
            entity=shape.entity,
            kind=shape.kind,
            nanoseconds=int(operation["ns"]),
            rows=int(operation.get("rows", 0)),
            failed=bool(operation.get("failed", False)),
        )
    fan_out = case / "fan_out.json"
    if fan_out.is_file():
        for entry in _read_json(fan_out):
            recorder.record_fan_out(
                group=str(entry["group"]),
                materialization=str(entry["materialization"]),
                nanoseconds=int(entry["ns"]),
                failed=bool(entry.get("failed", False)),
            )
    window = recorder.roll()
    assert window is not None, f"{_ident(case)} recorded nothing, so it pins nothing"
    return window


@pytest.mark.parametrize("case", _cases("telemetry"), ids=_ident)
def test_telemetry_vector(case: Path) -> None:
    """Tier 1: the window document, and every derivation behind it.

    **Compared as numbers rather than as bytes, and that is the one exception in this suite.**
    Section 1 of the contract rejects floating point outright because a float's textual form differs
    between languages, and almost every number in a window is a float. The document is not signed,
    not hashed and never compared for equality, so the rule does not apply - and the property that
    makes this family checkable is narrower: every number here is either a ratio of two integers or
    a bucket edge divided by a million, and IEEE 754 requires division to be correctly rounded. Two
    languages compute the same double; only their printing differs.
    """
    boundaries = case / "buckets.json"
    if boundaries.is_file():
        edges = _read_json(case / "percentiles.json")["edges_ms"]
        assert len(edges) == sde.telemetry.BUCKET_COUNT
        for nanoseconds, index in _read_json(boundaries):
            histogram = sde.Histogram()
            histogram.record(int(nanoseconds))
            landed = [i for i, hits in enumerate(histogram.buckets) if hits]
            assert landed == [index], (
                f"{nanoseconds} ns landed in bucket(s) {landed}, the vector says {index}. An "
                "implementation computing this with a logarithm agrees until a libm rounds the "
                "last bit differently, and then disagrees on exactly the boundary rows."
            )
            assert histogram.percentile_ms(0.5) == edges[index], (
                "the percentile a single sample reports is the upper edge of its bucket"
            )

    operations_file = case / "operations.json"
    if not operations_file.is_file():
        return

    model = model_from_neutral(_read_json(case / "model.json"))
    operations = _read_json(operations_file)

    expected_error = case / "expected.json"
    if expected_error.is_file():
        want = _read_json(expected_error)
        window = _recorded(model, operations, case)
        against = model_from_neutral(_read_json(case / "against.json"))
        # The class is deliberately not pinned - see the vector's own note. A caller handing this
        # the wrong model is a caller mistake rather than a document the library was given, so each
        # language raises whatever it raises for a bad argument and the message is what a reader
        # needs.
        with pytest.raises(Exception, match=re.escape(want["match"])):
            window.as_record(against)
        return

    window = _recorded(model, operations, case)
    document = case / "window.json"
    if document.is_file():
        assert window.as_record(model) == _read_json(document), (
            f"{_ident(case)}: the window document differs from the vector. Two libraries "
            "disagreeing here hand the planner different features for identical traffic, and "
            "nothing raises - the numbers are plausible either way."
        )

    explicit = case / "features_for.json"
    if explicit.is_file():
        for group, expected in sorted(_read_json(explicit).items()):
            members = next(g for g in sde.colocation_groups(model) if g.name == group)
            got = window.features(
                group, has_time_dimension=sde.has_time_dimension(model, members)
            )
            assert got.as_record() == expected, f"features for {group} differ from the vector"


def _placement_of(case: Path, model: sde.LogicalModel) -> sde.PlacementMap:
    """Load a case's map, with the signature options the case names.

    Signed maps appear in this family because the forward-only check only applies to one: an
    unsigned map is the client's own document and there is no newest version for us to be the
    authority on.
    """
    load = _read_json(case / "load.json") if (case / "load.json").is_file() else {}
    keys = _read_json(case / "keys.json") if (case / "keys.json").is_file() else {}
    named = load.get("public_key")
    return sde.load_map(
        _read_json(case / "map.json"),
        model=model,
        public_key=b64decode(keys[named]) if named else None,
        require_signature=bool(load.get("require_signature", False)),
    )


class _Rollback(Exception):
    """How a case asks its transaction to roll back.

    An exception raised by the runner rather than a flag on the session, because a rollback is what
    an application's own failure looks like: the guarantee under test is that a transaction which
    does not complete leaves nothing in the copy, and the only honest way to reach it is to fail.
    """


def _drive_session(
    case: Path,
    model: sde.LogicalModel,
    placement: sde.PlacementMap,
    engines: Mapping[str, Any],
) -> None:
    """Run a case's session operations and compare what each engine ended up holding.

    The heart of Tier 2 and the part no document transformation can reach: a fan-out is a second
    write in the client's own process, and every rule about it is about *when* it happens.
    """
    recorder = sde.Recorder(model.version)
    session = sde.Session(model, placement, engines, recorder=recorder)

    def run(steps: list[dict[str, Any]]) -> None:
        for step in steps:
            if step["op"] == "save":
                session.save(step["entity"], step["values"])
            elif step["op"] == "transaction":
                try:
                    with session.transaction(*step.get("entities", ())):
                        run(step["body"])
                        if step.get("rollback"):
                            raise _Rollback
                except _Rollback:
                    pass
            else:
                raise AssertionError(f"unknown operation {step['op']!r}")

    run(_read_json(case / "operations.json"))

    expected_tables = _read_json(case / "tables.json")
    got_tables = {
        name: {
            table: sorted(
                (dict(row) for row in rows), key=lambda row: json.dumps(row, sort_keys=True)
            )
            for table, rows in sorted(engine.tables.items())
        }
        for name, engine in sorted(engines.items())
    }
    assert got_tables == expected_tables, (
        f"{_ident(case)}: the engines do not hold what the vector says. A fan-out written at the "
        "wrong moment loses exactly the rows a migration exists not to lose, and nothing raises."
    )

    window = recorder.roll()
    group = next(iter(sorted(placement.groups)))
    copies = [] if window is None else list(window.copies(group))
    expected_copies = _read_json(case / "copies.json")
    assert len(copies) == len(expected_copies)
    for copy, want in zip(copies, expected_copies, strict=True):
        record = copy.as_record()
        for measured in ("lag_p50_ms", "lag_p99_ms"):
            # Elapsed time, so the vector carries a placeholder. What is a property of the library
            # rather than of the clock is that the field is there and is a number or null.
            assert want[measured] == "<any>"
            assert record[measured] is None or isinstance(record[measured], float)
            record[measured] = "<any>"
        assert record == want



@pytest.mark.parametrize("case", _cases("migration"), ids=_ident)
def test_migration_vector(case: Path) -> None:
    """Tier 2, second half: taking part in a migration.

    Two things are pinned and the second is the unusual one. **The record** is what a gate on our
    side reads - a backfill's progress, a verify's seven counts. **The calls** are how that record
    was obtained: a library that reached the same counts by scanning the whole table and filtering
    in memory would satisfy every number and be unusable on a real one.

    The fixture is the library's own in-memory engine rather than one written here, because a runner
    that writes its own is a runner whose *fixture* can be the thing that differs - and then a red
    vector says "one of two tables disagreed" instead of "one of two libraries disagreed".
    """
    model = model_from_neutral(_read_json(case / "model.json"))
    placement = _placement_of(case, model)
    engines = engines_from(_read_json(case / "engines.json"))

    watermark = case / "watermark.json"
    if watermark.is_file():
        want = _read_json(watermark)
        if "error" in want:
            with pytest.raises(_ERRORS[want["error"]], match=re.escape(want["match"])):
                sde.enforce_forward_only(placement, engines)
        else:
            got = sde.enforce_forward_only(placement, engines)
            record = {key: value for key, value in got.as_record().items() if key != "why"}
            assert record == want["expect"], (
                f"{_ident(case)}: the forward-only check disagrees with the vector. This decides "
                "whether a client can be silently reverted to a previous placement."
            )
            # `why` is prose, so it is pinned by substring like every other diagnostic here.
            for fragment in want["why_match"]:
                assert fragment in got.why, (
                    f"the explanation does not contain {fragment!r}. A protection whose state "
                    "cannot be read is a protection taken on trust, so the sentence is part of "
                    "what this check produces."
                )

    session: sde.Session | None = None
    backfill = case / "backfill.json"
    if backfill.is_file():
        want = _read_json(backfill)
        session = sde.Session(model, placement, engines)
        options = want.get("options") or {}
        if "error" in want:
            with pytest.raises(_ERRORS[want["error"]], match=re.escape(want["match"])):
                sde.backfill(session, want["group"], **options)
        else:
            progress = sde.backfill(session, want["group"], **options)
            assert progress.as_record() == want["progress"], (
                f"{_ident(case)}: the backfill progress differs from the vector"
            )

    check = case / "verify.json"
    if check.is_file():
        want = _read_json(check)
        if session is None:
            session = sde.Session(model, placement, engines)
        report = sde.verify(session, want["group"], **(want.get("options") or {}))
        # `at` is a clock reading, so the vector carries a placeholder rather than an instant: a
        # vector with a timestamp in it is a vector that expires.
        assert {**report.as_record(), "at": "<any>"} == want["report"]
        assert report.matched is want["matched"]
        assert [
            {
                "entity": difference.entity,
                "table": difference.table,
                "key": dict(difference.key),
                "columns": list(difference.columns),
            }
            for difference in report.differences
        ] == want["differences"], (
            "the differences differ. These hold the client's own key values and are deliberately "
            "absent from the record that crosses the boundary, so they are compared here and "
            "nowhere else."
        )

    bound = case / "verification.json"
    if bound.is_file():
        want = _read_json(bound)
        session = sde.Session(model, placement, engines, project_id=want.get("project_id"))
        def compare() -> sde.VerifyReport:
            request = sde.VerificationRequest.from_record(want["request"])
            return sde.verify(session, want["group"], request=request,
                              at=want["at"], chunk_rows=want.get("chunk_rows", 3))
        if "error" in want:
            with pytest.raises(_ERRORS[want["error"]], match=re.escape(want["match"])):
                compare()
        else:
            report = compare()
            assert report.as_record() == want["report"]
            assert report.matched is want["matched"]

    operations = case / "operations.json"
    if operations.is_file():
        _drive_session(case, model, placement, engines)

    calls = case / "calls.json"
    if calls.is_file():
        # **One sequence for the whole engine set.** Per-engine lists cannot express the guarantee
        # the dual-write cases are about - a row reaches the source before anything is attempted
        # against the copy - and reversing those two lines passed every vector while one of them
        # claimed in writing that the ordering was what it pinned.
        journal = next(iter(engines.values())).recorded
        assert journal.as_list() == _read_json(calls), (
            f"{_ident(case)}: the calls this library made to the engines differ from the vector. "
            "The counts can be right and the calls wrong - that is a library that works on a "
            "fixture and not on a table."
        )


def test_there_are_migration_vectors() -> None:
    assert _cases("migration"), "no migration vectors found, but this library claims Tier 2"


def test_the_no_account_mode_touches_no_engine() -> None:
    """The one case whose expectation is an **empty** call list, asserted here as well.

    Section 12.6 promises that in the no-account mode this library does nothing at all - no table,
    no query, no cost. That is a claim about calls that were *not* made, and a vector holding an
    empty list is easy to satisfy by accident: a runner that never built the engines would pass it.
    So the same document is read from the other side, and the assertion is that some other case in
    this family does make calls.
    """
    empty: list[str] = []
    busy: list[str] = []
    for case in _cases("migration"):
        document = case / "calls.json"
        if not document.is_file():
            continue
        made = len(_read_json(document))
        (empty if made == 0 else busy).append(case.name)
    assert empty, "no migration vector pins an engine this library must not touch"
    assert busy, "every migration vector expects zero calls, so the runner may not be running"


def test_every_shape_kind_is_exercised_by_a_vector() -> None:
    """Totality over the operation kinds, and it is here because a mutation survived.

    Removing ``bulk_write`` from the set of kinds that count as writes passed the whole shared
    suite: no vector recorded a bulk write, so nothing measured the classification. The consequence
    is not subtle - a group that takes every bulk load the application sends would be scored as
    read-heavy, and the planner would place it accordingly - but it is invisible, because the
    numbers are plausible either way.

    Kinds are resolved through each family's *model*, so a case cannot satisfy this by naming a kind
    in its own text.
    """
    seen: set[str] = set()
    for family, field in (("telemetry", "operations.json"), ("routing", "cases.json")):
        for case in _cases(family):
            document = case / field
            if not document.is_file():
                continue
            model = model_from_neutral(_read_json(case / "model.json"))
            by_id = {shape.id: shape for shape in sde.enumerate_shapes(model)}
            for entry in _read_json(document):
                shape = by_id.get(entry.get("shape", ""))
                if shape is not None:
                    seen.add(shape.kind)
    unreached = set(sde.SHAPE_KINDS) - seen
    assert unreached == set(), (
        f"no vector exercises {sorted(unreached)}. A kind nothing records is a classification "
        f"nothing shared checks."
    )


def test_there_are_telemetry_vectors() -> None:
    # This library reaches Tier 1, so these do not get to be skipped either.
    assert _cases("telemetry"), "no telemetry vectors found, but this library claims Tier 1"


def test_every_measured_field_is_reachable_from_the_telemetry_vectors() -> None:
    """Totality over the feature vector, in the direction that rots.

    A field this family never emits and never names as missing is one where two libraries can
    disagree with nothing shared to notice. Both halves count: emitting it and declaring it absent
    are the two things a window can say about a field, and a field that appears in neither is one
    this suite has no opinion about.
    """
    seen: set[str] = set()
    for case in _cases("telemetry"):
        documents = [case / "window.json"]
        for path in documents:
            if not path.is_file():
                continue
            for body in _read_json(path)["groups"].values():
                seen |= {key for key in body if key not in ("copies",)}
                seen |= set(body.get("missing", ()))
        explicit = case / "features_for.json"
        if explicit.is_file():
            for body in _read_json(explicit).values():
                seen |= set(body)
                seen |= set(body.get("missing", ()))
    unreached = set(sde.MEASURED_FIELDS) - seen
    assert unreached == set(), f"no telemetry vector reaches {sorted(unreached)}"


def test_there_are_schema_vectors() -> None:
    # This library reaches Tier 2, so these do not get to be skipped. A tier claim the vectors
    # cannot check is what section 10 said the gap was, and it is closed.
    assert _cases("schema"), "no schema vectors found, but this library claims Tier 2"


def test_every_dialect_this_library_renders_appears_in_the_schema_vectors() -> None:
    """Totality, in the direction that rots. A dialect with no vector is one where two libraries
    can disagree and nothing shared would notice - which is how section 7 came to have eleven
    refusals and no shared coverage."""
    covered = {
        expectation["dialect"]
        for case in _cases("schema")
        for expectation in _read_json(case / "cases.json")
    }
    assert set(sde.DIALECTS) <= covered, f"no schema vector renders {set(sde.DIALECTS) - covered}"


@pytest.mark.parametrize("case", _cases("signature"), ids=_ident)
def test_signature_vector(case: Path) -> None:
    """Accepting a **set** of public keys, which is what makes rotating our key possible.

    Shared rather than Python-only for the usual reason: an acceptance rule that holds in one
    runtime and not another is one map with two meanings, and nothing compiles differently. This
    family is also the only one whose expectations were produced by openssl rather than by this
    library - see ``conformance/tools/signature_vectors.py``.
    """
    model = model_from_neutral(_read_json(case / "model.json"))
    document = _read_json(case / "map.json")
    encoded: Mapping[str, str] = _read_json(case / "keys.json")
    expected = _read_json(case / "expected.json")

    # A single entry under the empty name is the bare-key form. It is not the same call as a
    # one-entry mapping, and the difference is what the library reports back afterwards.
    keys: Any
    if list(encoded) == [""]:
        keys = b64decode(encoded[""])
    else:
        keys = {name: b64decode(value) for name, value in encoded.items()}

    if "error" in expected:
        with pytest.raises(_ERRORS[expected["error"]], match=expected["match"]):
            sde.load_map(document, model=model, public_key=keys, require_signature=True)
        return

    placement = sde.load_map(document, model=model, public_key=keys, require_signature=True)
    assert placement.signed is True
    assert placement.verified_with == expected["verified_with"], (
        f"the map verified with {placement.verified_with!r}, the vector expects "
        f"{expected['verified_with']!r}"
    )


def test_the_signature_family_covers_both_outcomes() -> None:
    """A family of nothing but refusals proves a library can refuse, never that it can accept.

    Both directions, in one place, for the reason 12.11 needed its second half: a gate that
    rejects everything does not demonstrate a gate.
    """
    outcomes = {
        "error" in _read_json(case / "expected.json") for case in _cases("signature")
    }
    assert outcomes == {True, False}, "the signature vectors only cover one outcome"


_ERRORS: dict[str, type[Exception]] = {
    "DeclarationError": DeclarationError,
    # Named here rather than in a second table for the `schema/` family: one mapping from the name a
    # vector writes to the class, so a family added later cannot introduce a second spelling of
    # "which error".
    "EngineError": EngineError,
    "MapError": MapError,
    "MapRolledBack": MapRolledBack,
    "MigrationRefused": MigrationRefused,
}


def _load_map_from_vector(case: Path, load: Mapping[str, Any]) -> sde.PlacementMap:
    """Build the model, then load the map. The order is the point.

    A map-stage vector has a *valid* model, and building it has to happen outside the block that
    expects the failure. Otherwise a vector whose model was broken by accident would raise
    ``DeclarationError`` at the model stage, and an assertion checking only the class and the
    message would be satisfied by a failure at the wrong stage entirely - which is the exact bug
    the ``stage`` field exists to catch.
    """
    model = model_from_neutral(_read_json(case / "model.json"))
    encoded = load.get("public_key")
    return sde.load_map(
        _read_json(case / "map.json"),
        model=model,
        public_key=b64decode(encoded) if isinstance(encoded, str) else None,
        require_signature=bool(load.get("require_signature", False)),
    )


def _refuse_at_session(case: Path, exc_type: type[Exception], expected: Mapping[str, Any]) -> None:
    """A stage neither of the other two can reach, and the reason is in the map format.

    A placement map names engines **by name** and deliberately carries no dialect (§7), so a rule
    about what two dialects do to a value is unanswerable while reading the document. The earliest
    door that can answer is the one holding the adapters, which is where the session is built.

    Model and map are both valid here and are built outside the assertion, for the reason the map
    stage builds its model outside one: a vector broken by accident would fail earlier and satisfy
    a check that reads only the class and the message.
    """
    model = model_from_neutral(_read_json(case / "model.json"))
    placement = _load_map_from_vector(case, expected.get("load", {}))
    engines = engines_from(_read_json(case / "engines.json"))
    with pytest.raises(exc_type, match=expected["match"]):
        sde.Session(model, placement, engines)
    # **A map that can never work must not have cost anything first.** A library that gathered the
    # watermarks and refused afterwards would produce this same refusal with the price already
    # paid, and that is not hypothetical: it is how the no-account promise broke in
    # `migration/001`, in the other language, and the empty list was the only thing that showed it.
    journal = next(iter(engines.values())).recorded
    assert journal.as_list() == _read_json(case / "calls.json"), (
        f"{_ident(case)}: the refusal is right and it was not free. Nothing may be created, read "
        "or written on the strength of a map this library is about to reject."
    )


@pytest.mark.parametrize("case", _cases("errors"), ids=_ident)
def test_error_vector(case: Path) -> None:
    expected = _read_json(case / "expected.json")
    exc_type = _ERRORS[expected["error"]]
    stage = expected["stage"]

    # The stage matters as much as the error. A library that raises the right exception when the
    # query runs, rather than when the model is built, has a different bug that happens to look the
    # same in a test that only checks the type.
    assert stage in ("model", "map", "session"), (
        f"{_ident(case)} expects the error at stage {stage!r}, which this runner does not know how "
        "to exercise yet. Failing rather than skipping: a stage nobody runs is a rule nobody "
        "checks."
    )

    if stage == "session":
        _refuse_at_session(case, exc_type, expected)
        return

    with pytest.raises(exc_type, match=expected["match"]):
        if stage == "model":
            model_from_neutral(_read_json(case / "model.json"))
        else:
            _load_map_from_vector(case, expected.get("load", {}))


def test_all_three_stages_are_actually_covered_by_vectors() -> None:
    """A stage the runner supports and no vector uses is a rule that reads as covered.

    Before the map stage existed, every rule in section 7 of the contract - eleven refusals, each
    deciding where a client's data gets written - was checked in Python's own tests and in nothing
    shared. TypeScript enforced the same rules and no shared case reached any of them, which is how
    the contract-mismatch message came to render a literal ``{CONTRACT}`` in one language and the
    number in the other.

    The session stage arrived the same way and one turn later: the fan-out precision rule was fixed
    in both languages on the same day and held by a per-language test in each, which is the state
    this suite exists to refuse. Two green suites is exactly how one map with two meanings looks
    from the inside.
    """
    stages = {_read_json(case / "expected.json")["stage"] for case in _cases("errors")}
    assert stages == {"map", "model", "session"}, f"error vectors cover only {sorted(stages)}"



# --- canonical vectors -----------------------------------------------------------------------
#
# These feed a value straight into the encoder rather than going through a model, and they exist
# because of a mutation that should have failed and did not. Every object key in the model IR is
# fixed ASCII, so the *object key* comparator was never exercised: swapping code point ordering
# for a naive sort passed the whole suite. Field names do reach the IR, but as array elements,
# which is a different call site with a different comparator.


@pytest.mark.parametrize("case", _cases("canonical"), ids=_ident)
def test_canonical_vector(case: Path) -> None:
    raw = (case / "value.json").read_text(encoding="utf-8")
    value = json.loads(raw)

    expected_error = case / "expected.json"
    if expected_error.exists():
        want = _read_json(expected_error)
        assert want["error"] == "CanonicalError"
        with pytest.raises(CanonicalError, match=want["match"]):
            canonical_bytes(value)
        return

    expected = (case / "bytes.json").read_bytes()
    assert canonical_bytes(value) == expected, (
        f"{_ident(case)}: canonical bytes differ. See why.txt in that directory - every one of "
        "these expectations was written by hand from the format contract, so a mismatch means the "
        "implementation drifted from the document rather than the other way round."
    )


def test_there_are_canonical_vectors() -> None:
    assert _cases("canonical"), "no canonical vectors found"

# --- hashing vectors -------------------------------------------------------------------------
#
# Only run by a library that offers hashing (§2a), and hashing is a mode rather than a tier. What
# these pin is not the HMAC - anything can compute an HMAC - but the message: NFC first, U+0000 as
# the separator, the prefix outside, fields hashed with their entity. Every one of those is
# invisible in an ASCII-only test.


@pytest.mark.parametrize("case", _cases("hashing"), ids=_ident)
def test_hashing_vector(case: Path) -> None:
    salt = bytes.fromhex((case / "salt.hex").read_text().strip())
    sde.clear_registry()
    model = model_from_neutral(_read_json(case / "model.json"))
    hashed, names = hash_identifiers(model, salt)

    expected = _read_json(case / "names.json")
    assert dict(names.entities) == expected["entities"], (
        "entity digests differ from the vector. The HMAC is not the likely cause - check whether "
        "the name is NFC-normalised before hashing and whether the prefix leaked into the message."
    )
    assert {e: dict(m) for e, m in names.fields.items()} == expected["fields"], (
        "field digests differ. Fields are hashed *with* their entity, so the message is "
        "entity + U+0000 + field, not the field name alone."
    )
    assert {e: dict(m) for e, m in names.relations.items()} == expected["relations"]

    assert canonical_bytes(hashed.ir) == (case / "ir.json").read_bytes()
    assert hashed.version == (case / "version.txt").read_text().strip()

    groups = [
        {"name": g.name, "members": list(g.members)} for g in sde.colocation_groups(hashed)
    ]
    assert groups == _read_json(case / "groups.json")

    # Where the case carries the same identifiers in a second normal form, the two must agree. A
    # library that hashes before normalising passes everything above and fails here.
    decomposed = case / "model-decomposed.json"
    if decomposed.exists():
        raw_nfc = (case / "model.json").read_bytes()
        raw_nfd = decomposed.read_bytes()
        assert raw_nfc != raw_nfd, (
            f"{_ident(case)}: the two model files are byte-identical, so this case proves nothing "
            "about normalisation."
        )
        sde.clear_registry()
        other, _ = hash_identifiers(model_from_neutral(_read_json(decomposed)), salt)
        assert other.version == (case / "version-decomposed.txt").read_text().strip()
        assert other.version == hashed.version, (
            "the same identifier in two normal forms produced two hashed models. A map issued for "
            "one service would be refused by the other, and no ASCII test can see it."
        )


def test_there_are_hashing_vectors() -> None:
    # This library offers hashing, so skipping these silently is not an option. A library that does
    # not offer it removes this test along with the feature - and says so in its README, because
    # "supported" has to mean one thing across languages.
    assert _cases("hashing"), "no hashing vectors found, but this library implements section 2a"

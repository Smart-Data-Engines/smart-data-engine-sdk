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
from base64 import b64decode
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

import sde
from sde.canonical import CanonicalError, canonical_bytes
from sde.errors import DeclarationError, MapError
from sde.hashing import hash_identifiers
from sde.testing.loader import model_from_neutral

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


_ERRORS: dict[str, type[Exception]] = {
    "DeclarationError": DeclarationError,
    "MapError": MapError,
}


def _load_map_from_vector(case: Path, load: Mapping[str, Any]) -> None:
    """Build the model, then load the map. The order is the point.

    A map-stage vector has a *valid* model, and building it has to happen outside the block that
    expects the failure. Otherwise a vector whose model was broken by accident would raise
    ``DeclarationError`` at the model stage, and an assertion checking only the class and the
    message would be satisfied by a failure at the wrong stage entirely - which is the exact bug
    the ``stage`` field exists to catch.
    """
    model = model_from_neutral(_read_json(case / "model.json"))
    encoded = load.get("public_key")
    sde.load_map(
        _read_json(case / "map.json"),
        model=model,
        public_key=b64decode(encoded) if isinstance(encoded, str) else None,
        require_signature=bool(load.get("require_signature", False)),
    )


@pytest.mark.parametrize("case", _cases("errors"), ids=_ident)
def test_error_vector(case: Path) -> None:
    expected = _read_json(case / "expected.json")
    exc_type = _ERRORS[expected["error"]]
    stage = expected["stage"]

    # The stage matters as much as the error. A library that raises the right exception when the
    # query runs, rather than when the model is built, has a different bug that happens to look the
    # same in a test that only checks the type.
    assert stage in ("model", "map"), (
        f"{_ident(case)} expects the error at stage {stage!r}, which this runner does not know how "
        "to exercise yet. Failing rather than skipping: a stage nobody runs is a rule nobody "
        "checks."
    )

    with pytest.raises(exc_type, match=expected["match"]):
        if stage == "model":
            model_from_neutral(_read_json(case / "model.json"))
        else:
            _load_map_from_vector(case, expected.get("load", {}))


def test_both_stages_are_actually_covered_by_vectors() -> None:
    """A stage the runner supports and no vector uses is a rule that reads as covered.

    Before the map stage existed, every rule in section 7 of the contract - eleven refusals, each
    deciding where a client's data gets written - was checked in Python's own tests and in nothing
    shared. TypeScript enforced the same rules and no shared case reached any of them, which is how
    the contract-mismatch message came to render a literal ``{CONTRACT}`` in one language and the
    number in the other.
    """
    stages = {_read_json(case / "expected.json")["stage"] for case in _cases("errors")}
    assert stages == {"model", "map"}, f"error vectors cover only {sorted(stages)}"


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

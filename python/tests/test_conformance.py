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
from pathlib import Path
from typing import Any

import pytest

import sde
from sde.canonical import canonical_bytes
from sde.errors import DeclarationError, MapError
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


@pytest.mark.parametrize("case", _cases("errors"), ids=_ident)
def test_error_vector(case: Path) -> None:
    expected = _read_json(case / "expected.json")
    exc_type = _ERRORS[expected["error"]]

    # The stage matters as much as the error. A library that raises the right exception when the
    # query runs, rather than when the model is built, has a different bug that happens to look the
    # same in a test that only checks the type.
    assert expected["stage"] == "model", (
        f"{_ident(case)} expects the error at stage {expected['stage']!r}, which this runner does "
        "not know how to exercise yet"
    )

    with pytest.raises(exc_type, match=expected["match"]):
        model_from_neutral(_read_json(case / "model.json"))

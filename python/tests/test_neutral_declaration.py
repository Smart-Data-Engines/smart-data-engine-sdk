"""`sde.neutral_declaration`: a built model back into the form the control plane takes.

Format contract §4a is the document a client hands over and the shape the control plane stores. A
client declares their model in whichever way their language is comfortable with, so without an
exporter they write it a second time by hand and the two copies drift. That is not hypothetical:
three of our own control-plane tests handed over a model's **IR** instead, which is a different
document that looks close enough, and nothing noticed for weeks because nothing read the stored file
back.

So there are two assertions here and they are opposite. The round trip says the exporter and the
loader agree - on the version, which is the only thing agreement means here. The refusal says the IR
is not this document, which is the mistake the pair of shapes invites.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import sde
from sde.canonical import canonical_bytes
from sde.errors import DeclarationError
from sde.testing.loader import model_from_neutral

VECTORS = Path(__file__).resolve().parents[2] / "conformance" / "vectors"
MODELS = sorted(p.name for p in (VECTORS / "model").iterdir() if p.is_dir())


@pytest.mark.parametrize("vector", MODELS)
def test_the_round_trip_keeps_the_version_and_the_bytes(vector: str) -> None:
    """Every model vector, exported and reloaded. The version is a digest of the IR, so equal
    versions mean equal bytes - but both are asserted, because a test that only compares digests
    would pass a pair of implementations that agree on a hash of the wrong thing."""
    declaration = json.loads((VECTORS / "model" / vector / "model.json").read_text())
    original = model_from_neutral(declaration)
    again = model_from_neutral(sde.neutral_declaration(original))
    assert again.version == original.version
    assert canonical_bytes(again.ir) == canonical_bytes(original.ir)


def test_the_round_trip_survives_hashed_identifiers() -> None:
    """Hashing rewrites every name in the declaration, so the exporter has to carry the digests
    rather than anything it remembers about the real names - which it never sees."""
    declaration = json.loads((VECTORS / "hashing" / "001-shop" / "model.json").read_text())
    salt = bytes.fromhex((VECTORS / "hashing" / "001-shop" / "salt.hex").read_text().strip())
    model = model_from_neutral(declaration)
    hashed, _ = sde.hash_identifiers(model, salt=salt)
    again = model_from_neutral(sde.neutral_declaration(hashed))
    assert again.version == hashed.version
    assert again.version != model.version, "hashing is a model change, not a setting"


def test_the_exported_document_is_json_and_says_the_key_as_a_list() -> None:
    """The one difference between this document and the IR, asserted rather than described."""
    model = model_from_neutral(
        json.loads((VECTORS / "model" / "002-relations-keys-atomicity" / "model.json").read_text())
    )
    document = sde.neutral_declaration(model)
    json.dumps(document)  # a document that cannot be written is not a document
    order = next(e for e in document["entities"] if e["name"] == "Order")
    assert order["key"] == ["tenant", "id"], "in order, and as names"
    assert model.ir["entities"][1]["key"] == [
        {"field": "tenant", "position": 0},
        {"field": "id", "position": 1},
    ], "the IR's form, for contrast"


def test_handing_over_the_ir_is_refused_by_name() -> None:
    """The mistake the two shapes invite, and the reason the diagnostic is pinned by a vector.

    Before `errors/036` this reached a set-membership test on a dict and raised
    `TypeError: unhashable type: 'dict'` from inside the encoder - a library exception with no
    explanation, on the path whose whole job is to explain.
    """
    model = model_from_neutral(
        json.loads((VECTORS / "model" / "001-single-entity" / "model.json").read_text())
    )
    with pytest.raises(DeclarationError, match="the IR's key form"):
        model_from_neutral(model.ir)


def test_a_declaration_that_is_not_a_document_is_refused_rather_than_crashed_on() -> None:
    """Four shapes a client's tooling can produce, none of which used to reach a message."""
    for bad in ({}, {"entities": "Reading"}, {"entities": [{"fields": []}]},
                {"entities": [{"name": "A", "fields": "id"}]},
                {"entities": [{"name": "A", "fields": [{"name": "id"}]}]},
                {"entities": [{"name": "A", "fields": [{"name": "id", "type": "uuid"}],
                               "key": "id"}]}):
        with pytest.raises(DeclarationError):
            model_from_neutral(bad)

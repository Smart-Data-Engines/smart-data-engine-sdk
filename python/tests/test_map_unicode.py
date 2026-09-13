"""Map text must name the same identifier before and after canonical signing."""

from __future__ import annotations

import base64
from copy import deepcopy
from typing import Any

import pytest

import sde


def document(contract: int = 4) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "contract": contract,
        "model_version": "0123456789abcdef",
        "map_version": 1,
        "groups": {
            "Record": {
                "source": {
                    "id": "source",
                    "engine": "db",
                    "layout": {
                        "tables": {"Record": "records"},
                        "columns": {"Record": {"id": "bigint"}},
                    },
                }
            }
        },
    }
    if contract >= 4:
        raw["project_id"] = "1" * 32
        raw["groups"]["Record"]["write_epoch"] = 1
    return raw


@pytest.mark.parametrize("contract", [1, 2, 3, 4])
def test_every_map_contract_refuses_decomposed_physical_names(contract: int) -> None:
    raw = document(contract)
    raw["groups"]["Record"]["source"]["layout"]["tables"]["Record"] = "cafe\u0301"
    with pytest.raises(sde.MapError, match="NFC"):
        sde.load_map(raw)


@pytest.mark.parametrize("where", ["engine", "id", "column-key", "nested-list", "root-key"])
def test_payload_checks_names_and_values_at_every_depth(where: str) -> None:
    raw = document()
    source = raw["groups"]["Record"]["source"]
    if where in ("engine", "id"):
        source[where] = "e\u0301"
    elif where == "column-key":
        source["layout"]["columns"]["Record"]["e\u0301"] = "integer"
    elif where == "nested-list":
        raw["metadata"] = [{"nested": ["e\u0301"]}]
    else:
        raw["e\u0301"] = "metadata"
    with pytest.raises(sde.MapError, match="NFC"):
        sde.load_map(raw)


@pytest.mark.parametrize("value", ["\ud800", "\udfff"])
def test_map_payload_refuses_unpaired_surrogates(value: str) -> None:
    raw = document()
    raw["groups"]["Record"]["source"]["layout"]["tables"]["Record"] = value
    with pytest.raises(sde.MapError, match="Unicode scalar"):
        sde.load_map(raw)


def test_nfc_and_astral_identifiers_are_retained_exactly() -> None:
    raw = document()
    raw["groups"]["Record"]["source"]["layout"]["tables"]["Record"] = "caf\u00e9_\U0001f6f0"
    parsed = sde.load_map(raw)
    assert parsed.groups["Record"].source.layout.table_for("Record") == "caf\u00e9_\U0001f6f0"
    assert parsed.fingerprint is not None


def test_unsigned_signature_hint_remains_only_a_hint() -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    raw = document()
    key = Ed25519PrivateKey.generate()
    raw["signature"] = {
        "alg": "ed25519",
        "key_id": "e\u0301",
        "value": base64.b64encode(key.sign(sde.canonical_bytes(raw))).decode(),
    }
    parsed = sde.load_map(raw, public_key={"trusted": key.public_key().public_bytes_raw()})
    assert parsed.verified_with == "trusted"


def test_encoder_normalization_is_not_silent_identifier_rewriting() -> None:
    original = document()
    original["groups"]["Record"]["source"]["layout"]["tables"]["Record"] = "caf\u00e9"
    decomposed = deepcopy(original)
    decomposed["groups"]["Record"]["source"]["layout"]["tables"]["Record"] = "cafe\u0301"
    assert sde.canonical_bytes(original) == sde.canonical_bytes(decomposed)
    assert sde.load_map(original).groups["Record"].source.layout.table_for("Record") == "caf\u00e9"
    with pytest.raises(sde.MapError, match="NFC"):
        sde.load_map(decomposed)
    assert decomposed["groups"]["Record"]["source"]["layout"]["tables"]["Record"] == "cafe\u0301"

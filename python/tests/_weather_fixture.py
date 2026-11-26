"""Synthetic bootstrap signer for tests, with no dependency on a test runner."""

from __future__ import annotations

import base64
from copy import deepcopy
from typing import Any
from uuid import uuid4

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

import sde
from sde_demo.model import model

# The order of P-256: an ES256 signature's s is given in the lower half of it.
P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def supplied(source: str = "postgres", alg: str = "ed25519") -> tuple[dict[str, Any], Any]:
    """A bootstrap whose map is signed with `alg` (``ed25519`` or ``es256``), and its signer."""
    logical = model()
    ed25519 = Ed25519PrivateKey.generate()
    p256 = ec.generate_private_key(ec.SECP256R1())

    def signature(payload: bytes) -> bytes:
        if alg == "ed25519":
            return ed25519.sign(payload)
        r, s = decode_dss_signature(p256.sign(payload, ec.ECDSA(hashes.SHA256())))
        s = min(s, P256_N - s)
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    def sign(raw: dict[str, Any]) -> dict[str, Any]:
        value = deepcopy(raw)
        value.pop("signature", None)
        value["signature"] = {
            "alg": alg,
            "key_id": "demo-test",
            "value": base64.b64encode(signature(sde.canonical_bytes(value))).decode(),
        }
        return value

    public = (
        ed25519.public_key().public_bytes_raw()
        if alg == "ed25519"
        else p256.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
    )

    group = sde.colocation_groups(logical)[0]
    layout = sde.default_layout(logical, group, dialect=source)
    project_id = uuid4().hex
    current = sign(
        {
            "contract": 4,
            "project_id": project_id,
            "model_version": logical.version,
            "map_version": 1,
            "groups": {
                group.name: {
                    "source": {
                        "id": "source",
                        "engine": source,
                        "layout": {
                            "tables": dict(layout.tables),
                            "columns": {
                                name: dict(columns) for name, columns in layout.columns.items()
                            },
                            "indexes": [dict(index) for index in layout.indexes],
                        },
                    },
                    "write_epoch": 1,
                }
            },
        }
    )
    return {
        "kind": "sde-weather-bootstrap",
        "protocol": 1,
        "project_id": project_id,
        "model": sde.neutral_declaration(logical),
        "public_keys": {"demo-test": base64.b64encode(public).decode()},
        "engines": {"postgres": "postgres", "clickhouse": "clickhouse"},
        "current_map": current,
    }, sign

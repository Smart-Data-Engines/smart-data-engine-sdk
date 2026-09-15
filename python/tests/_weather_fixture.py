"""Synthetic bootstrap signer for tests, with no dependency on a test runner."""

from __future__ import annotations

import base64
from copy import deepcopy
from typing import Any
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import sde
from sde_demo.model import model


def supplied(source: str = "postgres") -> tuple[dict[str, Any], Any]:
    logical = model()
    key = Ed25519PrivateKey.generate()

    def sign(raw: dict[str, Any]) -> dict[str, Any]:
        value = deepcopy(raw)
        value.pop("signature", None)
        value["signature"] = {
            "alg": "ed25519",
            "key_id": "demo-test",
            "value": base64.b64encode(key.sign(sde.canonical_bytes(value))).decode(),
        }
        return value

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
        "public_keys": {
            "demo-test": base64.b64encode(key.public_key().public_bytes_raw()).decode()
        },
        "engines": {"postgres": "postgres", "clickhouse": "clickhouse"},
        "current_map": current,
    }, sign

"""Canonical Unicode physical names retain native save/get behavior with restricted credentials."""

from __future__ import annotations

from typing import Any

from test_generation_session_live import fixture
from test_runtime_privileges_live import roles as roles

import sde
from sde.placement import WATERMARK_TABLE


def test_signed_nfc_and_astral_table_names_work_on_restricted_runtime(roles: Any) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    table = "caf\u00e9_\U0001f6f0"
    model, placement = fixture(roles.operator, table, key=Ed25519PrivateKey.generate())
    sde.prepare_schema(model, placement, {"db": roles.operator}, project_id="1" * 32)
    roles.grant(table)
    roles.grant(WATERMARK_TABLE)
    session = sde.Session(model, placement, {"db": roles.runtime}, project_id="1" * 32)
    session.save("Record", {"id": 42})
    assert session.get("Record", {"id": 42}) == {"id": 42}
    assert roles.operator.count(table) == 1
    assert placement.groups["Record"].source.layout.table_for("Record") == table

"""Staging authorization is a snapshot and cannot be used against another current-map mode."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path

import pytest

import sde
from sde.testing.loader import model_from_neutral

CASE = (
    Path(__file__).resolve().parents[2]
    / "conformance/vectors/migration/099-staging-authorizes-one-fresh-copy"
)


def fixture():
    raw = json.loads((CASE / "plan.json").read_bytes())
    model = model_from_neutral(json.loads((CASE / "model.json").read_bytes()))
    keys = {
        name: base64.b64decode(value)
        for name, value in json.loads((CASE / "keys.json").read_bytes()).items()
    }
    plan = sde.load_staging_plan(raw, model=model, project_id="1" * 32, public_key=keys)
    return raw, model, keys, plan


def test_caller_and_returned_records_cannot_replace_the_prepared_copy() -> None:
    raw, _, _, plan = fixture()
    expected = plan.prepared_payload()
    raw["prepared"]["groups"]["Event"]["derived"][0]["layout"]["tables"]["Event"] = "other"
    returned = plan.as_record()
    returned["prepared"]["groups"]["Event"]["derived"][0]["layout"]["tables"]["Event"] = "other"
    assert plan.prepared_payload() == expected


def test_reconstructed_plan_cannot_inherit_loaded_provenance() -> None:
    _, _, _, plan = fixture()
    with pytest.raises(sde.MigrationRefused, match="immutable loaded"):
        replace(plan).as_record()
    with pytest.raises(sde.MigrationRefused, match="immutable loaded"):
        replace(plan, group="Order").check_current(plan.current)


def test_stage_checks_the_actual_signed_current_map() -> None:
    raw, model, _keys, plan = fixture()
    plan.check_current(plan.current)
    with pytest.raises(sde.MigrationRefused):
        plan.check_current(plan.prepared)
    with pytest.raises(sde.MigrationRefused):
        plan.check_current(replace(plan.current))
    del raw["current"]["signature"]
    unsigned = sde.load_map(raw["current"], model=model)
    assert unsigned.fingerprint == plan.current.fingerprint
    with pytest.raises(sde.MigrationRefused, match="signed current map"):
        plan.check_current(unsigned)


def test_integral_json_numbers_preserve_packet_identity() -> None:
    raw, model, keys, plan = fixture()
    raw["protocol"] = 1.0
    for name in ("current", "prepared"):
        raw[name]["contract"] = 4.0
        raw[name]["map_version"] = float(raw[name]["map_version"])
        raw[name]["groups"]["Event"]["write_epoch"] = 1.0
    assert (
        sde.load_staging_plan(raw, model=model, project_id="1" * 32, public_key=keys).fingerprint
        == plan.fingerprint
    )


@pytest.mark.parametrize("position", [0, -1, 1000000, True, 1.5])
def test_table_names_require_a_bounded_entity_position(position: int) -> None:
    with pytest.raises(sde.MigrationRefused, match="position"):
        sde.staging_table_name("5" * 32, position)


def test_fresh_names_have_an_explicit_portable_form() -> None:
    assert sde.staging_table_name("5" * 32, 1) == "sde_m_" + "5" * 32 + "_000001"
    assert len(sde.staging_table_name("5" * 32, 999999).encode()) < 64

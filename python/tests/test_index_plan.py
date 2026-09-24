"""An in-place build authorization is a snapshot and binds the exact signed map in force."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import sde
from sde.testing.loader import model_from_neutral

VECTORS = Path(__file__).resolve().parents[2] / "conformance/vectors/migration"
CASE = VECTORS / "144-index-build-adds-several-after-the-indexes-in-force"


def fixture() -> tuple[dict[str, Any], sde.LogicalModel, dict[str, bytes], sde.IndexPlan]:
    raw = json.loads((CASE / "plan.json").read_bytes())
    model = model_from_neutral(json.loads((CASE / "model.json").read_bytes()))
    keys = {
        name: base64.b64decode(value)
        for name, value in json.loads((CASE / "keys.json").read_bytes()).items()
    }
    plan = sde.load_index_plan(raw, model=model, project_id="1" * 32, public_key=keys)
    return raw, model, keys, plan


def test_caller_and_returned_records_cannot_replace_the_next_map_or_its_indexes() -> None:
    raw, _, _, plan = fixture()
    expected, added = plan.prepared_payload(), [dict(index) for index in plan.added]
    raw["prepared"]["groups"]["Event"]["source"]["layout"]["indexes"][1]["columns"] = ["id"]
    returned = plan.as_record()
    returned["prepared"]["groups"]["Event"]["source"]["layout"]["indexes"][1]["columns"] = ["id"]
    assert plan.prepared_payload() == expected
    assert [dict(index) for index in plan.added] == added
    with pytest.raises(TypeError):
        plan.added[0]["columns"] = ["id"]  # type: ignore[index]


def test_only_the_indexes_after_the_ones_in_force_are_added() -> None:
    _, _, _, plan = fixture()
    assert [index["name"] for index in plan.added] == [
        sde.index_build_name(plan.index_id, 1),
        sde.index_build_name(plan.index_id, 2),
    ]
    assert plan.added[0]["method"] == "brin"


def test_reconstructed_plan_cannot_inherit_loaded_provenance() -> None:
    _, _, _, plan = fixture()
    with pytest.raises(sde.MigrationRefused, match="immutable loaded"):
        replace(plan).as_record()
    with pytest.raises(sde.MigrationRefused, match="immutable loaded"):
        replace(plan, group="Order").check_current(plan.current)


def test_the_build_checks_the_actual_signed_current_map() -> None:
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
    raw["protocol"], raw["build_budget_ms"] = 1.0, float(raw["build_budget_ms"])
    for name in ("current", "prepared"):
        raw[name]["contract"] = float(raw[name]["contract"])
        raw[name]["map_version"] = float(raw[name]["map_version"])
    loaded = sde.load_index_plan(raw, model=model, project_id="1" * 32, public_key=keys)
    assert loaded.fingerprint == plan.fingerprint
    assert type(loaded.build_budget_ms) is int


@pytest.mark.parametrize("position", [0, -1, 1000000, True, 1.5])
def test_index_names_require_a_bounded_position(position: int) -> None:
    with pytest.raises(sde.MigrationRefused, match="position"):
        sde.index_build_name("6" * 32, position)


def test_index_names_have_an_explicit_portable_form() -> None:
    assert sde.index_build_name("6" * 32, 1) == "sde_i_" + "6" * 32 + "_000001"
    assert len(sde.index_build_name("6" * 32, 999999).encode()) < 64
    with pytest.raises(sde.MigrationRefused, match="index build index_id"):
        sde.index_build_name("G" * 32, 1)

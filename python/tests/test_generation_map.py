"""A generation-bearing map must remain an unambiguous, immutable signed artifact."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

import sde
from sde.testing.loader import model_from_neutral

ROOT = Path(__file__).resolve().parents[2]
CASE = ROOT / "conformance/vectors/migration/022-verification-is-bound-to-its-request"
PROJECT = "1" * 32


def document() -> tuple[sde.LogicalModel, dict[str, Any]]:
    model = model_from_neutral(json.loads((CASE / "model.json").read_text()))
    raw = json.loads((CASE / "map.json").read_text())
    raw.update(contract=4, project_id=PROJECT)
    for group in raw["groups"].values():
        group["write_epoch"] = 1
    return model, raw


def test_v4_binds_project_and_each_groups_write_epoch() -> None:
    model, raw = document()
    loaded = sde.load_map(raw, model=model)
    assert loaded.project_id == PROJECT
    assert loaded.placement_of("Reading").write_epoch == 1
    assert loaded.fingerprint is not None
    raw["groups"]["Reading"]["write_epoch"] = 2
    assert loaded.placement_of("Reading").write_epoch == 1


@pytest.mark.parametrize("epoch", [None, False, 0, -1, 1.5, "1", 2**53])
def test_v4_refuses_missing_invalid_or_unsafe_epochs(epoch: Any) -> None:
    model, raw = document()
    raw["groups"]["Reading"]["write_epoch"] = epoch
    with pytest.raises(sde.MapError, match="positive safe write_epoch"):
        sde.load_map(raw, model=model)


@pytest.mark.parametrize("project", [None, "", "A" * 32, "1" * 31, False])
def test_v4_needs_an_explicit_project(project: Any) -> None:
    model, raw = document()
    raw["project_id"] = project
    with pytest.raises(sde.MapError, match="project_id"):
        sde.load_map(raw, model=model)


def test_v4_normalizes_json_integer_spelling_before_fingerprinting() -> None:
    model, raw = document()
    normalized = sde.load_map(raw, model=model)
    raw["contract"] = 4.0
    raw["map_version"] = float(raw["map_version"])
    raw["groups"]["Reading"]["write_epoch"] = 1.0
    assert sde.load_map(raw, model=model).fingerprint == normalized.fingerprint
    raw["annotation"] = 1.5
    with pytest.raises(sde.MapError, match="canonically encodable"):
        sde.load_map(raw, model=model)


def test_a_new_feature_cannot_hide_in_an_old_contract() -> None:
    model, raw = document()
    raw["contract"] = 3
    with pytest.raises(sde.MapError, match="project_id requires"):
        sde.load_map(raw, model=model)
    raw.pop("project_id")
    with pytest.raises(sde.MapError, match="write_epoch requires"):
        sde.load_map(raw, model=model)


def test_the_runtime_epoch_column_cannot_be_a_logical_data_column() -> None:
    model, raw = document()
    layout = raw["groups"]["Reading"]["source"]["layout"]
    layout["columns"]["Reading"]["__sde_write_epoch"] = "bigint"
    with pytest.raises(sde.MapError, match="reserved for the SDK"):
        sde.load_map(raw, model=model)


def test_auto_layout_resolution_keeps_the_group_epoch() -> None:
    model, raw = document()
    raw["groups"]["Reading"]["source"]["layout"] = {"auto": True}
    loaded = sde.load_map(raw, model=model)
    assert loaded.placement_of("Reading").write_epoch == 1
    assert loaded.project_id == PROJECT
    assert copy.deepcopy(loaded).placement_of("Reading").write_epoch == 1

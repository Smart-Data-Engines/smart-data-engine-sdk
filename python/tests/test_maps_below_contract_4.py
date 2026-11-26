"""A map below contract 4 is refused where a project is configured (format contract §7r).

Such a map carries no project and no write generation, so neither a write fence nor a copy's mark
applies to it: taken by a session with a project configured, a forged map of an older contract would
direct the application's writes - its fan-out, and ``sde.backfill`` through it - to any table of its
shape, and ``prepare_schema`` would create tables for it. Without a project it is taken as before.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import sde
from sde.testing.loader import model_from_neutral
from sde.testing.memory import engines_from

ROOT = Path(__file__).resolve().parents[2]
CASE = ROOT / "conformance/vectors/migration/017-a-verify-that-matches"
PROJECT = "1" * 32


def _case() -> tuple[sde.LogicalModel, sde.PlacementMap, dict[str, Any]]:
    model = model_from_neutral(json.loads((CASE / "model.json").read_text()))
    placement = sde.load_map(json.loads((CASE / "map.json").read_text()), model=model)
    assert placement.contract < 4
    engines = engines_from(json.loads((CASE / "engines.json").read_text()))
    return model, placement, engines


def _calls(engines: dict[str, Any]) -> list[Any]:
    return list(next(iter(engines.values())).recorded.as_list())


def test_a_session_refuses_a_map_below_contract_4_where_a_project_is_configured() -> None:
    model, placement, engines = _case()
    with pytest.raises(sde.MigrationRefused, match="carries no project and no write generation"):
        sde.Session(model, placement, engines, project_id=PROJECT)
    assert _calls(engines) == []


def test_without_a_project_the_same_map_opens_as_before() -> None:
    model, placement, engines = _case()
    session = sde.Session(model, placement, engines)
    assert session.get("Reading", {"id": 1}) == {"id": 1, "celsius": 2}


def test_prepare_schema_refuses_a_map_below_contract_4_where_a_project_is_configured() -> None:
    model, placement, engines = _case()
    with pytest.raises(sde.MigrationRefused, match="carries no project and no write generation"):
        sde.prepare_schema(model, placement, engines, project_id=PROJECT)
    assert _calls(engines) == []


def test_without_a_project_prepare_schema_takes_the_same_map_as_before() -> None:
    model, placement, engines = _case()
    sde.prepare_schema(model, placement, engines)
    assert {call["call"] for call in _calls(engines)} == {"ensure_schema"}

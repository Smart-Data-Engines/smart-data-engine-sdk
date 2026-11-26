"""From contract 4, a copy's table is named by nothing else in the map (format contract §7, §7r).

The refusal itself is the shared vector ``errors/151``. These are the variants around it: what else
it refuses, and what it leaves as it was - two sources naming one table, and maps below contract 4,
whose layouts list every entity's table in every group.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

import sde
from sde.testing.loader import model_from_neutral

CASE = (
    Path(__file__).resolve().parents[2]
    / "conformance/vectors/errors/151-a-copy-named-like-another-groups-source"
)
TWICE = "is named twice in the map"


def _document() -> tuple[Any, dict[str, Any]]:
    model = model_from_neutral(json.loads((CASE / "model.json").read_text(encoding="utf-8")))
    return model, json.loads((CASE / "map.json").read_text(encoding="utf-8"))


def test_the_vector_is_refused_for_the_reason_it_names() -> None:
    model, raw = _document()
    with pytest.raises(sde.MapError, match=TWICE):
        sde.load_map(raw, model=model)


def test_two_sources_naming_one_table_load_as_they_did() -> None:
    model, raw = _document()
    event = raw["groups"]["Event"]
    del event["derived"], event["also_write"]
    event["source"] = {
        "id": "Event@pg", "engine": "pg-main", "layout": {"tables": {"Event": "order"}}
    }
    sde.load_map(raw, model=model)


def test_a_copy_named_like_another_group_s_copy_is_refused() -> None:
    model, raw = _document()
    line = raw["groups"]["Line"]
    line["source"]["engine"] = "ch-main"
    line["source"]["layout"]["tables"]["Order"] = "order_source"
    line["derived"] = [
        {
            "id": "Line@pg",
            "engine": "pg-main",
            "layout": {"tables": {"Line": "line_copy", "Order": "order"}},
            "lag_budget_ms": 30000,
        }
    ]
    line["source"]["id"] = "Line@ch"
    line["also_write"] = ["Line@pg"]
    with pytest.raises(sde.MapError, match="by group 'Event', materialisation 'Event@pg'"):
        sde.load_map(raw, model=model)


def test_a_copy_naming_one_table_for_two_entities_is_refused() -> None:
    model, raw = _document()
    raw["groups"]["Event"]["derived"][0]["layout"]["tables"]["Event"] = "event_copy"
    line = raw["groups"]["Line"]
    line["derived"] = [
        {
            "id": "Line@pg2",
            "engine": "pg-copy",
            "layout": {"tables": {"Line": "lines", "Order": "lines"}},
            "lag_budget_ms": 30000,
        }
    ]
    line["also_write"] = ["Line@pg2"]
    second = "materialisation 'Line@pg2' \\(entity 'Line'\\), and by"
    with pytest.raises(sde.MapError, match=second):
        sde.load_map(raw, model=model)


def test_a_map_below_contract_4_is_taken_as_it_was() -> None:
    model, raw = _document()
    older = copy.deepcopy(raw)
    older["contract"] = 3
    del older["project_id"]
    for group in older["groups"].values():
        del group["write_epoch"]
    sde.load_map(older, model=model)

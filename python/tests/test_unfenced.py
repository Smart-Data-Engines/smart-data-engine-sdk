"""Map contract 6 off the wire: schema preparation and operator inspection beside a group without
a write generation.

The shared vectors hold the loader, the session and the packets in both languages
(``tools/unfenced_vectors.py``), and ``_unfenced_scenario.py`` holds the operator against live
engines. This holds the two library entry points that neither reaches: ``prepare_schema``, which
must refuse a map that cannot work before it creates anything, and ``InspectionContext``, which an
operator builds without the engine it never binds.
"""

from __future__ import annotations

from typing import Any

import pytest
from _write_fence import MemoryFences

import sde
from sde.testing.loader import model_from_neutral
from sde.testing.memory import MemoryEngine, Recorded
from sde.write_fence import WriteFence

PROJECT = "1" * 32


def _model() -> sde.LogicalModel:
    sde.clear_registry()
    return model_from_neutral(
        {
            "entities": [
                {
                    "name": "Reading",
                    "fields": [
                        {"name": "celsius", "type": "int32"},
                        {"name": "id", "type": "int64"},
                    ],
                    "key": ["id"],
                },
                {
                    "name": "Tick",
                    "fields": [
                        {"name": "at", "type": "int64"},
                        {"name": "price", "type": "int64"},
                        {"name": "venue", "type": "string"},
                    ],
                    "key": ["venue", "at"],
                },
            ]
        }
    )


def _map(model: sde.LogicalModel) -> sde.PlacementMap:
    return sde.load_map(
        {
            "contract": 6,
            "project_id": PROJECT,
            "model_version": model.version,
            "map_version": 1,
            "groups": {
                "Reading": {
                    "write_epoch": 2,
                    "source": {
                        "id": "Reading@pg",
                        "engine": "pg-main",
                        "layout": {
                            "tables": {"Reading": "reading"},
                            "columns": {"Reading": {"id": "bigint", "celsius": "integer"}},
                        },
                    },
                },
                "Tick": {
                    "source": {
                        "id": "Tick@book",
                        "engine": "book-1",
                        "layout": {
                            "tables": {"Tick": "tick"},
                            "columns": {
                                "Tick": {"venue": "text", "at": "bigint", "price": "bigint"}
                            },
                        },
                    }
                },
            },
        },
        model=model,
    )


def _fenced(engine: MemoryEngine) -> dict[str, MemoryFences]:
    """Give ``engine`` write fences that start unprepared, and return their backends by table."""
    backends: dict[str, MemoryFences] = {}

    def factory(table: str, *, project_id: str) -> WriteFence:
        return WriteFence(backends.setdefault(table, MemoryFences()), table, project_id=project_id)

    engine.write_fence = factory  # type: ignore[attr-defined]
    return backends


def _engines(*, book_fences: bool) -> tuple[dict[str, Any], dict[str, MemoryFences], Recorded]:
    journal = Recorded()
    pg = MemoryEngine("postgres", name="pg-main", journal=journal)
    book = MemoryEngine("orderbook", name="book-1", journal=journal, can_keep_bookkeeping=False)
    backends = _fenced(pg)
    if book_fences:
        _fenced(book)
    return {"pg-main": pg, "book-1": book}, backends, journal


def test_schema_preparation_prepares_a_generation_only_where_the_map_carries_one() -> None:
    model = _model()
    engines, backends, journal = _engines(book_fences=False)
    sde.prepare_schema(model, _map(model), engines, project_id=PROJECT)
    assert [call["call"] for call in journal.as_list()] == ["ensure_schema", "ensure_schema"]
    assert set(backends) == {"reading"}
    assert backends["reading"].column == "valid"
    assert engines["book-1"].tables == {"tick": []}


def test_schema_preparation_refuses_an_engine_that_fences_before_creating_anything() -> None:
    model = _model()
    engines, backends, journal = _engines(book_fences=True)
    with pytest.raises(sde.MigrationRefused, match="carries no write generation on book-1"):
        sde.prepare_schema(model, _map(model), engines, project_id=PROJECT)
    assert journal.as_list() == [] and backends == {}
    assert engines["pg-main"].tables == {} and engines["book-1"].tables == {}


def test_schema_preparation_still_needs_every_engine_the_map_names() -> None:
    model = _model()
    engines, _, _ = _engines(book_fences=False)
    del engines["book-1"]
    with pytest.raises(sde.MigrationRefused, match="missing an engine named by the map"):
        sde.prepare_schema(model, _map(model), engines, project_id=PROJECT)


def test_operator_inspection_needs_no_engine_for_a_group_without_a_generation() -> None:
    model = _model()
    engines, _, _ = _engines(book_fences=False)
    placement = _map(model)
    context = sde.InspectionContext(model, placement, {"pg-main": engines["pg-main"]}, PROJECT)
    assert set(context.engines) == {"pg-main"}
    with pytest.raises(sde.MigrationRefused, match=r"missing engines \['pg-main'\]"):
        sde.InspectionContext(model, placement, {"book-1": engines["book-1"]}, PROJECT)


def test_the_fenced_groups_are_the_ones_with_a_generation_in_name_order() -> None:
    from sde.generation import fenced_groups

    model = _model()
    assert list(fenced_groups(_map(model))) == ["Reading"]

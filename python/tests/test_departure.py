"""What a departure renders, and what it refuses, without an engine.

The live half, `test_departure_live.py`, runs the text on PostgreSQL, ClickHouse and Elasticsearch.
This half pins the text itself: an administrator reads it before running it, so it is part of the
interface rather than an implementation detail.
"""

from __future__ import annotations

from typing import Any

import pytest

import sde
from sde.departure import (
    _run,
    bookkeeping_statement,
    bookkeeping_tables,
    column_statement,
    release_statements,
)

PROJECT = "a" * 32


def _map(contract: int = 4) -> tuple[sde.LogicalModel, sde.PlacementMap]:
    sde.clear_registry()

    @sde.entity
    class Reading:
        id: int

    @sde.entity
    class Tick:
        id: int

    model = sde.build_model(Reading, Tick)
    groups: dict[str, Any] = {
        "Reading": {
            "source": {
                "id": "source",
                "engine": "pg",
                "layout": {
                    "tables": {"Reading": 'read "ings'},
                    "columns": {"Reading": {"id": "bigint"}},
                },
            }
        },
        "Tick": {
            "source": {
                "id": "source",
                "engine": "ch",
                "layout": {"tables": {"Tick": "tick`s"}, "columns": {"Tick": {"id": "Int64"}}},
            }
        },
    }
    raw: dict[str, Any] = {
        "contract": contract,
        "model_version": model.version,
        "map_version": 3,
        "groups": groups,
    }
    if contract >= 4:
        raw["project_id"] = PROJECT
        groups["Reading"]["write_epoch"] = 2
        groups["Tick"]["write_epoch"] = 5
    return model, sde.load_map(raw, model=model)


def test_release_drops_every_fence_constraint_and_ownership_last() -> None:
    names = [
        "__sde_f_owner_" + PROJECT, "__sde_f_min_2", "__sde_f_max_2",
        "__sde_f_retired_" + "1" * 32, "unrelated_check",
    ]
    assert release_statements("postgres", 'read "ings', names) == (
        'ALTER TABLE "read ""ings" DROP CONSTRAINT IF EXISTS "__sde_f_max_2"',
        'ALTER TABLE "read ""ings" DROP CONSTRAINT IF EXISTS "__sde_f_min_2"',
        'ALTER TABLE "read ""ings" DROP CONSTRAINT IF EXISTS "__sde_f_retired_' + "1" * 32 + '"',
        'ALTER TABLE "read ""ings" DROP CONSTRAINT IF EXISTS "__sde_f_owner_' + PROJECT + '"',
    )
    # A constraint outside our namespace is the client's, and nothing here names it.
    assert "unrelated_check" not in " ".join(release_statements("clickhouse", "t", names))
    assert release_statements("clickhouse", "tick`s", ["__sde_f_min_5"]) == (
        "ALTER TABLE `tick\\`s` DROP CONSTRAINT IF EXISTS `__sde_f_min_5`",
    )


def test_elasticsearch_release_unsets_the_final_pipeline_before_deleting_it() -> None:
    assert release_statements("elasticsearch", "readings", ["anything"]) == (
        'PUT /readings/_settings {"index":{"final_pipeline":null}}',
        "DELETE /_ingest/pipeline/__sde_f_readings",
    )
    with pytest.raises(sde.MigrationRefused, match="not an Elasticsearch index name"):
        release_statements("elasticsearch", "a/../_cluster", [])


def test_cleanup_drops_the_column_and_our_bookkeeping_and_refuses_anything_else() -> None:
    assert column_statement("postgres", "t") == (
        'ALTER TABLE "t" DROP COLUMN IF EXISTS "__sde_write_epoch"'
    )
    assert column_statement("clickhouse", "t") == (
        "ALTER TABLE `t` DROP COLUMN IF EXISTS `__sde_write_epoch`"
    )
    # Elasticsearch removes a mapped field only by a reindex: no statement, rather than a wrong one.
    assert column_statement("elasticsearch", "t") is None
    assert bookkeeping_tables("postgres") == ("sde_map_state", "sde_backfill_state")
    assert bookkeeping_tables("clickhouse") == (
        "sde_map_state", "sde_backfill_state", "__sde_fence_drains",
    )
    assert bookkeeping_statement("clickhouse", "__sde_fence_drains") == (
        "DROP TABLE IF EXISTS `__sde_fence_drains` SYNC"
    )
    assert bookkeeping_statement("elasticsearch", "sde_map_state") == (
        "DELETE /sde_map_state?ignore_unavailable=true"
    )
    with pytest.raises(sde.MigrationRefused, match="not bookkeeping"):
        bookkeeping_statement("postgres", "readings")
    with pytest.raises(sde.MigrationRefused, match="nothing of this library"):
        bookkeeping_statement("orderbook", "sde_map_state")


def test_the_script_from_a_map_names_the_constraints_its_generations_imply() -> None:
    _, placement = _map()
    scripts = {
        script.engine: script
        for script in sde.departure_script(placement, {"pg": "postgres", "ch": "clickhouse"})
    }
    assert scripts["pg"].release == (
        'ALTER TABLE "read ""ings" DROP CONSTRAINT IF EXISTS "__sde_f_max_2"',
        'ALTER TABLE "read ""ings" DROP CONSTRAINT IF EXISTS "__sde_f_min_2"',
        'ALTER TABLE "read ""ings" DROP CONSTRAINT IF EXISTS "__sde_f_owner_' + PROJECT + '"',
    )
    assert scripts["ch"].release[0] == (
        "ALTER TABLE `tick\\`s` DROP CONSTRAINT IF EXISTS `__sde_f_max_5`"
    )
    assert scripts["pg"].cleanup == (
        'ALTER TABLE "read ""ings" DROP COLUMN IF EXISTS "__sde_write_epoch"',
        'DROP TABLE IF EXISTS "sde_map_state"',
        'DROP TABLE IF EXISTS "sde_backfill_state"',
    )
    assert scripts["ch"].cleanup[-1] == "DROP TABLE IF EXISTS `__sde_fence_drains` SYNC"


def test_a_map_without_generations_has_nothing_to_release_and_an_unknown_engine_nothing() -> None:
    _, placement = _map(contract=3)
    scripts = sde.departure_script(placement, {"pg": "postgres", "ch": "orderbook"})
    assert [script.engine for script in scripts] == ["pg"]
    assert scripts[0].release == ()
    assert scripts[0].cleanup == (
        'DROP TABLE IF EXISTS "sde_map_state"',
        'DROP TABLE IF EXISTS "sde_backfill_state"',
    )


def test_reading_the_engines_needs_every_fenced_engine_and_the_local_project() -> None:
    _, placement = _map()

    class Engine:
        dialect = "postgres"

    with pytest.raises(sde.MigrationRefused, match=r"every engine of a group.*\['ch'\]"):
        sde.departure(placement, {"pg": Engine()}, project_id=PROJECT)
    # The project decides which fences are ours: configured locally, never read off the map.
    with pytest.raises(sde.MigrationRefused, match="locally configured project_id"):
        sde.departure(placement, {"pg": Engine(), "ch": Engine()})


def test_only_the_requests_a_departure_renders_are_run_on_elasticsearch() -> None:
    sent: list[tuple[str, str, Any]] = []

    class Engine:
        def _request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
            sent.append((method, path, body))
            return 200, {}

    _run("elasticsearch", Engine(), 'PUT /i/_settings {"index":{"final_pipeline":null}}')
    assert sent == [("PUT", "/i/_settings", {"index": {"final_pipeline": None}})]
    for line in ("GET /_cat/indices", "POST /i/_delete_by_query {}", "DELETE"):
        with pytest.raises(sde.MigrationRefused, match="not a request departure renders"):
            _run("elasticsearch", Engine(), line)

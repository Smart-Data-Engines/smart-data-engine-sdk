"""Historical row epochs are bookkeeping; a deferred write still belongs to its old session."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest

import sde
from sde.engines.clickhouse import ClickHouseEngine
from sde.engines.postgres import PostgresEngine

PROJECT = "1" * 32
HOLD = "5" * 32


@pytest.fixture
def engines() -> Iterator[dict[str, Any]]:
    pg_dsn, ch_dsn = os.environ.get("SDE_POSTGRES_DSN"), os.environ.get("SDE_CLICKHOUSE_DSN")
    if not pg_dsn or not ch_dsn:
        pytest.skip("both live engines are required for generation-aware migration")
    name = "sde_gen_copy_" + uuid4().hex[:16]
    with PostgresEngine(pg_dsn) as pg, ClickHouseEngine(ch_dsn) as admin:
        pg._cx.execute(f'CREATE SCHEMA "{name}"')
        pg._cx.execute(f'SET search_path TO "{name}"')
        admin._cx.command(f"CREATE DATABASE `{name}` ENGINE=Atomic")
        parts = urlsplit(ch_dsn)
        local = urlunsplit((parts.scheme, parts.netloc, "/" + name, parts.query, parts.fragment))
        with ClickHouseEngine(local) as ch:
            try:
                yield {"postgres": pg, "clickhouse": ch}
            finally:
                pg._cx.execute(f'DROP SCHEMA "{name}" CASCADE')
                ch._cx.command(f"DROP DATABASE `{name}` SYNC")


def fixture(source: str, epoch: int) -> tuple[sde.LogicalModel, sde.PlacementMap, str]:
    sde.clear_registry()

    @sde.entity
    class Event:
        id: int
        value: sde.Int32

    model = sde.build_model(Event)
    target = "clickhouse" if source == "postgres" else "postgres"

    def material(engine: str, role: str) -> dict[str, Any]:
        return {
            "id": role,
            "engine": engine,
            "layout": {
                "tables": {"Event": role + "_events"},
                "columns": {
                    "Event": {
                        "id": "bigint" if engine == "postgres" else "Int64",
                        "value": "integer" if engine == "postgres" else "Int32",
                    }
                },
            },
        }

    raw = {
        "contract": 4,
        "project_id": PROJECT,
        "model_version": model.version,
        "map_version": epoch,
        "groups": {
            "Event": {
                "write_epoch": epoch,
                "source": material(source, "source"),
                "derived": [{**material(target, "copy"), "lag_budget_ms": 60000}],
                "also_write": ["copy"],
            }
        },
    }
    return model, sde.load_map(raw, model=model), target


def advance(engines: dict[str, Any], placement: sde.PlacementMap, epoch: int) -> None:
    for material in placement.placement_of("Event").all():
        fence = engines[material.engine].write_fence(
            material.layout.table_for("Event"), project_id=PROJECT
        )
        fence.freeze(HOLD)
        fence.advance(epoch)
        fence.release(HOLD)


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_copy_stamps_the_active_epoch_and_verification_ignores_historical_epochs(
    engines: dict[str, Any], source: str
) -> None:
    model, first, target = fixture(source, 1)
    sde.prepare_schema(model, first, engines, project_id=PROJECT)
    engines[source].insert("source_events", {"id": 1, "value": 11, sde.WRITE_EPOCH_COLUMN: 1})
    advance(engines, first, 2)
    model, current, _target = fixture(source, 2)
    session = sde.Session(model, current, engines, project_id=PROJECT)
    sde.backfill(session, "Event")
    assert engines[target].get("copy_events", {"id": 1})[sde.WRITE_EPOCH_COLUMN] == 2
    assert engines[source].get("source_events", {"id": 1})[sde.WRITE_EPOCH_COLUMN] == 1
    assert sde.verify(session, "Event").matched
    session.save("Event", {"id": 2, "value": 22})
    assert engines[target].get("copy_events", {"id": 2})[sde.WRITE_EPOCH_COLUMN] == 2
    assert session.get("Event", {"id": 2}) == {"id": 2, "value": 22}


def test_deferred_copy_keeps_the_old_epoch_after_commit_and_a_new_session(
    engines: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    model, first, _target = fixture("postgres", 1)
    sde.prepare_schema(model, first, engines, project_id=PROJECT)
    old = sde.Session(model, first, engines, project_id=PROJECT)
    original = engines["postgres"].transaction

    @contextmanager
    def change_generation_after_commit() -> Iterator[Any]:
        with original() as context:
            yield context
        advance(engines, first, 2)
        new_model, current, _ = fixture("postgres", 2)
        sde.Session(new_model, current, engines, project_id=PROJECT)
        engines["clickhouse"].insert(
            "copy_events", {"id": 1, "value": 99, sde.WRITE_EPOCH_COLUMN: 2}
        )

    monkeypatch.setattr(engines["postgres"], "transaction", change_generation_after_commit)
    with old.transaction("Event"):
        old.save("Event", {"id": 1, "value": 11})
    assert engines["postgres"].get("source_events", {"id": 1})["value"] == 11
    assert engines["clickhouse"].get("copy_events", {"id": 1})["value"] == 99

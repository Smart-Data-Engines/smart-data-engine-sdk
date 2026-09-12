"""Each active session retains its own generation, even when it shares an engine connection."""

from __future__ import annotations

import base64
from dataclasses import replace
from typing import Any

import pytest
from test_write_fence_live import guarded as guarded

import sde

PROJECT = "1" * 32
HOLD = "4" * 32


def fixture(
    engine: Any,
    table: str,
    *,
    epoch: int = 1,
    version: int = 1,
    key: Any = None,
    with_label: bool = False,
) -> tuple[sde.LogicalModel, sde.PlacementMap]:
    sde.clear_registry()

    if with_label:

        @sde.entity
        class Record:
            id: int
            label: str
    else:

        @sde.entity
        class Record:
            id: int

    model = sde.build_model(Record)
    raw: dict[str, Any] = {
        "contract": 4,
        "project_id": PROJECT,
        "model_version": model.version,
        "map_version": version,
        "groups": {
            "Record": {
                "write_epoch": epoch,
                "source": {
                    "id": "source",
                    "engine": "db",
                    "layout": {
                        "tables": {"Record": table},
                        "columns": {
                            "Record": {"id": "bigint" if engine.dialect == "postgres" else "Int64"}
                        },
                    },
                },
            }
        },
    }
    if with_label:
        raw["groups"]["Record"]["source"]["layout"]["columns"]["Record"]["label"] = (
            "text" if engine.dialect == "postgres" else "String"
        )
    public = None
    if key is not None:
        raw["signature"] = {
            "alg": "ed25519",
            "value": base64.b64encode(key.sign(sde.canonical_bytes(raw))).decode(),
        }
        public = key.public_key().public_bytes_raw()
    return model, sde.load_map(raw, model=model, public_key=public)


@pytest.mark.parametrize("guarded", ["postgres", "clickhouse"], indirect=True)
def test_existing_session_cannot_inherit_a_new_sessions_write_epoch(guarded: Any) -> None:
    engine, table, fence = guarded
    model, first_map = fixture(engine, table)
    sde.prepare_schema(model, first_map, {"db": engine}, project_id=PROJECT)
    old = sde.Session(model, first_map, {"db": engine}, project_id=PROJECT)
    old.save("Record", {"id": 1})
    assert old.get("Record", {"id": 1}) == {"id": 1}
    assert engine.get(table, {"id": 1})[sde.WRITE_EPOCH_COLUMN] == 1
    fence.freeze(HOLD)
    fence.advance(2)
    fence.release(HOLD)
    model, second_map = fixture(engine, table, epoch=2, version=2)
    current = sde.Session(model, second_map, {"db": engine}, project_id=PROJECT)
    with pytest.raises(sde.EngineError):
        old.save("Record", {"id": 2})
    current.save("Record", {"id": 3})
    assert current.get("Record", {"id": 3}) == {"id": 3}
    assert current.get("Record", {"id": 1}) == {"id": 1}
    assert engine.get(table, {"id": 3})[sde.WRITE_EPOCH_COLUMN] == 2
    assert engine.count(table) == 2


@pytest.mark.parametrize("guarded", ["postgres", "clickhouse"], indirect=True)
def test_future_map_is_refused_before_it_moves_the_watermark(guarded: Any) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    engine, table, _fence = guarded
    key = Ed25519PrivateKey.generate()
    model, first = fixture(engine, table, key=key)
    sde.prepare_schema(model, first, {"db": engine}, project_id=PROJECT)
    sde.Session(model, first, {"db": engine}, project_id=PROJECT)
    assert engine.map_watermark() == 1
    model, future = fixture(engine, table, epoch=2, version=2, key=key)
    with pytest.raises(sde.MigrationRefused, match="write generation"):
        sde.Session(model, future, {"db": engine}, project_id=PROJECT)
    assert engine.map_watermark() == 1
    with pytest.raises(sde.MigrationRefused, match="locally configured"):
        sde.Session(model, first, {"db": engine}, project_id="9" * 32)
    assert engine.map_watermark() == 1


@pytest.mark.parametrize("guarded", ["postgres", "clickhouse"], indirect=True)
def test_runtime_schema_check_does_not_provision_or_override_an_epoch(
    guarded: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, table, _fence = guarded
    model, placement = fixture(engine, table)
    sde.prepare_schema(model, placement, {"db": engine}, project_id=PROJECT)
    session = sde.Session(model, placement, {"db": engine}, project_id=PROJECT)

    def no_ddl(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("runtime tried to provision schema")

    monkeypatch.setattr(engine, "ensure_schema", no_ddl)
    session.ensure_schema()
    with pytest.raises(sde.MigrationRefused, match="reserved"):
        session.save("Record", {"id": 1, sde.WRITE_EPOCH_COLUMN: 1})
    with pytest.raises(sde.MigrationRefused, match="immutable loaded"):
        sde.Session(model, replace(placement), {"db": engine}, project_id=PROJECT)
    assert engine.count(table) == 0


@pytest.mark.parametrize("guarded", ["postgres", "clickhouse"], indirect=True)
def test_native_epoch_is_not_a_substitute_for_the_model_columns(guarded: Any) -> None:
    from sde.schema import QUOTE

    engine, table, _fence = guarded
    model, placement = fixture(engine, table, with_label=True)
    column = "text" if engine.dialect == "postgres" else "String"
    add = f"ALTER TABLE {QUOTE[engine.dialect](table)} ADD COLUMN label {column}"
    if engine.dialect == "postgres":
        engine._cx.execute(add)
    else:
        engine._cx.command(add)
    sde.prepare_schema(model, placement, {"db": engine}, project_id=PROJECT)
    statement = f"ALTER TABLE {QUOTE[engine.dialect](table)} RENAME COLUMN label TO other_label"
    if engine.dialect == "postgres":
        engine._cx.execute(statement)
    else:
        engine._cx.command(statement)
    with pytest.raises(sde.EngineError, match="different shape"):
        sde.Session(model, placement, {"db": engine}, project_id=PROJECT)

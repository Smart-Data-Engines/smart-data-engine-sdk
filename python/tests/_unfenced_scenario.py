"""A map with a group that carries no write generation, through a staging and a cutover, live.

Map contract 6 lets a group leave ``write_epoch`` out when its engine cannot fence writes - our own
orderbook engine. The operator then works around that engine rather than through it: it needs no
binding for it, reads none of its tables, and refuses a binding offered for it. The fenced group in
the same map moves from PostgreSQL to ClickHouse as usual, and the other group is still where it
was, written and read by the same sessions.

Shared by two runners, because the engine without fences comes in two kinds: the SDK's memory
engine, in the ``python`` job's live slices, and the orderbook engine itself, in the ``orderbook``
job (``test_orderbook_three_engines.py``).
"""

from __future__ import annotations

import base64
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from test_runtime_privileges_live import runtime_roles

import sde

PROJECT = "1" * 32
T0 = 1_790_000_000_000_000_000


def model() -> sde.LogicalModel:
    from sde.testing.loader import model_from_neutral

    sde.clear_registry()
    return model_from_neutral(
        {
            "entities": [
                {
                    "name": "Event",
                    "fields": [
                        {"name": "id", "type": "int64"},
                        {"name": "value", "type": "int32"},
                    ],
                    "key": ["id"],
                },
                {
                    "name": "DepthLevel",
                    "fields": [
                        {"name": name, "type": kind, "nullable": name == "sequence_number"}
                        for name, kind in sde.ORDERBOOK_SHAPE.items()
                    ],
                    "key": list(sde.ORDERBOOK_KEY),
                },
            ],
            "relations": [],
            "atomic": [],
        }
    )


def material(engine: str, table: str, identity: str) -> dict[str, Any]:
    return {
        "id": identity,
        "engine": engine,
        "layout": {
            "tables": {"Event": table},
            "columns": {
                "Event": {
                    "id": "bigint" if engine == "postgres" else "Int64",
                    "value": "integer" if engine == "postgres" else "Int32",
                }
            },
        },
    }


def level(symbol: str, price: int, offset: int) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "exchange": "binance",
        "timestamp_ns": T0 + offset,
        "side": "bid",
        "level": 0,
        "price": price,
        "quantity": 5,
        "order_count": 1,
        "sequence_number": None,
    }


def run(book: Any, tmp_path: Path, *, symbol: str) -> None:
    """Stage and cut over Event while DepthLevel stays on ``book``, an engine without fences."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    logical = model()
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw()

    def signed(raw: dict[str, Any]) -> dict[str, Any]:
        document = deepcopy(raw)
        document.pop("signature", None)
        document["signature"] = {
            "alg": "ed25519",
            "value": base64.b64encode(key.sign(sde.canonical_bytes(document))).decode(),
        }
        return document

    depth = next(group for group in sde.colocation_groups(logical) if group.name == "DepthLevel")
    layout = sde.default_layout(logical, depth, dialect="orderbook")
    book_layout = {
        "tables": dict(layout.tables),
        "columns": {entity: dict(columns) for entity, columns in layout.columns.items()},
    }
    current = signed(
        {
            "contract": 6,
            "project_id": PROJECT,
            "model_version": logical.version,
            "map_version": 1,
            "groups": {
                "Event": {
                    "write_epoch": 1,
                    "source": material("postgres", "initial_events", "source"),
                },
                # No write_epoch: the engine cannot fence, so the group carries no generation.
                "DepthLevel": {"source": {"id": "book", "engine": "book", "layout": book_layout}},
            },
        }
    )
    with runtime_roles("postgres") as pg, runtime_roles("clickhouse") as ch:
        roles = {"postgres": pg, "clickhouse": ch}
        parsed = sde.load_map(current, model=logical, public_key=public)
        operators = {name: role.operator for name, role in roles.items()}
        sde.prepare_schema(logical, parsed, {**operators, "book": book}, project_id=PROJECT)
        pg.grant("initial_events")
        for role in roles.values():
            role.grant("sde_map_state")
        runtime = {"postgres": pg.runtime, "clickhouse": ch.runtime, "book": book}
        old = sde.Session(logical, parsed, runtime, project_id=PROJECT)
        old.save("Event", {"id": 1, "value": 11})
        old.save("DepthLevel", level(symbol, 100, 1))

        # The operator is never given the engine without fences, and refuses it when it is.
        with pytest.raises(sde.MigrationRefused, match="leave its engine out of the operator"):
            sde.LocalCutover(
                tmp_path / "refused",
                model=logical,
                project_id=PROJECT,
                public_key=public,
                operators={**operators, "book": book},
                runtime={"postgres": [pg.runtime], "clickhouse": [ch.runtime], "book": [book]},
            )
        operator = sde.LocalCutover(
            tmp_path / "project",
            model=logical,
            project_id=PROJECT,
            public_key=public,
            operators=operators,
            runtime={name: [role.runtime] for name, role in roles.items()},
        )
        operator.enroll(current)

        stage_id = uuid4().hex
        prepared = deepcopy(current)
        prepared["map_version"] = 2
        copy = {
            **material("clickhouse", sde.staging_table_name(stage_id, 1), "copy"),
            "lag_budget_ms": 30000,
        }
        prepared["groups"]["Event"].update(derived=[copy], also_write=["copy"])
        stage = sde.load_staging_plan(
            signed(
                {
                    "kind": "sde-stage",
                    "protocol": 1,
                    "stage_id": stage_id,
                    "project_id": PROJECT,
                    "group": "Event",
                    "current": current,
                    "prepared": signed(prepared),
                }
            ),
            model=logical,
            project_id=PROJECT,
            public_key=public,
        )
        assert operator.stage(stage).as_record()["outcome"] == "prepared"
        staged = sde.Session(logical, operator.active_map(), runtime, project_id=PROJECT)
        staged.save("Event", {"id": 2, "value": 22})
        staged.save("DepthLevel", level(symbol, 101, 2))

        before = stage.as_record()["prepared"]
        target = deepcopy(before["groups"]["Event"]["derived"][0])
        target.pop("lag_budget_ms")
        success, abort = deepcopy(before), deepcopy(before)
        success["map_version"], abort["map_version"] = 3, 4
        success["groups"]["Event"] = {"source": target, "write_epoch": 3}
        abort["groups"]["Event"] = {"source": before["groups"]["Event"]["source"], "write_epoch": 2}
        request = sde.verification_request(
            stage.prepared,
            project_id=PROJECT,
            group="Event",
            request_id=uuid4().hex,
            requested_at=datetime.now(UTC).isoformat(),
        )
        plan = sde.load_cutover_plan(
            signed(
                {
                    "kind": "sde-cutover",
                    "protocol": 1,
                    "plan_id": uuid4().hex,
                    "project_id": PROJECT,
                    "group": "Event",
                    "before": before,
                    "success": signed(success),
                    "abort": signed(abort),
                    "verification": request.as_record(),
                    "pause_budget_ms": 30000,
                    "query_impact_digest": "a" * 64,
                }
            ),
            model=logical,
            project_id=PROJECT,
            public_key=public,
        )
        assert operator.execute(plan).as_record()["outcome"] == "success"

        moved = sde.Session(logical, operator.active_map(), runtime, project_id=PROJECT)
        assert moved.placement.groups["Event"].source.engine == "clickhouse"
        assert moved.placement.groups["DepthLevel"].write_epoch is None
        for identity in (1, 2):
            assert moved.get("Event", {"id": identity}) == {"id": identity, "value": identity * 11}
        moved.save("DepthLevel", level(symbol, 102, 3))
        for offset, price in ((1, 100), (2, 101), (3, 102)):
            key = {
                "symbol": symbol,
                "exchange": "binance",
                "timestamp_ns": T0 + offset,
                "side": "bid",
                "level": 0,
            }
            row = moved.get("DepthLevel", key)
            assert row is not None and row["price"] == price, (offset, row)
        # The old session's Event writes are cut off by their generation; its DepthLevel writes are
        # not, because that group carries none - and it never moves, so there is nothing to cut off.
        with pytest.raises(sde.EngineError):
            old.save("Event", {"id": 9, "value": 99})
        old.save("DepthLevel", level(symbol, 103, 4))

"""Signed synthetic authorizations for the local executor qualification, without planner code."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import sde


def stage(configured: dict[str, Any], version: int) -> sde.StagingPlan:
    if "issue_stage" in configured:
        provided = configured["issue_stage"](configured, version)
        if not isinstance(provided, sde.StagingPlan):
            raise TypeError("external issuer did not return a loaded staging authorization")
        return provided
    model = configured["model"]
    current = configured["operator"].store.read()["active_map"]
    group = sde.colocation_groups(model)[0]
    source = current["groups"][group.name]["source"]["engine"]
    target = "clickhouse" if source == "postgres" else "postgres"
    identity = uuid4().hex
    layout = sde.default_layout(model, group, dialect=target)
    tables = {
        entity: sde.staging_table_name(identity, index)
        for index, entity in enumerate(sorted(group.members), start=1)
    }
    physical = {
        "tables": tables,
        "columns": {entity: dict(fields) for entity, fields in layout.columns.items()},
        "indexes": [
            {**index, "name": f"sde_i_{identity}_{position:06d}"}
            for position, index in enumerate(layout.indexes, start=1)
        ],
    }
    prepared = deepcopy(current)
    prepared["map_version"] = version
    prepared["groups"][group.name].update(
        derived=[
            {"id": "copy-" + identity, "engine": target, "layout": physical, "lag_budget_ms": 30000}
        ],
        also_write=["copy-" + identity],
    )
    sign = configured["signed"]
    return sde.load_staging_plan(
        sign(
            {
                "kind": "sde-stage",
                "protocol": 1,
                "stage_id": identity,
                "project_id": configured["project_id"],
                "group": group.name,
                "current": current,
                "prepared": sign(prepared),
            }
        ),
        model=model,
        project_id=configured["project_id"],
        public_key=configured["public"],
    )


def cutover(configured: dict[str, Any], staged: sde.StagingPlan) -> sde.CutoverPlan:
    if "issue_cutover" in configured:
        provided = configured["issue_cutover"](configured, staged)
        if not isinstance(provided, sde.CutoverPlan):
            raise TypeError("external issuer did not return a loaded cutover authorization")
        return provided
    before = staged.as_record()["prepared"]
    spot = before["groups"][staged.group]
    source = deepcopy(spot["source"])
    target = deepcopy(spot["derived"][0])
    target.pop("lag_budget_ms")
    success, abort = deepcopy(before), deepcopy(before)
    success["map_version"], abort["map_version"] = (
        before["map_version"] + 1,
        before["map_version"] + 2,
    )
    success["groups"][staged.group] = {"source": target, "write_epoch": spot["write_epoch"] + 2}
    abort["groups"][staged.group] = {"source": source, "write_epoch": spot["write_epoch"] + 1}
    request = sde.verification_request(
        staged.prepared,
        project_id=staged.project_id,
        group=staged.group,
        request_id=uuid4().hex,
        requested_at=datetime.now(UTC).isoformat(),
    )
    sign = configured["signed"]
    return sde.load_cutover_plan(
        sign(
            {
                "kind": "sde-cutover",
                "protocol": 1,
                "plan_id": uuid4().hex,
                "project_id": staged.project_id,
                "group": staged.group,
                "before": before,
                "success": sign(success),
                "abort": sign(abort),
                "verification": request.as_record(),
                "pause_budget_ms": 30000,
                "query_impact_digest": "a" * 64,
            }
        ),
        model=configured["model"],
        project_id=staged.project_id,
        public_key=configured["public"],
    )

"""Durable preparation of a fresh physical copy using only customer-local connections."""

from __future__ import annotations

import json
from time import monotonic_ns
from typing import TYPE_CHECKING, Any

from .errors import MigrationRefused
from .groups import colocation_groups
from .local_cutover import CutoverRecoveryRequired
from .placement import WATERMARK_TABLE
from .staging import StagingPlan, StagingReceipt, load_staging_plan

if TYPE_CHECKING:
    from .local_cutover import LocalCutover


def _snapshot(operator: LocalCutover, plan: StagingPlan, state: dict[str, Any]) -> dict[str, Any]:
    from .engines._staging import NativeStaging, creation_marker

    plan.check_current(operator.active_map())
    needed = {
        material.engine for placed in plan.prepared.groups.values() for material in placed.all()
    }
    if not needed <= set(operator.engines):
        raise MigrationRefused("staging is missing a configured local engine binding")
    group = next(value for value in colocation_groups(operator.model) if value.name == plan.group)
    target = plan.prepared.groups[plan.group].derived[0]
    keys = {entity: operator.model.entity(entity).key for entity in group.members}
    NativeStaging(operator.native[target.engine]).preflight(target.layout, keys)
    sources = []
    allowed: dict[str, set[str]] = {name: {WATERMARK_TABLE} for name in operator.engines}
    for placed in plan.current.groups.values():
        for material in placed.all():
            engine = operator.engines[material.engine]
            engine.validate_schema(material.layout)
            for table in material.layout.tables.values():
                allowed[material.engine].add(table)
                identity = operator.native[material.engine].identity(table)
                state_ = engine.write_fence(table, project_id=operator.project_id).state()
                if not state_.complete or state_.epoch != placed.write_epoch or state_.holds:
                    raise MigrationRefused(
                        "staging needs the current generation without another barrier"
                    )
                sources.append(
                    {
                        "engine": material.engine,
                        "identity": identity.as_record(),
                        "epoch": placed.write_epoch,
                    }
                )
    allowed[target.engine].update(target.layout.tables.values())
    bindings = {}
    for name, native in operator.native.items():
        value = operator.engines[name].map_watermark()
        if value is not None and value > plan.current.map_version:
            raise MigrationRefused("a newer map was adopted before local staging")
        native.qualify([WATERMARK_TABLE], sorted(allowed[name]))
        bindings[name] = {
            "endpoint": list(native.endpoint()),
            "watermark_identity": native.identity(WATERMARK_TABLE).as_record(),
            "principals": dict(native.principal_ids),
            "allowed_tables": sorted(allowed[name]),
        }
    table_names = set(target.layout.tables.values())
    if any(row[-1] in table_names for row in state["retired_names"]):
        raise MigrationRefused("staging cannot reuse a retired physical name")
    for previous in state.get("stages", {}).values():
        if any(row["identity"]["name"] in table_names for row in previous["receipt"]["tables"]):
            raise MigrationRefused("staging cannot reuse a previously prepared physical name")
    return {
        "kind": "staging",
        "plan_id": plan.stage_id,
        "plan_fingerprint": plan.fingerprint,
        "plan": plan.as_record(),
        "phase": "stage_prepared",
        "pending_hold": None,
        "decision": None,
        "sources": sources,
        "bindings": bindings,
        "tables": [
            {
                "entity": entity,
                "engine": target.engine,
                "table": table,
                "marker": creation_marker(operator.project_id, plan.stage_id, table),
                "identity": None,
            }
            for entity, table in sorted(target.layout.tables.items())
        ],
    }


def _recheck_sources(operator: LocalCutover, execution: dict[str, Any]) -> None:
    from .engines._operator import TableIdentity

    for row in execution["sources"]:
        expected = TableIdentity(**row["identity"])
        native = operator.native[row["engine"]]
        if native.identity(expected.name).physical_key != expected.physical_key:
            raise MigrationRefused("a current table was replaced during staging")
        state = (
            operator.engines[row["engine"]]
            .write_fence(expected.name, project_id=operator.project_id)
            .state()
        )
        if not state.complete or state.epoch != row["epoch"] or state.holds:
            raise MigrationRefused("the current write generation changed during staging")


def _finish(
    operator: LocalCutover, state: dict[str, Any], plan: StagingPlan, *, recovered: bool
) -> StagingReceipt:
    from .engines._operator import TableIdentity
    from .engines._staging import NativeStaging

    execution = state["execution"]
    target = plan.prepared.groups[plan.group].derived[0]
    epoch = plan.prepared.groups[plan.group].write_epoch
    assert epoch is not None
    keys = {entity: operator.model.entity(entity).key for entity in target.layout.tables}
    for name, binding in execution["bindings"].items():
        native = operator.native[name]
        if list(native.endpoint()) != binding["endpoint"]:
            raise MigrationRefused("a staging binding names another native database")
        expected_watermark = TableIdentity(**binding["watermark_identity"])
        if native.identity(WATERMARK_TABLE).physical_key != expected_watermark.physical_key:
            raise MigrationRefused("a staging namespace or watermark identity changed")
        native.qualify([WATERMARK_TABLE], binding["allowed_tables"])
        if native.principal_ids != binding["principals"]:
            raise MigrationRefused("a runtime login identity changed during staging")
    _recheck_sources(operator, execution)
    creator = NativeStaging(operator.native[target.engine])
    for row in execution["tables"]:

        def create(row: dict[str, Any] = row) -> None:
            identity = creator.create_table(
                entity=row["entity"], layout=target.layout, keys=keys, marker=row["marker"]
            )
            if row["identity"] is not None and identity.as_record() != row["identity"]:
                raise MigrationRefused("a stage-owned table was replaced")
            row["identity"] = identity.as_record()

        operator._step(state, "stage_create_" + row["entity"], create)

        def prepare_generation(row: dict[str, Any] = row) -> None:
            operator.engines[target.engine].write_fence(
                row["table"], project_id=operator.project_id
            ).prepare(epoch)

        operator._step(state, "stage_generation_" + row["entity"], prepare_generation)
    operator._step(state, "stage_indexes", lambda: creator.create_indexes(target.layout))

    def qualify() -> None:
        operator.engines[target.engine].validate_schema(target.layout)
        native = operator.native[target.engine]
        native.qualify(
            sorted(target.layout.tables.values()),
            execution["bindings"][target.engine]["allowed_tables"],
            required_access=False,
        )
        if native.principal_ids != execution["bindings"][target.engine]["principals"]:
            raise MigrationRefused("a runtime login changed before staging grants")
        for row in execution["tables"]:
            expected = TableIdentity(**row["identity"])
            if native.identity(expected.name).physical_key != expected.physical_key:
                raise MigrationRefused("a stage-owned table was replaced before activation")
            observed = (
                operator.engines[target.engine]
                .write_fence(expected.name, project_id=operator.project_id)
                .state()
            )
            if not observed.complete or observed.epoch != epoch or observed.closed:
                raise MigrationRefused("the staged write generation is not ready for activation")

    operator._step(state, "stage_qualify", qualify)
    operator._step(
        state,
        "stage_grants",
        lambda: operator.native[target.engine].access(
            [TableIdentity(**row["identity"]) for row in execution["tables"]], enabled=True
        ),
    )
    _recheck_sources(operator, execution)
    qualify()
    execution["decision"] = "prepared"
    operator.store.write(state)
    operator._after_step("stage_decision")

    def watermarks() -> None:
        for engine in operator.engines.values():
            observed = engine.map_watermark()
            if observed is not None and observed > plan.prepared.map_version:
                raise MigrationRefused("a newer map appeared while finishing staging")
            engine.record_map_version(
                plan.prepared.map_version, model_version=operator.model.version
            )

    operator._step(state, "stage_watermarks", watermarks)

    def publish() -> None:
        current = operator.active_map()
        if current.fingerprint not in (plan.current.fingerprint, plan.prepared.fingerprint):
            raise MigrationRefused("another active map replaced the staging instruction")
        operator.store.publish(plan.prepared_payload())

    operator._step(state, "stage_publish", publish)
    receipt = {
        "protocol": 1,
        "stage_id": plan.stage_id,
        "stage_fingerprint": plan.fingerprint,
        "project_id": operator.project_id,
        "group": plan.group,
        "outcome": "prepared",
        "map_version": plan.prepared.map_version,
        "map_fingerprint": plan.prepared.fingerprint,
        "tables": [
            {"engine": row["engine"], "entity": row["entity"], "identity": row["identity"]}
            for row in execution["tables"]
        ],
        "elapsed_ms": operator._elapsed(),
        "recovered": recovered,
    }
    state["stages"][plan.stage_id] = {
        "plan_fingerprint": plan.fingerprint,
        "plan": plan.as_record(),
        "receipt": receipt,
    }
    state["active_map"], state["execution"] = json.loads(plan.prepared_payload()), None
    operator.store.write(state)
    operator._after_step("stage_complete")
    return StagingReceipt(receipt)


def execute_stage(operator: LocalCutover, plan: StagingPlan) -> StagingReceipt:
    with operator.store.lock():
        state = operator.store.read()
        complete = state.get("stages", {}).get(plan.stage_id)
        if complete is not None:
            if complete["plan_fingerprint"] != plan.fingerprint:
                raise MigrationRefused("the completed staging id names another authorization")
            operator.store.confirm()
            return StagingReceipt(complete["receipt"])
        if state["execution"] is not None:
            raise CutoverRecoveryRequired("an unfinished local operation requires resume")
        execution = _snapshot(operator, plan, state)
        state.setdefault("stages", {})
        state["execution"] = execution
        operator.store.write(state)
        operator._after_step("stage_prepared")
        operator._started, operator._enforce_budget = monotonic_ns(), False
        try:
            return _finish(operator, state, plan, recovered=False)
        except Exception as exc:
            raise CutoverRecoveryRequired(
                "local staging needs recovery; preserve its project state"
            ) from exc


def resume_stage(operator: LocalCutover, state: dict[str, Any]) -> StagingReceipt:
    execution = state["execution"]
    plan = load_staging_plan(
        execution["plan"],
        model=operator.model,
        project_id=operator.project_id,
        public_key=operator.keys,
    )
    if plan.fingerprint != execution["plan_fingerprint"]:
        raise MigrationRefused("staging recovery names another signed authorization")
    operator._started, operator._enforce_budget = monotonic_ns(), False
    try:
        return _finish(operator, state, plan, recovered=True)
    except Exception as exc:
        raise CutoverRecoveryRequired(
            "staging recovery remains incomplete; preserve its state"
        ) from exc

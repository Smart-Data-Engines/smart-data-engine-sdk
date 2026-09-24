"""Durable in-place index builds using only customer-local connections.

The build pauses nothing: writers on the map in force keep writing to the same tables while the
index is built beside them, and the next map differs only by declaring it. So the operation holds no
barrier and raises no generation - it refuses to start next to anybody else's barrier instead - and
its two terminal outcomes are ``built`` (the next map is published) and ``abandoned`` (this build's
own indexes are removed and the map in force stays). Without a decision a recovery builds on:
unlike a cutover, where the source stands frozen until a decision, here nothing waits, and an index
that gets built is harmless.
"""

from __future__ import annotations

import json
from functools import partial
from time import monotonic_ns
from typing import TYPE_CHECKING, Any

from .errors import MigrationRefused
from .index_build import IndexPlan, IndexReceipt, load_index_plan
from .local_cutover import CutoverRecoveryRequired
from .physical import METHODS_BY_DIALECT, index_method, refuse_findings
from .placement import WATERMARK_TABLE

if TYPE_CHECKING:
    from .local_cutover import LocalCutover


def _snapshot(operator: LocalCutover, plan: IndexPlan, state: dict[str, Any]) -> dict[str, Any]:
    from .engines._index_build import NativeIndexBuild

    plan.check_current(operator.active_map())
    placement = plan.current.groups[plan.group]
    source = placement.source
    if source.engine not in operator.engines:
        raise MigrationRefused("an index build is missing its configured local engine binding")
    native = operator.native[source.engine]
    for index in plan.added:
        if index_method(index) not in METHODS_BY_DIALECT.get(native.dialect, ()):
            raise MigrationRefused(
                f"{native.dialect} cannot build a {index_method(index)} index in place"
            )
    engine = operator.engines[source.engine]
    # The design in force, read back before any DDL: a build beside a table that already differs
    # from its map would be refused only at publication, after the work.
    keys = {entity: operator.model.entity(entity).key for entity in source.layout.tables}
    refuse_findings(engine.validate_schema(source.layout, keys=keys), MigrationRefused)
    tables: dict[str, dict[str, str]] = {}
    builder = NativeIndexBuild(native)
    for entity, table in sorted(source.layout.tables.items()):
        identity = native.identity(table)
        observed = engine.write_fence(table, project_id=operator.project_id).state()
        if not observed.complete or observed.epoch != placement.write_epoch or observed.holds:
            # A build beside another operation's barrier would publish a map that operation's
            # own next map does not know about.
            raise MigrationRefused(
                "an index build needs the current generation without another barrier"
            )
        tables[entity] = identity.as_record()
        for index in plan.added:
            if index["entity"] == entity:
                status, reason = builder.inspect(identity, index)
                if status == "foreign":
                    raise MigrationRefused(reason)
    allowed: dict[str, set[str]] = {name: {WATERMARK_TABLE} for name in operator.engines}
    for placed in plan.current.groups.values():
        for material in placed.all():
            allowed[material.engine].update(material.layout.tables.values())
    bindings = {}
    for name, driver in operator.native.items():
        value = operator.engines[name].map_watermark()
        if value is not None and value > plan.current.map_version:
            raise MigrationRefused("a newer map was adopted before this index build")
        driver.qualify([WATERMARK_TABLE], sorted(allowed[name]))
        bindings[name] = {
            "endpoint": list(driver.endpoint()),
            "watermark_identity": driver.identity(WATERMARK_TABLE).as_record(),
            "principals": dict(driver.principal_ids),
            "allowed_tables": sorted(allowed[name]),
        }
    return {
        "kind": "index",
        "plan_id": plan.index_id,
        "plan_fingerprint": plan.fingerprint,
        "plan": plan.as_record(),
        "phase": "index_prepared",
        "pending_hold": None,
        "decision": None,
        "bindings": bindings,
        "engine": source.engine,
        "epoch": placement.write_epoch,
        "tables": tables,
        "indexes": [
            {"entity": str(index["entity"]), "name": str(index["name"]), "index": dict(index)}
            for index in plan.added
        ],
    }


def _recheck(operator: LocalCutover, execution: dict[str, Any]) -> None:
    """Everything a publication stands on: bindings, logins, tables and a generation to itself."""
    from .engines._operator import TableIdentity

    for name, binding in execution["bindings"].items():
        native = operator.native[name]
        if list(native.endpoint()) != binding["endpoint"]:
            raise MigrationRefused("an index build binding names another native database")
        expected_watermark = TableIdentity(**binding["watermark_identity"])
        if native.identity(WATERMARK_TABLE).physical_key != expected_watermark.physical_key:
            raise MigrationRefused("an index build's namespace or watermark identity changed")
        native.qualify([WATERMARK_TABLE], binding["allowed_tables"])
        if native.principal_ids != binding["principals"]:
            raise MigrationRefused("a runtime login identity changed during the index build")
    engine = operator.engines[execution["engine"]]
    native = operator.native[execution["engine"]]
    for record in execution["tables"].values():
        expected = TableIdentity(**record)
        if native.identity(expected.name).physical_key != expected.physical_key:
            raise MigrationRefused("a table was replaced during its index build")
        observed = engine.write_fence(expected.name, project_id=operator.project_id).state()
        if not observed.complete or observed.epoch != execution["epoch"] or observed.holds:
            raise MigrationRefused("another barrier or generation appeared during the index build")


def _finish(
    operator: LocalCutover, state: dict[str, Any], plan: IndexPlan, *, recovered: bool
) -> IndexReceipt:
    from .engines._index_build import NativeIndexBuild
    from .engines._operator import TableIdentity

    execution = state["execution"]
    builder = NativeIndexBuild(operator.native[execution["engine"]])
    if execution["decision"] == "abandoned":
        # Removing our own indexes publishes nothing, so it needs only the same database - each
        # drop checks its table's identity - and neither the logins nor a barrier-free table.
        # That is what keeps abandonment available when the build cannot finish.
        binding = execution["bindings"][execution["engine"]]
        if list(operator.native[execution["engine"]].endpoint()) != binding["endpoint"]:
            raise MigrationRefused("an index build binding names another native database")
    else:
        _recheck(operator, execution)
    rows = [
        (TableIdentity(**execution["tables"][row["entity"]]), row["index"])
        for row in execution["indexes"]
    ]
    if execution["decision"] is None:
        for table, index in rows:
            operator._step(
                state, "index_build_" + str(index["name"]), partial(builder.build, table, index)
            )

        def qualify() -> None:
            # The whole next layout, read back: a build that left the right index beside a table
            # that no longer matches would still be publishing a map the engine does not hold.
            source = plan.prepared.groups[plan.group].source
            keys = {entity: operator.model.entity(entity).key for entity in source.layout.tables}
            refuse_findings(
                operator.engines[execution["engine"]].validate_schema(source.layout, keys=keys),
                MigrationRefused,
            )
            for table, index in rows:
                if builder.status(table, index) != "ready":
                    raise MigrationRefused("an index is not ready for publication")
            _recheck(operator, execution)

        operator._step(state, "index_qualify", qualify)
        execution["decision"] = "built"
        operator.store.write(state)
        operator._after_step("index_decision")
    if execution["decision"] == "built":

        def watermarks() -> None:
            for engine in operator.engines.values():
                observed = engine.map_watermark()
                if observed is not None and observed > plan.prepared.map_version:
                    raise MigrationRefused("a newer map appeared while finishing the index build")
                engine.record_map_version(
                    plan.prepared.map_version, model_version=operator.model.version
                )

        operator._step(state, "index_watermarks", watermarks)

        def publish() -> None:
            current = operator.active_map()
            if current.fingerprint not in (plan.current.fingerprint, plan.prepared.fingerprint):
                raise MigrationRefused("another active map replaced the index build instruction")
            operator.store.publish(plan.prepared_payload())

        operator._step(state, "index_publish", publish)
        outcome, active = "built", plan.prepared
    else:
        for table, index in rows:
            operator._step(
                state, "index_drop_" + str(index["name"]), partial(builder.drop, table, index)
            )
        if operator.active_map().fingerprint != plan.current.fingerprint:
            raise MigrationRefused("an abandoned index build found another active map")
        outcome, active = "abandoned", plan.current
    receipt = {
        "protocol": 1,
        "index_id": plan.index_id,
        "index_fingerprint": plan.fingerprint,
        "project_id": operator.project_id,
        "group": plan.group,
        "outcome": outcome,
        "map_version": active.map_version,
        "map_fingerprint": active.fingerprint,
        "indexes": [
            {
                "engine": execution["engine"],
                "entity": row["entity"],
                "name": row["name"],
                "table": execution["tables"][row["entity"]],
            }
            for row in execution["indexes"]
        ],
        "elapsed_ms": operator._elapsed(),
        "recovered": recovered,
    }
    state["indexes"][plan.index_id] = {
        "plan_fingerprint": plan.fingerprint,
        "plan": plan.as_record(),
        "receipt": receipt,
    }
    if outcome == "built":
        state["active_map"] = json.loads(plan.prepared_payload())
    state["execution"] = None
    operator.store.write(state)
    operator._after_step("index_complete")
    return IndexReceipt(receipt)


def _completed(operator: LocalCutover, state: dict[str, Any], plan: IndexPlan) -> IndexReceipt:
    complete = state.get("indexes", {}).get(plan.index_id)
    assert complete is not None
    if complete["plan_fingerprint"] != plan.fingerprint:
        raise MigrationRefused("the completed index build id names another authorization")
    operator.store.confirm()
    return IndexReceipt(complete["receipt"])


def execute_index(operator: LocalCutover, plan: IndexPlan) -> IndexReceipt:
    with operator.store.lock():
        state = operator.store.read()
        if plan.index_id in state.get("indexes", {}):
            return _completed(operator, state, plan)
        if state["execution"] is not None:
            raise CutoverRecoveryRequired("an unfinished local operation requires resume")
        execution = _snapshot(operator, plan, state)
        state.setdefault("stages", {})
        state.setdefault("indexes", {})
        state["execution"] = execution
        operator.store.write(state)
        operator._after_step("index_prepared")
        operator._started, operator._enforce_budget = monotonic_ns(), False
        try:
            return _finish(operator, state, plan, recovered=False)
        except Exception as exc:
            raise CutoverRecoveryRequired(
                "the local index build needs recovery; preserve its project state"
            ) from exc


def _stored_plan(operator: LocalCutover, execution: dict[str, Any]) -> IndexPlan:
    plan = load_index_plan(
        execution["plan"],
        model=operator.model,
        project_id=operator.project_id,
        public_key=operator.keys,
    )
    if plan.fingerprint != execution["plan_fingerprint"]:
        raise MigrationRefused("index build recovery names another signed authorization")
    return plan


def resume_index(operator: LocalCutover, state: dict[str, Any]) -> IndexReceipt:
    plan = _stored_plan(operator, state["execution"])
    operator._alarm.arm(plan.build_budget_ms)
    operator._started, operator._enforce_budget = monotonic_ns(), False
    try:
        return _finish(operator, state, plan, recovered=True)
    except Exception as exc:
        raise CutoverRecoveryRequired(
            "index build recovery remains incomplete; preserve its state"
        ) from exc


def abandon_index(operator: LocalCutover) -> IndexReceipt:
    with operator.store.lock():
        state = operator.store.read()
        execution = state["execution"]
        if execution is None or execution.get("kind") != "index":
            raise MigrationRefused("there is no unfinished index build to abandon")
        if execution["decision"] == "built":
            raise MigrationRefused(
                "a built index cannot be abandoned: its next map is decided; resume to publish it"
            )
        plan = _stored_plan(operator, execution)
        operator._alarm.arm(plan.build_budget_ms)
        if execution["decision"] is None:
            execution["decision"] = "abandoned"
            operator.store.write(state)
            operator._after_step("index_abandoned")
        operator._started, operator._enforce_budget = monotonic_ns(), False
        try:
            return _finish(operator, state, plan, recovered=True)
        except Exception as exc:
            raise CutoverRecoveryRequired(
                "abandoning the index build remains incomplete; preserve its state"
            ) from exc

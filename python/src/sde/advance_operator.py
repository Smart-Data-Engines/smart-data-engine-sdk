"""Durable generation advances using only customer-local connections.

An advance pauses one group's writes for as long as it takes to raise one number: a barrier on the
source's tables and then on each copy's, the next generation behind it, the next map published,
the barrier lifted. No row is read, copied or compared, and no grant changes hands - the barrier
is what closes the tables, so there is nothing to give back after a crash.

Its decision is the cutover's in small. Until the generation is raised on a first table the map
in force is still true of every table, so an interruption, an exhausted pause budget and a
recovery all lift this advance's own barrier and leave that map in force (``abandoned``):
applications hold it and write again at once. A write fence cannot lower a generation, so from
the decision on a recovery only completes (``advanced``).
"""

from __future__ import annotations

import hashlib
import json
from functools import partial
from time import monotonic_ns
from typing import TYPE_CHECKING, Any

from .advance import ADVANCE_PROTOCOL, AdvancePlan, AdvanceReceipt, load_advance_plan
from .errors import MigrationRefused
from .generation import fenced_groups
from .local_cutover import CutoverRecoveryRequired, PauseBudgetExceeded
from .placement import WATERMARK_TABLE
from .watermark import newer_map

if TYPE_CHECKING:
    from .local_cutover import LocalCutover

FREEZE_ORDER = ("source", "copy")
"""A write is the source's first and a copy's additionally, so the source closes first: after it
nothing reaches a copy but what was already on its way. The barrier lifts in the other order, so
that no write is taken by the source while a copy still refuses it."""


def _snapshot(operator: LocalCutover, plan: AdvancePlan, state: dict[str, Any]) -> dict[str, Any]:
    plan.check_current(operator.active_map())
    # The next map is recorded in every engine a fenced group uses, so each needs a binding.
    fenced = fenced_groups(plan.before)
    if not {material.engine for placed in fenced.values() for material in placed.all()} <= set(
        operator.engines
    ):
        raise MigrationRefused("an advance is missing an operator binding")
    spot = plan.before.groups[plan.group]
    in_engine: dict[str, set[str]] = {name: set() for name in operator.engines}
    others: set[tuple[str, str, str, str]] = set()
    for group, placed in fenced.items():
        for material in placed.all():
            for table in material.layout.tables.values():
                in_engine[material.engine].add(table)
                if group != plan.group:
                    others.add(operator.native[material.engine].identity(table).physical_key)
    tables: list[dict[str, Any]] = []
    own: list[tuple[str, str, str, str]] = []
    for material in spot.all():
        for entity, table in sorted(material.layout.tables.items()):
            identity = operator.native[material.engine].identity(table)
            observed = (
                operator.engines[material.engine]
                .write_fence(table, project_id=operator.project_id)
                .state()
            )
            if not observed.complete or observed.epoch != plan.source_epoch or observed.holds:
                # Beside another operation's barrier the next map would be one that operation's
                # own maps do not know about.
                raise MigrationRefused(
                    "an advance needs the group's current generation without another barrier"
                )
            own.append(identity.physical_key)
            tables.append(
                {
                    "engine": material.engine,
                    "materialization": material.id,
                    "entity": entity,
                    "role": "source" if material is spot.source else "copy",
                    "identity": identity.as_record(),
                }
            )
    if len(set(own)) != len(own):
        raise MigrationRefused("two tables of the group resolve to the same physical table")
    if set(own) & others:
        # Its generation would be raised under a group whose map still names the earlier one.
        raise MigrationRefused("an advance would touch another group's physical table")
    bindings: dict[str, Any] = {}
    for name, native in operator.native.items():
        selected = sorted(
            {
                table
                for material in spot.all()
                if material.engine == name
                for table in material.layout.tables.values()
            }
        )
        allowed = sorted(in_engine[name] | {WATERMARK_TABLE})
        if selected:
            # As a cutover qualifies them, every other grantee included: the generation this
            # leaves behind is a mark a later comparison stands on.
            native.qualify(selected, allowed)
        watermark = operator.engines[name].map_watermark()
        if watermark is not None and watermark > plan.before.map_version:
            raise newer_map("a newer map was adopted before this advance", name,
                            operator.engines[name].dialect, watermark, plan.before.map_version)
        bindings[name] = {
            "endpoint": list(native.endpoint()),
            "principals": dict(native.principal_ids),
            "allowed_tables": allowed,
        }
    return {
        "kind": "advance",
        "plan_id": plan.advance_id,
        "plan_fingerprint": plan.fingerprint,
        "plan": plan.as_record(),
        "phase": "advance_prepared",
        "pending_hold": None,
        "decision": None,
        "reason": None,
        "bindings": bindings,
        "epoch": plan.source_epoch,
        "hold": hashlib.sha256((plan.advance_id + ":advance").encode()).hexdigest()[:32],
        "tables": tables,
    }


def _held(operator: LocalCutover, execution: dict[str, Any]) -> None:
    """Under the barrier and before the decision: every table of the group is the one that was
    qualified, closed by this advance's barrier and by no other, at the generation it starts from.
    """
    from .engines._operator import TableIdentity

    for name, binding in execution["bindings"].items():
        if list(operator.native[name].endpoint()) != binding["endpoint"]:
            raise MigrationRefused("an advance binding names another native database")
    for row in execution["tables"]:
        expected = TableIdentity(**row["identity"])
        native = operator.native[row["engine"]]
        if native.identity(expected.name).physical_key != expected.physical_key:
            raise MigrationRefused("a table was replaced during its advance")
        observed = operator._call(
            operator.engines[row["engine"]]
            .write_fence(expected.name, project_id=operator.project_id)
            .state
        )
        if (
            not observed.complete
            or observed.epoch != execution["epoch"]
            or tuple(observed.holds) != (execution["hold"],)
        ):
            raise MigrationRefused(
                "another barrier or generation appeared on a table during its advance"
            )


def _decide(operator: LocalCutover, state: dict[str, Any], outcome: str, reason: str) -> None:
    execution = state["execution"]
    if execution["decision"] is not None and execution["decision"] != outcome:
        raise MigrationRefused("a durable advance decision cannot be changed")
    if outcome == "advanced":
        operator._check_budget()
    execution["decision"], execution["reason"] = outcome, reason
    operator.store.write(state)
    operator._after_step("advance_decision:" + outcome)


def _finish(
    operator: LocalCutover, state: dict[str, Any], plan: AdvancePlan, *, recovered: bool
) -> AdvanceReceipt:
    execution = state["execution"]
    outcome = execution["decision"]
    if outcome not in ("advanced", "abandoned"):
        raise MigrationRefused("an advance has no durable decision")
    # A committed decision is completed, never converted by an elapsed budget into another one.
    operator._enforce_budget = False
    if outcome == "advanced":
        for role in FREEZE_ORDER:
            operator._step(
                state,
                "advance_" + role,
                partial(operator._advance, execution, role, plan.next_epoch),
            )

        def watermarks() -> None:
            for name, engine in operator.engines.items():
                observed = engine.map_watermark()
                if observed is not None and observed > plan.after.map_version:
                    raise newer_map("a newer map appeared while completing the advance", name,
                                    engine.dialect, observed, plan.after.map_version)
                engine.record_map_version(
                    plan.after.map_version, model_version=operator.model.version
                )

        operator._step(state, "advance_watermarks", watermarks)

        def publish() -> None:
            current = operator.active_map()
            if current.fingerprint not in (plan.before.fingerprint, plan.after.fingerprint):
                raise MigrationRefused("another active map replaced the advance instruction")
            operator.store.publish(plan.after_payload())

        operator._step(state, "advance_publish", publish)
        active, epoch = plan.after, plan.next_epoch
    else:
        if operator.active_map().fingerprint != plan.before.fingerprint:
            raise MigrationRefused("an abandoned advance found another active map")
        active, epoch = plan.before, plan.source_epoch
    for role in reversed(FREEZE_ORDER):
        operator._step(
            state,
            "advance_release_" + role,
            partial(operator._release, execution, role, [execution["hold"]]),
        )
    for row in execution["tables"]:
        observed = (
            operator.engines[row["engine"]]
            .write_fence(row["identity"]["name"], project_id=operator.project_id)
            .state()
        )
        if observed.epoch != epoch or observed.closed:
            raise CutoverRecoveryRequired("a table of the group still has a write barrier")
    elapsed = operator._elapsed()
    operator._alarm.cancel()  # The group is open again; only the receipt remains to be kept.
    receipt = {
        "protocol": ADVANCE_PROTOCOL,
        "advance_id": plan.advance_id,
        "advance_fingerprint": plan.fingerprint,
        "project_id": operator.project_id,
        "group": plan.group,
        "outcome": outcome,
        "reason": execution["reason"],
        "map_version": active.map_version,
        "map_fingerprint": active.fingerprint,
        "write_epoch": epoch,
        "tables": [
            {
                "engine": row["engine"],
                "materialization": row["materialization"],
                "entity": row["entity"],
                "role": row["role"],
                "table": row["identity"],
            }
            for row in execution["tables"]
        ],
        "elapsed_ms": elapsed,
        "recovered": recovered,
        "within_budget": not recovered and elapsed <= plan.pause_budget_ms,
    }
    state["advances"][plan.advance_id] = {
        "plan_fingerprint": plan.fingerprint,
        "plan": plan.as_record(),
        "receipt": receipt,
    }
    if outcome == "advanced":
        state["active_map"] = json.loads(plan.after_payload())
    state["execution"] = None
    operator.store.write(state)
    operator._after_step("advance_complete")
    return AdvanceReceipt(receipt)


def execute_advance(operator: LocalCutover, plan: AdvancePlan) -> AdvanceReceipt:
    with operator.store.lock():
        state = operator.store.read()
        complete = state.get("advances", {}).get(plan.advance_id)
        if complete is not None:
            if complete["plan_fingerprint"] != plan.fingerprint:
                raise MigrationRefused("the completed advance id names another authorization")
            operator.store.confirm()
            return AdvanceReceipt(complete["receipt"])
        if state["execution"] is not None:
            raise CutoverRecoveryRequired("an unfinished local operation requires resume")
        execution = _snapshot(operator, plan, state)
        # The history of each kind of operation is part of what a reader of this state must
        # know: an operator that predates advances refuses the state whole.
        state.setdefault("stages", {})
        state.setdefault("indexes", {})
        state.setdefault("advances", {})
        state["execution"] = execution
        operator.store.write(state)
        operator._after_step("advance_prepared")
        # From here the group's writes wait, and the signed budget is what bounds that.
        operator._started, operator._budget_ms, operator._enforce_budget = (
            monotonic_ns(),
            plan.pause_budget_ms,
            True,
        )
        operator._alarm.arm(plan.pause_budget_ms)
        hold = execution["hold"]
        try:
            for role in FREEZE_ORDER:
                operator._step(
                    state,
                    "advance_freeze_" + role,
                    partial(operator._freeze, execution, role, hold),
                    hold=hold,
                )
            operator._step(state, "advance_held", lambda: _held(operator, execution))
            _decide(operator, state, "advanced", "completed")
            return _finish(operator, state, plan, recovered=False)
        except PauseBudgetExceeded:
            operator._alarm.cancel()
            operator._enforce_budget = False
            if execution["decision"] is not None:
                raise CutoverRecoveryRequired(
                    "the pause budget elapsed after a durable decision; resume it"
                ) from None
            _decide(operator, state, "abandoned", "budget_exceeded")
            operator._alarm.arm(30000)
            return _finish(operator, state, plan, recovered=True)
        except Exception as exc:
            raise CutoverRecoveryRequired(
                "the generation advance did not complete; resume its durable local state"
            ) from exc


def resume_advance(operator: LocalCutover, state: dict[str, Any]) -> AdvanceReceipt:
    execution = state["execution"]
    plan = load_advance_plan(
        execution["plan"],
        model=operator.model,
        project_id=operator.project_id,
        public_key=operator.keys,
    )
    if plan.fingerprint != execution["plan_fingerprint"]:
        raise MigrationRefused("advance recovery names another signed authorization")
    operator._started, operator._enforce_budget = monotonic_ns(), False
    try:
        operator._restore_connections(execution)
        if execution["decision"] is None:
            _decide(operator, state, "abandoned", "recovered_before_decision")
        else:
            operator.store.write(state)  # Reconfirm durability before completing a decision.
        return _finish(operator, state, plan, recovered=True)
    except Exception as exc:
        raise CutoverRecoveryRequired(
            "advance recovery remains incomplete; keep the project state"
        ) from exc

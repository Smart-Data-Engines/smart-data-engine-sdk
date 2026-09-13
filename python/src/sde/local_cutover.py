"""A client-local durable cutover executor. It obeys signed decisions and keeps data local."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from time import monotonic_ns
from typing import TYPE_CHECKING, Any

from .canonical import canonical_bytes
from .cutover import CutoverPlan, load_cutover_plan
from .errors import EngineError, MigrationRefused
from .frozen_verification import verify_frozen
from .generation import EPOCH_COLUMN, check_map_project, json_numbers
from .groups import colocation_groups
from .inspection import InspectionContext
from .layout import group_columns
from .migration import CHUNK_ROWS, _plan, precision_refusal
from .model import LogicalModel
from .placement import WATERMARK_TABLE, PlacementMap, load_map

if TYPE_CHECKING:
    from ._cutover_project import ProjectState
    from .engines._operator import NativeOperator, TableIdentity


class CutoverRecoveryRequired(EngineError):
    """The durable local state must be resumed; no outcome may be inferred from a lost response."""


class PauseBudgetExceeded(MigrationRefused):
    """The local attempt exceeded its signed work budget before a success decision."""


@dataclass(frozen=True, init=False)
class CutoverReceipt:
    _payload: bytes

    def __init__(self, record: Mapping[str, Any]) -> None:
        object.__setattr__(
            self,
            "_payload",
            json.dumps(
                record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode(),
        )

    def as_record(self) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(self._payload)
        return result


def load_local_map(
    directory: str | Path,
    *,
    model: LogicalModel,
    project_id: str,
    public_key: bytes | Mapping[str, bytes],
) -> PlacementMap:
    """Read the atomically published active map, independently of the controller and operator."""
    try:
        raw = json.loads((Path(directory) / "active-map.json").read_bytes())
    except (OSError, ValueError) as exc:
        raise MigrationRefused("the local active map is missing or unreadable") from exc
    current = load_map(raw, model=model, public_key=public_key, require_signature=True)
    check_map_project(current, project_id)
    return current


def _local_status(
    directory: str | Path,
    *,
    model: LogicalModel,
    project_id: str,
    public_key: bytes | Mapping[str, bytes],
) -> dict[str, Any]:
    from ._cutover_project import ProjectState

    store = ProjectState(Path(directory), project_id, model.version)
    with store.lock():
        state = store.read()
        current = load_local_map(
            directory, model=model, project_id=project_id, public_key=public_key
        )
        execution = state["execution"]
        return {
            "project_id": project_id,
            "active_map_version": current.map_version,
            "recorded_map_version": state["active_map"]["map_version"],
            "plan_id": None if execution is None else execution["plan_id"],
            "phase": None if execution is None else execution["phase"],
            "decision": None if execution is None else execution["decision"],
        }


class LocalCutover:
    """One local POSIX project directory and dedicated operator/runtime-probe connections.

    Applications keep their own engine connections. Only this executor writes its project state;
    applications consume active-map.json through load_local_map and refresh after a named refusal.
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        model: LogicalModel,
        project_id: str,
        public_key: bytes | Mapping[str, bytes],
        operators: Mapping[str, Any],
        runtime: Mapping[str, Sequence[Any]],
        chunk_rows: int = CHUNK_ROWS,
    ) -> None:
        if os.name != "posix":
            raise MigrationRefused("local cutover requires a local POSIX filesystem")
        from ._cutover_project import ProjectState
        from .engines._operator import NativeOperator

        if chunk_rows < 1 or set(runtime) != set(operators):
            raise MigrationRefused(
                "cutover needs positive chunks and matching operator/runtime bindings"
            )
        self.model, self.project_id = model, project_id
        self.keys = (
            bytes(public_key)
            if isinstance(public_key, (bytes, bytearray))
            else {name: bytes(value) for name, value in public_key.items()}
        )
        self.engines = dict(operators)
        self.native: dict[str, NativeOperator] = {
            name: NativeOperator(engine, runtime[name]) for name, engine in self.engines.items()
        }
        self.store: ProjectState = ProjectState(Path(directory), project_id, model.version)
        self.chunk_rows = chunk_rows
        self._pid = os.getpid()
        self._after_step: Callable[[str], None] = lambda _step: None
        self._started = 0
        self._budget_ms = 0
        self._enforce_budget = False
        self._alarm: Any = None

    def _owner(self) -> None:
        if os.getpid() != self._pid:
            raise MigrationRefused("create fresh operator connections after fork")

    def enroll(self, current: Mapping[str, Any]) -> None:
        self._owner()
        document = json_numbers(deepcopy(dict(current)))
        parsed = load_map(document, model=self.model, public_key=self.keys, require_signature=True)
        if parsed.contract != 4:
            raise MigrationRefused("local cutover enrollment requires map contract 4")
        check_map_project(parsed, self.project_id)
        self.store.enroll(document, canonical_bytes(document))

    def active_map(self) -> PlacementMap:
        return load_local_map(
            self.store.root, model=self.model, project_id=self.project_id, public_key=self.keys
        )

    def status(self) -> dict[str, Any]:
        return _local_status(
            self.store.root, model=self.model, project_id=self.project_id, public_key=self.keys
        )

    def _elapsed(self) -> int:
        return (monotonic_ns() - self._started) // 1_000_000

    def _check_budget(self) -> None:
        if self._enforce_budget and self._elapsed() >= self._budget_ms:
            raise PauseBudgetExceeded("the signed cutover pause budget was exceeded")

    def _call(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        self._check_budget()
        value = function(*args, **kwargs)
        self._check_budget()
        return value

    def _step(
        self,
        state: dict[str, Any],
        phase: str,
        action: Callable[[], Any],
        *,
        hold: str | None = None,
    ) -> Any:
        execution = state["execution"]
        execution["phase"], execution["pending_hold"] = phase, hold
        self.store.write(state)
        self._after_step(phase + ":intent")
        value = action()
        execution["pending_hold"] = None
        self.store.write(state)
        self._after_step(phase + ":done")
        return value

    def _snapshot(self, plan: CutoverPlan, state: dict[str, Any]) -> dict[str, Any]:
        import hashlib

        plan.check_current(self.active_map())
        needed = {m.engine for g in plan.before.groups.values() for m in g.all()}
        if not needed <= set(self.engines):
            raise MigrationRefused("cutover is missing an operator binding")
        spot = plan.before.groups[plan.group]
        members = next(group for group in colocation_groups(self.model) if group.name == plan.group)
        for material in spot.all():
            if set(material.layout.tables) != set(members.members) or set(
                material.layout.columns
            ) != set(members.members):
                raise MigrationRefused(
                    "cutover layouts must cover exactly the migrating group's entities"
                )
        columns = group_columns(self.model, members)
        for entity in columns:
            reason = precision_refusal(
                group=plan.group,
                entity=entity,
                columns=columns[entity],
                source_dialect=self.engines[spot.source.engine].dialect,
                target_dialect=self.engines[spot.derived[0].engine].dialect,
            )
            if reason:
                raise MigrationRefused(reason)
        context = InspectionContext(self.model, plan.before, self.engines, self.project_id)
        _plan(context, plan.group)  # Shape/key/copy capability refusals before any barrier.
        all_tables: dict[str, list[str]] = {name: [] for name in self.engines}
        identities: list[tuple[str, str, TableIdentity]] = []
        for group, placement in plan.before.groups.items():
            for material in placement.all():
                self.engines[material.engine].validate_schema(material.layout)
                for table in material.layout.tables.values():
                    all_tables[material.engine].append(table)
                    identity = self.native[material.engine].identity(table)
                    identities.append((group, material.id, identity))
        moving = [value for group, _, value in identities if group == plan.group]
        if len({value.physical_key for value in moving}) != len(moving):
            raise MigrationRefused("source and target aliases resolve to the same physical table")
        if {value.physical_key for value in moving} & {
            value.physical_key for group, _, value in identities if group != plan.group
        }:
            raise MigrationRefused("cutover would touch another group's physical table")
        names = [
            list((value.dialect, value.server, value.database, value.namespace, value.name))
            for value in moving
        ]
        if any(name in state["retired_names"] for name in names):
            raise MigrationRefused("a retired physical name cannot be reused for a materialization")
        bindings: dict[str, Any] = {}
        for name, native in self.native.items():
            selected = sorted(
                {
                    table
                    for material in spot.all()
                    if material.engine == name
                    for table in material.layout.tables.values()
                }
            )
            if selected:
                native.qualify(selected, sorted(set(all_tables[name]) | {WATERMARK_TABLE}))
            bindings[name] = {
                "endpoint": list(native.endpoint()),
                "principals": dict(native.principal_ids),
                "allowed_tables": sorted(set(all_tables[name]) | {WATERMARK_TABLE}),
            }
            watermark = self.engines[name].map_watermark()
            if watermark is not None and watermark > plan.before.map_version:
                raise MigrationRefused("a newer map was adopted before this local cutover")
        tables = []
        for material in spot.all():
            for entity, table in sorted(material.layout.tables.items()):
                identity = self.native[material.engine].identity(table)
                fence = self.engines[material.engine].write_fence(table, project_id=self.project_id)
                observed = fence.state()
                if not observed.complete or observed.epoch != plan.source_epoch or observed.holds:
                    raise MigrationRefused(
                        "cutover needs an active before-generation without foreign holds"
                    )
                tables.append(
                    {
                        "engine": material.engine,
                        "materialization": material.id,
                        "entity": entity,
                        "role": "source" if material is spot.source else "target",
                        "identity": identity.as_record(),
                    }
                )
        holds = {
            name: hashlib.sha256((plan.plan_id + ":" + name).encode()).hexdigest()[:32]
            for name in ("source", "maintenance", "final", "abort")
        }
        return {
            "plan_id": plan.plan_id,
            "plan_fingerprint": plan.fingerprint,
            "plan": plan.as_record(),
            "phase": "prepared",
            "pending_hold": None,
            "decision": None,
            "reason": None,
            "bindings": bindings,
            "tables": tables,
            "holds": holds,
            "verification": None,
        }

    def _tables(self, execution: dict[str, Any], role: str) -> list[dict[str, Any]]:
        return [row for row in execution["tables"] if row["role"] == role]

    def _fences(self, execution: dict[str, Any], role: str) -> list[Any]:
        return [
            self.engines[row["engine"]].write_fence(
                row["identity"]["name"], project_id=self.project_id
            )
            for row in self._tables(execution, role)
        ]

    def _access(self, execution: dict[str, Any], role: str, enabled: bool) -> None:
        from .engines._operator import TableIdentity

        for name, native in self.native.items():
            tables = [
                TableIdentity(**row["identity"])
                for row in self._tables(execution, role)
                if row["engine"] == name
            ]
            if tables:
                self._call(native.access, tables, enabled=enabled)

    def _freeze(self, execution: dict[str, Any], role: str, hold: str) -> None:
        for fence in self._fences(execution, role):
            self._call(fence.freeze, hold)

    def _advance(self, execution: dict[str, Any], role: str, epoch: int) -> None:
        for fence in self._fences(execution, role):
            observed = self._call(fence.state)
            if observed.epoch != epoch:
                self._call(fence.advance, epoch)

    def _release(self, execution: dict[str, Any], role: str, holds: Sequence[str]) -> None:
        for fence in self._fences(execution, role):
            for hold in holds:
                self._call(fence.release, hold)

    def _repair(self, plan: CutoverPlan, execution: dict[str, Any]) -> None:
        from .engines._operator import TableIdentity

        for name, native in self.native.items():
            tables = [
                TableIdentity(**row["identity"])
                for row in self._tables(execution, "target")
                if row["engine"] == name
            ]
            if tables:
                self._call(native.truncate, tables)
        context = InspectionContext(self.model, plan.before, self.engines, self.project_id)
        for copy in _plan(context, plan.group):
            after = None
            while True:
                rows = self._call(
                    copy.source.key_range,
                    copy.source_table,
                    copy.key,
                    after=after,
                    limit=self.chunk_rows,
                )
                if not rows:
                    break
                payload = [
                    {
                        **{key: value for key, value in row.items() if key != EPOCH_COLUMN},
                        EPOCH_COLUMN: plan.maintenance_epoch,
                    }
                    for row in rows
                ]
                self._call(copy.target.copy_in, copy.target_table, payload)
                after = tuple(rows[-1][key] for key in copy.key)

    def _decide(self, state: dict[str, Any], outcome: str, reason: str) -> None:
        execution = state["execution"]
        if execution["decision"] is not None and execution["decision"] != outcome:
            raise MigrationRefused("a durable cutover decision cannot be changed")
        if outcome == "success":
            self._check_budget()
            for row in self._tables(execution, "source"):
                identity = row["identity"]
                name = [
                    identity[key] for key in ("dialect", "server", "database", "namespace", "name")
                ]
                if name not in state["retired_names"]:
                    state["retired_names"].append(name)
        execution["decision"], execution["reason"] = outcome, reason
        self.store.write(state)
        self._after_step("decision:" + outcome)

    def _finish(
        self, state: dict[str, Any], plan: CutoverPlan, *, recovered: bool
    ) -> CutoverReceipt:
        execution = state["execution"]
        outcome = execution["decision"]
        if outcome not in ("success", "abort"):
            raise MigrationRefused("cutover has no durable terminal decision")
        # A committed decision is completed, never converted by an elapsed budget into another one.
        self._enforce_budget = False
        if outcome == "success":
            self._step(
                state, "confirm_source_denied", lambda: self._access(execution, "source", False)
            )
            self._step(
                state,
                "retire_source",
                lambda: self._advance(execution, "source", plan.activation_epoch),
            )
            self._step(
                state,
                "activate_target",
                lambda: self._advance(execution, "target", plan.activation_epoch),
            )
            candidate, active_role = plan.success, "target"
        else:
            self._step(
                state,
                "abort_source_barrier",
                lambda: self._ensure_source_hold(execution),
                hold=execution["holds"]["source"],
            )
            self._step(
                state, "confirm_target_denied", lambda: self._access(execution, "target", False)
            )
            self._step(
                state,
                "abort_target_barrier",
                lambda: self._freeze(execution, "target", execution["holds"]["abort"]),
                hold=execution["holds"]["abort"],
            )
            self._step(
                state,
                "abort_target_epoch",
                lambda: self._advance(execution, "target", plan.maintenance_epoch),
            )
            self._step(
                state,
                "activate_abort_source",
                lambda: self._advance(execution, "source", plan.maintenance_epoch),
            )
            candidate, active_role = plan.abort, "source"

        def watermark() -> None:
            for engine in self.engines.values():
                value = engine.map_watermark()
                if value is not None and value > candidate.map_version:
                    raise MigrationRefused("a newer map appeared while completing cutover")
                engine.record_map_version(candidate.map_version, model_version=self.model.version)

        self._step(state, "watermarks", watermark)

        def publish() -> None:
            current = self.active_map()
            if current.fingerprint not in (plan.before.fingerprint, candidate.fingerprint):
                raise MigrationRefused("another active map replaced the local cutover instruction")
            self.store.publish(plan.candidate_payload(outcome))

        self._step(state, "publish_map", publish)
        self._step(state, "enable_runtime", lambda: self._access(execution, active_role, True))
        holds = (
            [execution["holds"]["final"]]
            if outcome == "success"
            else [execution["holds"]["final"], execution["holds"]["source"]]
        )
        self._step(state, "release_runtime", lambda: self._release(execution, active_role, holds))
        for fence in self._fences(execution, active_role):
            observed = fence.state()
            expected_epoch = (
                plan.activation_epoch if outcome == "success" else plan.maintenance_epoch
            )
            if observed.epoch != expected_epoch or observed.closed:
                raise CutoverRecoveryRequired("the active generation still has a write barrier")
        elapsed = self._elapsed()
        self._alarm.cancel()  # The active generation is open; only receipt persistence remains.
        receipt = {
            "protocol": 1,
            "plan_id": plan.plan_id,
            "plan_fingerprint": plan.fingerprint,
            "project_id": self.project_id,
            "group": plan.group,
            "outcome": outcome,
            "map_version": candidate.map_version,
            "map_fingerprint": candidate.fingerprint,
            "verification": execution["verification"],
            "reason": execution["reason"],
            "elapsed_ms": elapsed,
            "recovered": recovered,
            "within_budget": not recovered and elapsed <= plan.pause_budget_ms,
        }
        state["completed"][plan.plan_id] = {
            "plan_fingerprint": plan.fingerprint,
            "receipt": receipt,
        }
        state["active_map"] = json.loads(plan.candidate_payload(outcome))
        state["execution"] = None
        self.store.write(state)
        self._after_step("completed")
        return CutoverReceipt(receipt)

    def _ensure_source_hold(self, execution: dict[str, Any]) -> None:
        hold = execution["holds"]["source"]
        for fence in self._fences(execution, "source"):
            observed = fence.state()
            if hold not in observed.retired:
                fence.freeze(hold)

    def _interrupted(self) -> None:
        # The server's final statement may still have an uncertain outcome. Close these dedicated
        # connections and stop the protocol; recovery uses fresh connections and native evidence.
        for native in self.native.values():
            for engine in (native.engine, *native.runtime):
                with suppress(Exception):
                    engine.close()

    def execute(self, plan: CutoverPlan) -> CutoverReceipt:
        from ._operator_deadline import DeadlineInterrupt, OperatorDeadline

        self._owner()
        try:
            with OperatorDeadline() as deadline:
                self._alarm = deadline
                deadline.arm(30000)  # Qualification does not pause application writes.
                return self._execute(plan)
        except DeadlineInterrupt as exc:
            self._interrupted()
            raise CutoverRecoveryRequired(
                "operator deadline interrupted an operation; reconnect dedicated adapters and "
                "inspect local status before resuming or retrying qualification"
            ) from exc
        finally:
            self._alarm = None

    def _execute(self, plan: CutoverPlan) -> CutoverReceipt:
        self._owner()
        with self.store.lock():
            state = self.store.read()
            if plan.plan_id in state["completed"]:
                complete = state["completed"][plan.plan_id]
                if complete["plan_fingerprint"] != plan.fingerprint:
                    raise MigrationRefused("the completed plan id names another packet")
                return CutoverReceipt(complete["receipt"])
            if state["execution"] is not None:
                raise CutoverRecoveryRequired("an unfinished local cutover requires resume")
            execution = self._snapshot(plan, state)
            state["execution"] = execution
            self.store.write(state)
            self._after_step("prepared")
            self._started, self._budget_ms, self._enforce_budget = (
                monotonic_ns(),
                plan.pause_budget_ms,
                True,
            )
            self._alarm.arm(plan.pause_budget_ms)
            try:
                self._step(state, "deny_target", lambda: self._access(execution, "target", False))
                self._step(
                    state,
                    "freeze_target",
                    lambda: self._freeze(execution, "target", execution["holds"]["maintenance"]),
                    hold=execution["holds"]["maintenance"],
                )
                self._step(
                    state,
                    "freeze_source",
                    lambda: self._freeze(execution, "source", execution["holds"]["source"]),
                    hold=execution["holds"]["source"],
                )
                self._step(state, "deny_source", lambda: self._access(execution, "source", False))
                self._step(
                    state,
                    "maintenance_epoch",
                    lambda: self._advance(execution, "target", plan.maintenance_epoch),
                )
                self._step(
                    state,
                    "release_maintenance",
                    lambda: self._release(execution, "target", [execution["holds"]["maintenance"]]),
                )
                self._step(state, "repair", lambda: self._repair(plan, execution))
                context = InspectionContext(self.model, plan.before, self.engines, self.project_id)
                spot = plan.before.groups[plan.group]
                report = self._step(
                    state,
                    "verify_frozen",
                    lambda: self._call(
                        verify_frozen,
                        context,
                        plan.group,
                        request=plan.verification,
                        hold_id=execution["holds"]["final"],
                        epochs={
                            spot.source.id: plan.source_epoch,
                            spot.derived[0].id: plan.maintenance_epoch,
                        },
                        chunk_rows=self.chunk_rows,
                    ),
                    hold=execution["holds"]["final"],
                )
                execution["verification"] = report.as_record()
                self._decide(
                    state,
                    "success" if report.matched else "abort",
                    "matched" if report.matched else "verification_mismatch",
                )
                return self._finish(state, plan, recovered=False)
            except PauseBudgetExceeded:
                self._alarm.cancel()
                self._enforce_budget = False
                if execution["decision"] is not None:
                    raise CutoverRecoveryRequired(
                        "budget elapsed after a durable decision; resume it"
                    ) from None
                self._decide(state, "abort", "budget_exceeded")
                self._alarm.arm(30000)
                return self._finish(state, plan, recovered=True)
            except Exception as exc:
                raise CutoverRecoveryRequired(
                    "cutover did not complete; resume its durable local state"
                ) from exc

    def resume(self) -> CutoverReceipt:
        from ._operator_deadline import DeadlineInterrupt, OperatorDeadline

        self._owner()
        try:
            with OperatorDeadline() as deadline:
                self._alarm = deadline
                deadline.arm(30000)
                return self._resume()
        except DeadlineInterrupt as exc:
            self._interrupted()
            raise CutoverRecoveryRequired(
                "recovery deadline interrupted an operation; reconnect and resume"
            ) from exc
        finally:
            self._alarm = None

    def _resume(self) -> CutoverReceipt:
        self._owner()
        with self.store.lock():
            state = self.store.read()
            execution = state["execution"]
            if execution is None:
                raise MigrationRefused("there is no unfinished local cutover")
            plan = load_cutover_plan(
                execution["plan"],
                model=self.model,
                project_id=self.project_id,
                public_key=self.keys,
            )
            if plan.fingerprint != execution["plan_fingerprint"]:
                raise MigrationRefused("the stored execution names another signed packet")
            self._started, self._enforce_budget = monotonic_ns(), False
            try:
                self._restore_connections(execution)
                if execution["decision"] is None:
                    self._decide(state, "abort", "recovered_before_decision")
                else:
                    self.store.write(state)  # Reconfirm durability before completing a decision.
                return self._finish(state, plan, recovered=True)
            except Exception as exc:
                raise CutoverRecoveryRequired(
                    "cutover recovery remains incomplete; keep the project state"
                ) from exc

    def _restore_connections(self, execution: dict[str, Any]) -> None:
        from .engines._operator import TableIdentity

        for name, binding in execution["bindings"].items():
            native = self.native[name]
            if list(native.endpoint()) != binding["endpoint"]:
                raise MigrationRefused("the recovery binding names another native database")
            selected = [
                row["identity"]["name"] for row in execution["tables"] if row["engine"] == name
            ]
            if selected:
                native.qualify(selected, binding["allowed_tables"], required_access=False)
                if native.principal_ids != binding["principals"]:
                    raise MigrationRefused("runtime principal identity changed during cutover")
        for row in execution["tables"]:
            native = self.native[row["engine"]]
            expected = TableIdentity(**row["identity"])
            if native.dialect == "clickhouse":
                detached = native.rows(
                    "SELECT toString(uuid) FROM system.detached_tables "
                    "WHERE database=currentDatabase() AND table={table:String} "
                    "AND is_permanently=1",
                    {"table": expected.name},
                )
                if detached:
                    hold = execution["pending_hold"]
                    if detached != [(expected.object,)] or hold is None:
                        raise MigrationRefused(
                            "detached table has no matching local recovery intent"
                        )
                    self.engines[row["engine"]].write_fence(
                        expected.name, project_id=self.project_id
                    ).resume(hold)
            if native.identity(expected.name).physical_key != expected.physical_key:
                raise MigrationRefused("a physical table was replaced during cutover")

"""A client-local durable cutover executor. It obeys signed decisions and keeps data local."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from time import monotonic_ns
from typing import TYPE_CHECKING, Any, Final, cast

from .advance import AdvancePlan, AdvanceReceipt
from .canonical import canonical_bytes
from .cutover import CutoverPlan, load_cutover_plan
from .errors import EngineError, MigrationRefused
from .frozen_verification import verify_frozen
from .generation import (
    EPOCH_COLUMN,
    GENERATIONS_SINCE,
    check_map_project,
    fenced_groups,
    json_numbers,
)
from .groups import colocation_groups
from .index_build import IndexPlan, IndexReceipt
from .inspection import InspectionContext
from .layout import group_columns
from .migration import CHUNK_ROWS, BackfillProgress, _plan, precision_refusal
from .model import LogicalModel
from .physical import refuse_findings
from .placement import WATERMARK_TABLE, PlacementMap, load_map
from .reconcile import (
    Reconciliation,
    compare_stamped,
    copy_is_mostly_there,
    reconcile,
    reconcile_since,
    repairs_what_differs,
    settles_before_a_pause,
)
from .staging import StagingPlan, StagingReceipt
from .watermark import newer_map

if TYPE_CHECKING:
    from ._cutover_project import ProjectState
    from .engines._operator import NativeOperations, NativeOperator, TableIdentity
    from .erasure import ErasurePlan, ErasureReceipt, ErasureRequest


REPAIRS = ("diff", "refill")
"""How a cutover makes its copy the frozen source's equal inside the pause.

``diff`` compares the two and writes over the rows that differ; ``refill`` empties the copy and
copies every row, which is what every cutover did before and what ``diff`` falls back to. Both end
in a comparison under the final barriers, and neither decides success by anything else.
"""


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



Reach = dict[tuple[str, str, str, str], list[tuple[str, str, str, str, str]]]
"""Each physical table a map's names reach - :attr:`TableIdentity.physical_key` - and the names
that reach it: group, materialisation id, entity, table, engine binding."""


def map_reach(current: PlacementMap, natives: Mapping[str, Any]) -> Reach:
    """Every table ``current`` names, by the physical table each name reaches through its binding.

    One look-up per binding (:meth:`object_keys_many`), done once for a command and asked of every
    copy it writes to: per copy and per name, it was a quadratic count of round trips. A name
    whose engine has no binding here is not looked up; every path that writes to a copy needs the
    binding of each engine of its group anyway.
    """
    named: dict[str, list[tuple[str, str, str, str]]] = {}
    for group, spot in sorted(current.groups.items()):
        for material in spot.all():
            if material.engine not in natives:
                continue
            for entity, table in sorted(material.layout.tables.items()):
                named.setdefault(material.engine, []).append((group, material.id, entity, table))
    reach: Reach = {}
    for engine, names in sorted(named.items()):
        keys = natives[engine].object_keys_many([table for *_, table in names])
        for group, materialisation, entity, table in names:
            for key in sorted(keys[table]):
                reach.setdefault(key, []).append((group, materialisation, entity, table, engine))
    return reach


def refuse_another_name(
    reach: Reach, group: str, material: str, table: str, identity: TableIdentity
) -> None:
    """Refuse a copy's table that another name of the map reaches (format contract §7r).

    ``load_map`` refuses a copy's table named twice in one engine. Two engine names bound to one
    database - or an alias in Elasticsearch - reach one table by two names, which only the
    bindings can tell: ``reach`` (:func:`map_reach`) holds what every name of the map reaches, and
    a copy that another name reaches is refused. A table that is not there reaches nothing.
    """
    for name, other, entity, named, engine in reach.get(identity.physical_key, ()):
        if (name, other, named) != (group, material, table):
            raise MigrationRefused(
                f"the copy's table {table!r} of {group} is also {name}'s table {named!r} for "
                f"{entity} in {engine}: a copy's table holds that copy alone, and two names of "
                f"the map reach this one"
            )


def refuse_shared_source(
    reach: Reach, group: str, material: str, entity: str, table: str
) -> None:
    """Refuse a source's table that another name of the map reaches (format contract §7r).

    An erasure deletes from a source's table: one group's source named as another group's table -
    two engine names bound to one database - or as another entity's of its own group would have it
    delete the other's rows. A table that is not there reaches nothing.
    """
    this = (group, material, entity, table)
    for key in sorted(reach):
        names = reach[key]
        if all((name, other, ent, named) != this for name, other, ent, named, _ in names):
            continue
        for name, other, ent, named, engine in names:
            if (name, other, ent, named) != this:
                raise MigrationRefused(
                    f"the source's table {table!r} of {group} for {entity} is also {name}'s "
                    f"table {named!r} for {ent} in {engine}: a source's table holds one entity "
                    f"of its group alone, and two names of the map reach this one"
                )



_NOT_WRITTEN: Final[dict[str | None, str]] = {
    "privilege": (
        "the operator's login may not write the table's mark: in PostgreSQL it must own the "
        "table, in ClickHouse hold ALTER on it, in Elasticsearch update the index's mapping"
    ),
    "kind": "the name is not an ordinary table, and a mark is written on a table alone",
    "block": (
        "a block on the index refuses the update of its mapping: lift it, and run the command again"
    ),
    # The driver's text can carry what a connection is, and is left out as the CLI leaves it out of
    # every result.
    None: (
        "the engine did not answer or did not take the mark (its error is left out): run the "
        "command again"
    ),
}
"""Why ``mark-sources`` left a source whose mark the engine did not take, by
:attr:`~sde.engines._operator.MarkNotWritten.cause`."""

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
        repair: str = "diff",
    ) -> None:
        if os.name != "posix":
            raise MigrationRefused("local cutover requires a local POSIX filesystem")
        from ._cutover_project import ProjectState
        from .engines._operator import native_operator

        if chunk_rows < 1 or set(runtime) != set(operators):
            raise MigrationRefused(
                "cutover needs positive chunks and matching operator/runtime bindings"
            )
        if repair not in REPAIRS:
            raise MigrationRefused(
                f"a cutover repairs its copy by one of {list(REPAIRS)}, not {repair!r}"
            )
        self.model, self.project_id = model, project_id
        self.keys = (
            bytes(public_key)
            if isinstance(public_key, (bytes, bytearray))
            else {name: bytes(value) for name, value in public_key.items()}
        )
        self.engines = dict(operators)
        self.native: dict[str, NativeOperations] = {
            name: native_operator(engine, runtime[name]) for name, engine in self.engines.items()
        }
        self.store: ProjectState = ProjectState(Path(directory), project_id, model.version)
        self.chunk_rows = chunk_rows
        self.repair = repair
        self._pid = os.getpid()
        self._after_step: Callable[[str], None] = lambda _step: None
        self._started = 0
        self._budget_ms = 0
        self._enforce_budget = False
        self._alarm: Any = None

    def _owner(self) -> None:
        if os.getpid() != self._pid:
            raise MigrationRefused("create fresh operator connections after fork")

    def _copy_owned(
        self,
        current: PlacementMap,
        material: str,
        group: str,
        table: str,
        state: Mapping[str, Any],
        reach: Reach,
    ) -> TableIdentity:
        """``table``'s identity once its mark says it is a copy of ``group`` (§7r), and the map
        names that table for nothing else.

        ``load_map`` refuses a copy's table named twice in one engine; only the bindings know that
        two engine names reach one database, so ``reach`` (:func:`map_reach`, once a command)
        holds what every name of the map reaches, and a copy another name reaches is refused.
        """
        from .engines._ownership import copy_owned

        spot = current.groups[group]
        engine = spot.by_id(material).engine
        identity = copy_owned(
            self.native[engine],
            project_id=self.project_id,
            group=group,
            table=table,
            stages=state.get("stages", {}),
        )
        refuse_another_name(reach, group, material, table, identity)
        return identity

    def enroll(self, current: Mapping[str, Any]) -> None:
        self._owner()
        document = json_numbers(deepcopy(dict(current)))
        parsed = load_map(document, model=self.model, public_key=self.keys, require_signature=True)
        # Generations arrived in contract 4; contract 5 adds a physical design and keeps them,
        # and an AI-designed initial map is contract 5 from its first version.
        if parsed.contract < GENERATIONS_SINCE:
            raise MigrationRefused(
                f"local cutover enrollment requires map contract {GENERATIONS_SINCE} or later"
            )
        check_map_project(parsed, self.project_id)
        self.store.enroll(document, canonical_bytes(document))

    def active_map(self) -> PlacementMap:
        return load_local_map(
            self.store.root, model=self.model, project_id=self.project_id, public_key=self.keys
        )

    def backfill(self, *, stop_after: int | None = None) -> list[BackfillProgress]:
        """Fill every copy the map in force names from its source, while writes continue.

        A staging creates a copy and publishes the map that fans new writes out to it, and copies
        nothing. Without this, the cutover that follows copies the whole group inside its pause.
        With it the pause compares the two and repairs what differs: the rows a session on the
        earlier map wrote meanwhile, and on ClickHouse a row whose older version this copy wrote
        after a newer one arrived.

        It is the library's own :func:`sde.backfill` on the operator's logins: chunk first and
        marker after, so a run that stops is continued by the next one. Under the project lock,
        beside no staging, cutover or index build; ``stop_after`` bounds a run to that many
        chunks an entity.
        """
        from .migration import backfill_view

        self._owner()
        with self.store.lock():
            state = self.store.read()
            if state["execution"] is not None:
                raise MigrationRefused(
                    "an unfinished operation of this project holds its tables; resume or abandon "
                    "it before a backfill"
                )
            current = self.active_map()
            groups = sorted(name for name, spot in current.groups.items() if spot.also_write)
            if not groups:
                raise MigrationRefused(
                    "the map in force names no copy to fill: a backfill follows a staging and "
                    "precedes its cutover"
                )
            reach = map_reach(current, self.native)
            for group in groups:
                # What a backfill copies is what its login is shown of the source: a table with a
                # row policy is refused here as a cutover refuses it, and not a copy later.
                spot = current.groups[group]
                for material in (spot.source, *spot.derived):
                    native = self.native.get(material.engine)
                    if native is None:
                        raise MigrationRefused(
                            f"{group} has a copy in {material.engine}, which this operator is "
                            f"not bound to: a backfill needs the binding of every engine it "
                            f"reads or fills"
                        )
                    for table in sorted(material.layout.tables.values()):
                        if material is spot.source:
                            native.identity(table)
                        else:
                            # A backfill writes the copy's rows: its table is one this project
                            # created and marked for this group, or nothing is written (§7r).
                            self._copy_owned(current, material.id, group, table, state, reach)
            context = InspectionContext(self.model, current, self.engines, self.project_id)
            return [
                backfill_view(context, group, chunk_rows=self.chunk_rows, stop_after=stop_after)
                for group in groups
            ]

    def settle(self) -> list[dict[str, Any]]:
        """Compare the rows written before each group's last advance with its copy, while the
        application keeps writing, and keep what was settled for the cutover that follows.

        A cutover compares the whole group under its pause unless it can tell which rows changed
        since an earlier comparison. After a generation advance (``docs/generation-advance.md``)
        it can: a row below the map's generation has not been written since, on the source or on
        the copy. This compares those rows, writes over a copy row that differs - under the map's
        generation, as a fan-out would - and keeps, per group, the generation, the map and the
        tables it settled under. A cutover of that group from that map then reads, under its
        pause, only the rows that carry the generation (:func:`sde.reconcile.reconcile_since`).

        Under the project lock, beside no staging, cutover, index build or advance. A record is
        kept only for a pass that confirmed every row it wrote; one that did not removes the
        group's earlier record, so that its cutover reads the group whole.
        """
        from .reconcile import settle

        self._owner()
        with self.store.lock():
            state = self.store.read()
            if state["execution"] is not None:
                raise MigrationRefused(
                    "an unfinished operation of this project holds its tables; resume or abandon "
                    "it before a settle"
                )
            current = self.active_map()
            groups = sorted(name for name, spot in current.groups.items() if spot.also_write)
            if not groups:
                raise MigrationRefused(
                    "the map in force names no copy to settle: a settle follows a staging, its "
                    "backfill and an advance, and precedes the cutover"
                )
            context = InspectionContext(self.model, current, self.engines, self.project_id)
            tables: dict[str, list[dict[str, Any]]] = {}
            reach = map_reach(current, self.native)
            for group in groups:
                spot = current.groups[group]
                rows: list[dict[str, Any]] = []
                for material in spot.all():
                    native = self.native.get(material.engine)
                    if native is None:
                        raise MigrationRefused(
                            f"{group} has a copy in {material.engine}, which this operator is "
                            f"not bound to: a settle needs the binding of every engine it reads "
                            f"or writes"
                        )
                    for entity, table in sorted(material.layout.tables.items()):
                        # As a cutover takes them: a table with a row policy is refused here, and a
                        # copy's table without its creator's mark (§7r) before a row is written.
                        identity = (
                            native.identity(table)
                            if material is spot.source
                            else self._copy_owned(
                                current, material.id, group, table, state, reach
                            )
                        )
                        rows.append(
                            {
                                "engine": material.engine,
                                "materialization": material.id,
                                "entity": entity,
                                "role": "source" if material is spot.source else "target",
                                "identity": identity.as_record(),
                            }
                        )
                if not settles_before_a_pause(context, group):
                    raise MigrationRefused(
                        f"{group} cannot be settled before its cutover: an engine of it has no "
                        f"read by write generation, so its cutover compares it whole"
                    )
                tables[group] = rows
            settled = dict(state.get("settled", {}))
            records: list[dict[str, Any]] = []
            for group in groups:
                spot = current.groups[group]
                assert spot.write_epoch is not None  # a group with a copy carries a generation
                done = settle(context, group, epoch=spot.write_epoch, chunk_rows=self.chunk_rows)
                record = {
                    "write_epoch": spot.write_epoch,
                    "map_version": current.map_version,
                    "map_fingerprint": current.fingerprint,
                    "tables": tables[group],
                    **done.as_record(),
                }
                if done.settled:
                    settled[group] = record
                else:
                    settled.pop(group, None)
                records.append({"group": group, "settled": done.settled, **record})
            for history in ("stages", "indexes", "advances"):
                state.setdefault(history, {})
            state["settled"] = settled
            self.store.write(state)
            return records

    def erase(
        self, request: ErasureRequest, *, execute: bool = False, at: str | None = None
    ) -> ErasurePlan | ErasureReceipt:
        """Plan the erasure of one entity's keys and, with ``execute``, run it (``sde.erasure``).

        Under the project lock, so beside no staging, cutover or index build of this project: an
        unfinished one is refused, not waited for. The receipt is also kept in the project
        directory as ``erasures/<id>.json``.
        """
        from .erasure import erasure_secret, execute_erasure, plan_erasure, write_receipt

        self._owner()
        with self.store.lock():
            state = self.store.read()
            if state["execution"] is not None:
                raise MigrationRefused(
                    "an unfinished operation of this project holds its tables; resume or abandon "
                    "it before an erasure"
                )
            # Rows go from every copy of the entity first: each copy's table is one this project
            # created and marked for its group (§7r), and each source's table no other name of
            # the map reaches, checked before anything is read or deleted.
            erased = self.active_map()
            reach = map_reach(erased, self.native)
            for name, spot in sorted(erased.groups.items()):
                if spot.write_epoch is None or request.entity not in spot.source.layout.tables:
                    continue
                for material in spot.derived:
                    if material.engine in self.native:
                        table = material.layout.tables[request.entity]
                        self._copy_owned(erased, material.id, name, table, state, reach)
                source = spot.source
                refuse_shared_source(
                    reach, name, source.id, request.entity, source.layout.tables[request.entity]
                )
            plan = plan_erasure(self, request)
            if not execute:
                return plan
            if state.get("settled"):
                # Rows are about to go from every table of the map, and a settle stood on the
                # rows it compared staying where they were: what it settled is void, before the
                # first row goes, so that an erasure that stops halfway leaves nothing settled.
                state["settled"] = {}
                self.store.write(state)
            receipt = execute_erasure(self, plan, erasure_secret(self.store.root), at=at)
            write_receipt(self.store.root, receipt)
            return receipt

    def mark_sources(self) -> dict[str, list[Any]]:
        """A source's mark on each source of the map in force an earlier cutover left with the
        staging's mark, and every source still carrying a copy's or a staging's mark, with why.

        A cutover before the source's mark existed left a staged copy it made the source with the
        staging's mark (``sde.stage1``), and a session of this release refuses a source that carries
        one (format contract §7r): it cannot tell that source from a staged copy a forged map names
        as one. This project's records can. The mark names the stage; the stage's receipt says it
        was completed for the group the map in force gives the table to; and a cutover of that
        group completed with success. Only then is the source's mark written, after the table's
        identity is read again. Nothing a cutover requires of a table is asked of it here - no
        trigger, no dependent view, no pipeline - since a mark moves no row and no authority.

        Only groups with a write generation are read: a session reads the marks of those alone.
        Returns ``marked``, what it marked; ``left``, each of their sources that still carries a
        copy's or a staging's mark, which a session of this release refuses, with the reason it
        was left; and ``unexamined``, the bindings of those groups this operator is not bound to,
        whose sources it could not read. A table it cannot mark does not stop it: the next one is
        read. Idempotent; under the project lock, beside no unfinished operation.
        """
        from .engines._operator import MarkNotWritten
        from .engines._staging import native_staging
        from .ownership import copy_marks, source_marker

        self._owner()
        with self.store.lock():
            state = self.store.read()
            if state["execution"] is not None:
                raise MigrationRefused(
                    "an unfinished operation of this project holds its tables; resume or abandon "
                    "it before marking sources"
                )
            current = self.active_map()
            promoted = frozenset(
                str(receipt.get("group"))
                for record in state.get("completed", {}).values()
                for receipt in [record.get("receipt") or {}]
                if receipt.get("outcome") == "success"
            )
            marked: list[dict[str, Any]] = []
            left: list[dict[str, Any]] = []
            unexamined: set[str] = set()
            for group, spot in sorted(current.groups.items()):
                source = spot.source
                if spot.write_epoch is None:
                    # A session reads the marks of a group's tables only where its writes carry a
                    # generation (format contract §7r), and nothing marked a group without one.
                    continue
                native = self.native.get(source.engine)
                if native is None:
                    unexamined.add(source.engine)
                    continue
                for entity, table in sorted(source.layout.tables.items()):
                    where = {
                        "group": group, "entity": entity, "engine": source.engine, "table": table
                    }
                    try:
                        found = native_staging(native).marker(table)
                        if found is None or not copy_marks(found[1]):
                            continue
                        assert found[1] is not None
                        reason = self._unpromoted(found[1], group, table, state, promoted)
                        if reason is None:
                            native.write_source_mark(
                                table, found[0], source_marker(self.project_id, group, table)
                            )
                    except MarkNotWritten as exc:
                        reason = _NOT_WRITTEN.get(exc.cause, _NOT_WRITTEN[None])
                    except MigrationRefused as exc:
                        reason = str(exc)
                    except EngineError:
                        reason = _NOT_WRITTEN[None]
                    if reason is None:
                        marked.append(where)
                    else:
                        left.append({**where, "reason": reason})
            return {"marked": marked, "left": left, "unexamined": sorted(unexamined)}

    def _unpromoted(
        self,
        mark: str,
        group: str,
        table: str,
        state: Mapping[str, Any],
        promoted: frozenset[str],
    ) -> str | None:
        """Why this project's records do not show ``table`` promoted by a cutover of ``group``,
        whose mark ``mark`` is a copy's or a staging's; ``None`` when they do."""
        from .ownership import STAGE_MARKER, stage_marker

        if STAGE_MARKER.fullmatch(mark) is None:
            # A copy's mark, or a staging's that names its group: a cutover of this release writes
            # the source's mark on the copy it promotes, so no cutover left either on a source.
            return (
                "it carries a copy's mark or a staging's of this release, which a cutover of this "
                "release replaces with the source's when it promotes the copy: no record of this "
                "project has it promoted"
            )
        stage = mark.split(".")[3]
        receipt = (state.get("stages", {}).get(stage) or {}).get("receipt") or {}
        if mark != stage_marker(self.project_id, stage, table):
            return "its staging's mark is not one this project wrote on this table"
        if receipt.get("outcome") != "prepared":
            return f"no staging {stage} completed in this project's records"
        if receipt.get("group") != group:
            return (
                f"staging {stage} prepared {receipt.get('group')!r}, and the map in force gives "
                f"the table to {group!r}"
            )
        if group not in promoted:
            return f"no cutover of {group!r} completed with success in this project's records"
        return None

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

        relayout = plan.as_record()["protocol"] == 2
        source = self.engines.get(plan.before.groups[plan.group].source.engine)
        if relayout and getattr(source, "dialect", None) == "elasticsearch":
            raise MigrationRefused(
                "Elasticsearch has no physical design to relayout: a layout there names its "
                "fields and nothing else, so a relayout (cutover protocol 2) has no object"
            )
        plan.check_current(self.active_map())
        # Engines whose groups carry no write generation (contract 6) need no binding here.
        fenced = fenced_groups(plan.before)
        needed = {m.engine for g in fenced.values() for m in g.all()}
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
        target_id = plan.before.groups[plan.group].derived[0].id
        for group, placement in fenced.items():
            for material in placement.all():
                keys = {entity: self.model.entity(entity).key for entity in material.layout.tables}
                findings = self.engines[material.engine].validate_schema(material.layout, keys=keys)
                # Only the fresh target must match its authorized design exactly; the current
                # source's physical state is what the move may be correcting, not a reason to stop.
                if group == plan.group and material.id == target_id:
                    refuse_findings(findings, MigrationRefused)
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
                raise newer_map("a newer map was adopted before this local cutover", name,
                                self.engines[name].dialect, watermark, plan.before.map_version)
        tables = []
        reach = map_reach(plan.before, self.native)
        for material in spot.all():
            for entity, table in sorted(material.layout.tables.items()):
                # The cutover repairs, empties and refills the copy: its table is one this project
                # created and marked for this group (§7r), checked before anything is recorded.
                identity = (
                    self.native[material.engine].identity(table)
                    if material is spot.source
                    else self._copy_owned(
                        plan.before, material.id, plan.group, table, state, reach
                    )
                )
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
            "settled": self._settlement(plan, state, tables),
        }

    def _settlement(
        self, plan: CutoverPlan, state: dict[str, Any], tables: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """The settle a cutover of ``plan``'s group can stand on, or None.

        Made from the map the packet starts from - the same fingerprint, so that no advance,
        index build or other map came between - at its generation, and over the tables the
        cutover has just qualified, by their native identities.
        """
        record: dict[str, Any] | None = state.get("settled", {}).get(plan.group)
        if record is None:
            return None
        if (
            record["map_fingerprint"] != plan.before.fingerprint
            or record["write_epoch"] != plan.source_epoch
        ):
            return None
        def identities(rows: list[dict[str, Any]]) -> dict[tuple[str, str], Any]:
            return {(row["materialization"], row["entity"]): row["identity"] for row in rows}

        return record if identities(record["tables"]) == identities(tables) else None

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

    def _native(self, execution: dict[str, Any], role: str, action: str) -> None:
        """``pause`` or ``retire`` on each binding's tables of ``role``."""
        from .engines._operator import TableIdentity

        for name, native in self.native.items():
            tables = [
                TableIdentity(**row["identity"])
                for row in self._tables(execution, role)
                if row["engine"] == name
            ]
            if tables:
                self._call(getattr(native, action), tables)

    def _mark_source(self, execution: dict[str, Any], group: str) -> None:
        """A source's mark on each table of the copy a successful cutover made the source."""
        from .engines._operator import TableIdentity
        from .ownership import source_marker

        for name, native in self.native.items():
            tables = [
                TableIdentity(**row["identity"])
                for row in self._tables(execution, "target")
                if row["engine"] == name
            ]
            if tables:
                marks = {
                    table.name: source_marker(self.project_id, group, table.name)
                    for table in tables
                }
                self._call(native.mark_source, tables, marks)

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

    def _repair(self, plan: CutoverPlan, execution: dict[str, Any]) -> Reconciliation | None:
        """Make the copy the frozen source's equal, and say which way.

        By what differs where every engine of the copy can and the copy is mostly there
        (:mod:`sde.reconcile`): one read of each side and a write of the rows that differ, which
        after a backfill and under fan-out are few. Whole, as before - emptied and refilled -
        where an engine cannot, where the copy was never filled, where the operator's
        configuration says ``refill``, where the copy holds a key the source lacks, which only
        emptying removes, where a row this repair wrote could not be read back, and where its
        walk of the source read another number of rows than the source counts. What comes back
        decides the comparison that follows: the rows this repair wrote, or every row.
        """
        context = InspectionContext(self.model, plan.before, self.engines, self.project_id)
        if (
            self.repair == "diff"
            and execution.get("settled") is not None
            and settles_before_a_pause(context, plan.group)
        ):
            # Settled before the pause: the rows below the generation are the copy's equal, and
            # only those written since the advance are read here.
            since = reconcile_since(
                context,
                plan.group,
                since=plan.source_epoch,
                epoch=plan.maintenance_epoch,
                chunk_rows=self.chunk_rows,
                call=self._call,
            )
            if since.settled:
                execution["repair"] = {"mode": "since", **since.as_record()}
                return since
            # A key the source lacks or a write that could not be read back: only the whole
            # refill and the whole comparison settle it.
            self._refill(plan, execution)
            execution["repair"] = {"mode": "refill", "after": since.as_record()}
            return None
        if (
            self.repair == "diff"
            and repairs_what_differs(context, plan.group)
            and copy_is_mostly_there(context, plan.group, call=self._call)
        ):
            done = reconcile(
                context,
                plan.group,
                epoch=plan.maintenance_epoch,
                chunk_rows=self.chunk_rows,
                call=self._call,
            )
            if done.settled:
                execution["repair"] = {"mode": "diff", **done.as_record()}
                return done
            # A key the source lacks, a write that could not be read back, or a walk that did
            # not read the whole source: only the whole refill and the whole comparison settle it.
            self._refill(plan, execution)
            execution["repair"] = {"mode": "refill", "after": done.as_record()}
            return None
        self._refill(plan, execution)
        execution["repair"] = {"mode": "refill"}
        return None

    def _refill(self, plan: CutoverPlan, execution: dict[str, Any]) -> None:
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
        for row in self._tables(execution, "source" if outcome == "success" else "target"):
            identity = row["identity"]
            name = [identity[key] for key in ("dialect", "server", "database", "namespace", "name")]
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
                state, "confirm_source_denied", lambda: self._native(execution, "source", "retire")
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
            # The copy is the source from here: its mark says so, so no later map takes it for a
            # copy again (format contract §7r).
            self._step(
                state, "mark_target_source", lambda: self._mark_source(execution, plan.group)
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
                state, "confirm_target_denied", lambda: self._native(execution, "target", "retire")
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
            for name, engine in self.engines.items():
                value = engine.map_watermark()
                if value is not None and value > candidate.map_version:
                    raise newer_map("a newer map appeared while completing cutover", name,
                                    engine.dialect, value, candidate.map_version)
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
            # Local, and numbers only: which way the copy was repaired and how many rows that
            # wrote. The receipt keeps the fields the control plane reads.
            "repair": execution.get("repair"),
        }
        state["active_map"] = json.loads(plan.candidate_payload(outcome))
        if "settled" in state:
            # Settled under the map this cutover replaced: void now, and kept no longer.
            state["settled"].pop(plan.group, None)
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
                self.store.confirm()
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
                self._step(state, "deny_target", lambda: self._native(execution, "target", "pause"))
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
                self._step(state, "deny_source", lambda: self._native(execution, "source", "pause"))
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
                reconciled = self._step(state, "repair", lambda: self._repair(plan, execution))
                context = InspectionContext(self.model, plan.before, self.engines, self.project_id)
                spot = plan.before.groups[plan.group]
                # After a repair of what differs, the rows it wrote and both counts; after a
                # refill, every row. Under the same barriers either way.
                compare = (
                    None
                    if reconciled is None
                    else partial(
                        compare_stamped, epoch=plan.maintenance_epoch, reconciled=reconciled
                    )
                )
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
                        compare=compare,
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

    def stage(self, plan: StagingPlan) -> StagingReceipt:
        from ._operator_deadline import DeadlineInterrupt, OperatorDeadline
        from .staging_operator import execute_stage

        self._owner()
        try:
            with OperatorDeadline() as deadline:
                self._alarm = deadline
                deadline.arm(30000)
                return execute_stage(self, plan)
        except DeadlineInterrupt as exc:
            self._interrupted()
            raise CutoverRecoveryRequired(
                "staging deadline interrupted an operation; reconnect and resume its state"
            ) from exc
        finally:
            self._alarm = None

    def index(self, plan: IndexPlan) -> IndexReceipt:
        """Build the indexes ``plan`` adds on the tables in force, then publish its next map.

        The deadline is the signed build budget, not a staging's 30 s: nothing is paused while an
        index builds, so the budget only bounds a build on a server that stopped answering.
        """
        from ._operator_deadline import DeadlineInterrupt, OperatorDeadline
        from .index_operator import execute_index

        self._owner()
        try:
            with OperatorDeadline() as deadline:
                self._alarm = deadline
                deadline.arm(plan.build_budget_ms)
                return execute_index(self, plan)
        except DeadlineInterrupt as exc:
            self._interrupted()
            raise CutoverRecoveryRequired(
                f"the index build budget interrupted an operation ({exc}); reconnect, then resume "
                "or abandon its state"
            ) from exc
        finally:
            self._alarm = None

    def advance(self, plan: AdvancePlan) -> AdvanceReceipt:
        """Raise the group's write generation by one on its source and copies, then publish
        ``plan``'s next map.

        The group's writes wait from the first barrier to the last release, which the signed
        pause budget bounds: an advance that cannot decide within it lifts its barrier and
        leaves the map in force.
        """
        from ._operator_deadline import DeadlineInterrupt, OperatorDeadline
        from .advance_operator import execute_advance

        self._owner()
        try:
            with OperatorDeadline() as deadline:
                self._alarm = deadline
                deadline.arm(30000)  # Qualification does not pause application writes.
                return execute_advance(self, plan)
        except DeadlineInterrupt as exc:
            self._interrupted()
            raise CutoverRecoveryRequired(
                "the advance deadline interrupted an operation; reconnect and resume its state"
            ) from exc
        finally:
            self._alarm = None

    def abandon(self) -> IndexReceipt | StagingReceipt:
        """Abandon the unfinished index build or staging before its decision.

        Only this operation's own objects are removed - the indexes a build created, the tables a
        staging created - and the map in force stays. A cutover has no abandonment: without a
        decision its recovery aborts it by itself.
        """
        from ._operator_deadline import DeadlineInterrupt, OperatorDeadline
        from .index_operator import abandon_index
        from .staging_operator import abandon_stage

        self._owner()
        try:
            with OperatorDeadline() as deadline:
                self._alarm = deadline
                deadline.arm(30000)  # an index build re-arms from its signed build budget
                with self.store.lock():
                    execution = self.store.read()["execution"]
                    kind = None if execution is None else execution.get("kind")
                if kind == "index":
                    return abandon_index(self)
                if kind == "staging":
                    return abandon_stage(self)
                if kind == "advance":
                    raise MigrationRefused(
                        "a generation advance is not abandoned by hand: resume lifts its barrier "
                        "and keeps the map in force before its decision, and completes it after"
                    )
                raise MigrationRefused("there is no unfinished index build or staging to abandon")
        except DeadlineInterrupt as exc:
            self._interrupted()
            raise CutoverRecoveryRequired(
                "abandoning was interrupted; reconnect and abandon again"
            ) from exc
        finally:
            self._alarm = None

    def resume(self) -> CutoverReceipt | StagingReceipt | IndexReceipt | AdvanceReceipt:
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

    def _resume(self) -> CutoverReceipt | StagingReceipt | IndexReceipt | AdvanceReceipt:
        self._owner()
        with self.store.lock():
            state = self.store.read()
            execution = state["execution"]
            if execution is None:
                raise MigrationRefused("there is no unfinished local operation")
            if execution.get("kind") == "staging":
                from .staging_operator import resume_stage

                return resume_stage(self, state)
            if execution.get("kind") == "index":
                from .index_operator import resume_index

                return resume_index(self, state)
            if execution.get("kind") == "advance":
                from .advance_operator import resume_advance

                return resume_advance(self, state)
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
                detached = cast("NativeOperator", native).rows(
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

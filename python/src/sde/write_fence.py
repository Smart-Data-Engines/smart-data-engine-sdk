"""Engine-enforced write generations and named barriers, entirely on the client's side.

These are primitives for a cutover executor, not permission to switch a placement map. The caller
must still close the source and target, compare their stable contents and durably commit its choice.
A generation travels with each write; checking it before an INSERT would leave a paused process
able to commit against a stale placement. Native CHECK constraints make that test part of INSERT.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .errors import MigrationRefused
from .generation import DRAIN_TABLE as DRAIN_TABLE
from .generation import EPOCH_COLUMN as EPOCH_COLUMN
from .generation import MAX_EPOCH as MAX_EPOCH
from .generation import check_epoch as check_epoch
from .placement import BACKFILL_TABLE, WATERMARK_TABLE

FENCE_PREFIX = "__sde_f_"
SETUP = FENCE_PREFIX + "setup"
ColumnState = Literal["absent", "valid", "conflict"]


@dataclass(frozen=True)
class FenceMetadata:
    identity: str
    column: ColumnState
    constraints: Mapping[str, str]


class FenceBackend(Protocol):
    """DDL capability held by provisioning/execution roles, never required by a runtime role."""

    def metadata(self, table: str) -> FenceMetadata: ...
    def add_column(self, table: str) -> None: ...
    def add_constraint(self, table: str, name: str, expression: str) -> None: ...
    def drop_constraint(self, table: str, name: str) -> None: ...
    def change_constraints(
        self, table: str, *, add: Sequence[tuple[str, str]], drop: Sequence[str]
    ) -> None:
        """Add and drop constraints in one change where the engine has one: one statement, whose
        metadata is written - and synced - once, and in which the constraints dropped give way to
        the ones added at one instant. Idempotent, as add and drop are."""
        ...
    def drain(self, table: str, *, project_id: str, hold: str) -> None: ...
    def restore(self, table: str, *, project_id: str, hold: str) -> None: ...


def _identity(value: str, label: str) -> None:
    if not isinstance(value, str) or re.fullmatch("[0-9a-f]{32}", value) is None:
        raise MigrationRefused(f"write fence {label} must be 32 lowercase hexadecimal digits")


def _predicate(value: str) -> str:
    """Normalize only our predicate grammar, preserving quoted identifiers and their spaces."""
    value = value.strip()
    if value.startswith("CHECK"):
        value = value[5:].strip()
    value = re.sub(r"\s+NOT\s+VALID$", "", value)
    while value.startswith("(") and value.endswith(")"):
        value = value[1:-1].strip()
    if value in ("true", "1"):
        return "1"
    if value in ("false", "0"):
        return "0"
    matched = re.fullmatch(
        r'\(*\s*(?:"__sde_write_epoch"|`__sde_write_epoch`|__sde_write_epoch)'
        r"\s*\)*\s*(>=|<=)\s*\(*\s*(?:([0-9]+)|'([0-9]+)'::bigint)\s*\)*",
        value,
    )
    if matched is None:
        return "<unrecognized>"
    number = matched.group(2) or matched.group(3)
    return EPOCH_COLUMN + matched.group(1) + str(int(number))


@dataclass(frozen=True)
class FenceState:
    identity: str
    project_id: str | None
    column: ColumnState
    minimums: tuple[int, ...]
    maximums: tuple[int, ...]
    holds: tuple[str, ...]
    retired: tuple[str, ...]

    @property
    def lower_epoch(self) -> int | None:
        return max(self.minimums) if self.minimums else None

    @property
    def upper_epoch(self) -> int | None:
        return min(self.maximums) if self.maximums else None

    @property
    def complete(self) -> bool:
        return (
            self.project_id is not None
            and self.column == "valid"
            and bool(self.minimums and self.maximums)
        )

    @property
    def closed(self) -> bool:
        low, high = self.lower_epoch, self.upper_epoch
        return bool(self.holds) or (low is not None and high is not None and low > high)

    @property
    def epoch(self) -> int | None:
        if self.complete and self.lower_epoch == self.upper_epoch:
            return self.lower_epoch
        return None

    def as_record(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "project_id": self.project_id,
            "column": self.column,
            "lower_epoch": self.lower_epoch,
            "upper_epoch": self.upper_epoch,
            "holds": list(self.holds),
            "retired": list(self.retired),
            "closed": self.closed,
        }


def fence_state(metadata: FenceMetadata) -> FenceState:
    owners: list[str] = []
    minima: list[int] = []
    maxima: list[int] = []
    holds: list[str] = []
    retired: list[str] = []
    for name, raw in sorted(metadata.constraints.items()):
        if not name.startswith(FENCE_PREFIX):
            continue
        suffix = name[len(FENCE_PREFIX) :]
        predicate = _predicate(raw)
        if re.fullmatch("owner_[0-9a-f]{32}", suffix):
            owners.append(suffix[6:])
            expected = "1"
        elif re.fullmatch("retired_[0-9a-f]{32}", suffix):
            retired.append(suffix[8:])
            expected = "1"
        elif suffix == "setup" or re.fullmatch("hold_[0-9a-f]{32}", suffix):
            holds.append("setup" if suffix == "setup" else suffix[5:])
            expected = "0"
        elif re.fullmatch("(?:min|max)_[1-9][0-9]*", suffix):
            value = int(suffix[4:])
            check_epoch(value)
            (minima if suffix.startswith("min_") else maxima).append(value)
            expected = EPOCH_COLUMN + (">=" if suffix.startswith("min_") else "<=") + str(value)
        else:
            raise MigrationRefused("unrecognized constraint in the reserved write-fence namespace")
        if predicate != expected:
            raise MigrationRefused(f"write fence constraint {name} has an unexpected predicate")
    if len(owners) > 1:
        raise MigrationRefused("the table carries write fences from more than one project")
    if not owners and (minima or maxima or holds or retired):
        raise MigrationRefused("write fence constraints have no project owner")
    return FenceState(
        metadata.identity,
        owners[0] if owners else None,
        metadata.column,
        tuple(sorted(minima)),
        tuple(sorted(maxima)),
        tuple(sorted(holds)),
        tuple(sorted(retired)),
    )


def _complete(state: FenceState) -> FenceState:
    if not state.complete:
        raise MigrationRefused("the write fence is not fully provisioned")
    return state


class WriteFence:
    """A table's provisioning capability. Serialize executor commands per project state directory.

    Writes carry a number from their own immutable placement. Replaying
    a stale administrative command cannot lower the native minimum. A partial command can leave the
    table closed, so an I/O error calls for inspection/resumption, not an assumption of rollback.
    """

    def __init__(self, backend: FenceBackend, table: str, *, project_id: str) -> None:
        _identity(project_id, "project_id")
        if not isinstance(table, str) or not table or "\0" in table:
            raise MigrationRefused("write fence table must be a nonempty identifier")
        if table in (DRAIN_TABLE, BACKFILL_TABLE, WATERMARK_TABLE):
            raise MigrationRefused("write fences cannot take over an SDK metadata table")
        self._backend = backend
        self.table = table
        self.project_id = project_id

    def state(self) -> FenceState:
        state = fence_state(self._backend.metadata(self.table))
        if state.project_id is not None and state.project_id != self.project_id:
            raise MigrationRefused("the write fence belongs to another project")
        if state.column == "conflict":
            raise MigrationRefused("the reserved write-epoch column has an incompatible definition")
        return state

    def _ready(self) -> FenceState:
        return _complete(self.state())

    def prepare(self, epoch: int) -> FenceState:
        epoch = check_epoch(epoch)
        state = self.state()
        if state.complete and "setup" not in state.holds:
            if state.epoch != epoch:
                raise MigrationRefused("provisioning cannot change an existing write epoch")
            return state
        if state.project_id is None and state.column != "absent":
            raise MigrationRefused("the reserved write-epoch column is not owned by this project")
        if state.lower_epoch is not None and state.lower_epoch > epoch:
            raise MigrationRefused("a write epoch cannot move backwards")
        if state.project_id is None:
            self._backend.add_constraint(self.table, FENCE_PREFIX + "owner_" + self.project_id, "1")
        self._backend.add_constraint(self.table, SETUP, "0")
        self._backend.add_column(self.table)
        self._bounds(epoch, state)
        self._backend.drain(self.table, project_id=self.project_id, hold="setup")
        self._backend.drop_constraint(self.table, SETUP)
        return self._ready()

    def freeze(self, request_id: str) -> FenceState:
        _identity(request_id, "request_id")
        state = self._ready()
        if request_id in state.retired:
            raise MigrationRefused("a completed write barrier id cannot be reused")
        self._backend.add_constraint(self.table, FENCE_PREFIX + "hold_" + request_id, "0")
        # Always drain, even on a retry: existence of the constraint proves admission is closed,
        # not that an INSERT which captured older metadata has finished.
        self._backend.drain(self.table, project_id=self.project_id, hold=request_id)
        return self._ready()

    def resume(self, request_id: str) -> FenceState:
        _identity(request_id, "request_id")
        self._backend.restore(self.table, project_id=self.project_id, hold=request_id)
        return self.freeze(request_id)

    def resume_prepare(self, epoch: int) -> FenceState:
        epoch = check_epoch(epoch)
        self._backend.restore(self.table, project_id=self.project_id, hold="setup")
        return self.prepare(epoch)

    def advance(self, epoch: int) -> FenceState:
        epoch = check_epoch(epoch)
        state = self._ready()
        if not state.holds:
            raise MigrationRefused("changing a write epoch needs a named write barrier")
        if state.lower_epoch is not None and epoch < state.lower_epoch:
            raise MigrationRefused("a write epoch cannot move backwards")
        return _complete(self._bounds(epoch, state))

    def _bounds(self, epoch: int, before: FenceState) -> FenceState:
        """Install ``epoch``'s bounds and remove the older ones, in one change of the table.

        One statement where the engine has one: on ClickHouse an ALTER writes and syncs the table's
        metadata, and that sync is most of its time, so four statements paid it four times in a
        pause; on PostgreSQL each took the table's lock again. Only bounds older than
        ``epoch``, named as ``before`` showed them, are dropped: a stale caller never removes a
        newer constraint installed after that read, and the check after the change finds it.
        Measured: ``docs/qualification/fence-in-one-statement/``.
        """
        older = [
            *(FENCE_PREFIX + "max_" + str(old) for old in before.maximums if old < epoch),
            *(FENCE_PREFIX + "min_" + str(old) for old in before.minimums if old < epoch),
        ]
        self._backend.change_constraints(
            self.table,
            add=[
                (FENCE_PREFIX + "min_" + str(epoch), f"{EPOCH_COLUMN} >= {epoch}"),
                (FENCE_PREFIX + "max_" + str(epoch), f"{EPOCH_COLUMN} <= {epoch}"),
            ],
            drop=older,
        )
        after = self.state()
        if after.lower_epoch is not None and after.lower_epoch > epoch:
            raise MigrationRefused(
                "another executor installed a newer write epoch; table stays closed"
            )
        return after

    def release(self, request_id: str) -> FenceState:
        _identity(request_id, "request_id")
        state = self._ready()
        if state.epoch is None:
            raise MigrationRefused("cannot release a barrier with an incomplete epoch change")
        if request_id not in state.holds:
            return state
        # Completion is recorded no later than admission is released - in the same change - so a
        # delayed invocation never recreates a completed hold, nor reuses its id for a later
        # generation and inherits an old release.
        self._backend.change_constraints(
            self.table,
            add=[(FENCE_PREFIX + "retired_" + request_id, "1")],
            drop=[FENCE_PREFIX + "hold_" + request_id],
        )
        return self._ready()

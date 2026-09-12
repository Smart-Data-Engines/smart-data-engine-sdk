"""Recording backend for the public native-fencing conformance protocol."""

from __future__ import annotations

from typing import Any

from sde.write_fence import FenceMetadata, fence_state


class MemoryFences:
    def __init__(self) -> None:
        self.identity = "table-identity"
        self.column = "absent"
        self.constraints: dict[str, str] = {}
        self.calls: list[tuple[Any, ...]] = []
        self.fail_after: int | None = None

    def metadata(self, table: str) -> FenceMetadata:
        return FenceMetadata(self.identity, self.column, dict(self.constraints))  # type: ignore[arg-type]

    def done(self, *call: Any) -> None:
        self.calls.append(call)
        if len(self.calls) == self.fail_after:
            raise OSError("lost the response after the DDL took effect")

    def add_column(self, table: str) -> None:
        self.column = "valid"
        self.done("column", table)

    def add_constraint(self, table: str, name: str, expression: str) -> None:
        self.constraints.setdefault(name, expression)
        self.done("add", table, name, expression)

    def drop_constraint(self, table: str, name: str) -> None:
        self.constraints.pop(name, None)
        self.done("drop", table, name)

    def drain(self, table: str, *, project_id: str, hold: str) -> None:
        state = fence_state(self.metadata(table))
        assert state.project_id == project_id
        assert hold in state.holds
        self.done("drain", table, project_id, hold)

    def restore(self, table: str, *, project_id: str, hold: str) -> None:
        self.done("restore", table, project_id, hold)

    def accepts(self, epoch: int) -> bool:
        state = fence_state(self.metadata("events"))
        return (
            state.complete
            and not state.closed
            and state.lower_epoch is not None
            and state.lower_epoch <= epoch
            and state.upper_epoch is not None
            and epoch <= state.upper_epoch
        )

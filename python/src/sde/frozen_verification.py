"""Drain named native barriers and compare stable copies, including target-only rows.

This is a client-side data operation. It neither chooses a placement nor activates a map. It
leaves its barriers installed on success and failure; the executor owns the durable decision
which permits release, rollback or activation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from time import perf_counter_ns
from typing import Any, cast

from .errors import MigrationRefused
from .generation import Fencable, check_epoch
from .inspection import InspectionContext
from .migration import CHUNK_ROWS, VerifyReport, verify
from .verification import VerificationRequest
from .write_fence import FenceState, WriteFence


@dataclass(frozen=True)
class FrozenTable:
    engine: str
    materialization: str
    table: str
    identity: str
    project_id: str
    epoch: int
    hold_id: str

    def as_record(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "materialization": self.materialization,
            "table": self.table,
            "identity": self.identity,
            "project_id": self.project_id,
            "epoch": self.epoch,
            "hold_id": self.hold_id,
        }


@dataclass(frozen=True)
class FrozenVerifyReport:
    comparison: VerifyReport
    barriers: tuple[FrozenTable, ...]
    elapsed_ms: int

    @property
    def matched(self) -> bool:
        return (
            self.comparison.matched and self.comparison.rows_source == self.comparison.rows_target
        )

    def as_record(self) -> dict[str, Any]:
        return {
            "protocol": 1,
            "comparison": self.comparison.as_record(),
            "barriers": [barrier.as_record() for barrier in self.barriers],
            "elapsed_ms": self.elapsed_ms,
            "matched": self.matched,
        }


def _matches(state: FenceState, wanted: FrozenTable) -> None:
    if state.identity != wanted.identity or state.project_id != wanted.project_id:
        raise MigrationRefused("a frozen comparison table changed identity or project")
    if not state.complete or state.epoch != wanted.epoch or wanted.hold_id not in state.holds:
        raise MigrationRefused("a frozen comparison lost its named barrier or write generation")


def verify_frozen(
    context: InspectionContext,
    group: str,
    *,
    request: VerificationRequest,
    hold_id: str,
    epochs: Mapping[str, int],
    chunk_rows: int = CHUNK_ROWS,
    at: str | None = None,
) -> FrozenVerifyReport:
    """Acquire/drain each barrier and compare exact logical content; do not release the holds.

    `epochs` names materialization ids, including the source. They can differ during operator
    maintenance and therefore cannot be inferred from a runtime session's single group epoch.
    A caller must budget this work and handle uncertain engine I/O as recovery, not as success.
    """
    if not isinstance(hold_id, str) or re.fullmatch("[0-9a-f]{32}", hold_id) is None:
        raise MigrationRefused(
            "a frozen comparison hold id must be 32 lowercase hexadecimal digits"
        )
    if chunk_rows < 1:
        raise MigrationRefused("a frozen comparison needs a positive chunk size")
    request.check_session(context.placement, project_id=context.project_id, group=group)
    if at is not None:
        request.check_time(at)
    spot = context.placement.placement_of(group)
    materials = (spot.source, *spot.also_write)
    if not spot.also_write or set(epochs) != {material.id for material in materials}:
        raise MigrationRefused("frozen comparison epochs must name exactly the source and copy ids")
    checked_epochs = {name: check_epoch(value) for name, value in epochs.items()}
    planned: list[tuple[WriteFence, FrozenTable]] = []
    for material in sorted(materials, key=lambda value: (value.engine, value.id)):
        engine = context.engines[material.engine]
        if not callable(getattr(engine, "write_fence", None)) or not callable(
            getattr(engine, "validate_schema", None)
        ):
            raise MigrationRefused(
                "frozen comparison requires native write fences and schema checks"
            )
        native = cast(Fencable, engine)
        native.validate_schema(material.layout)
        for table in sorted(material.layout.tables.values()):
            fence = native.write_fence(table, project_id=context.project_id)
            state = fence.state()
            epoch = checked_epochs[material.id]
            if hold_id in state.retired:
                raise MigrationRefused("a frozen comparison cannot reuse a retired barrier id")
            if not state.complete or state.epoch != epoch:
                raise MigrationRefused(
                    "a frozen comparison table is not at the expected write generation"
                )
            planned.append(
                (
                    fence,
                    FrozenTable(
                        material.engine,
                        material.id,
                        table,
                        state.identity,
                        context.project_id,
                        epoch,
                        hold_id,
                    ),
                )
            )
    started = perf_counter_ns()
    for fence, wanted in planned:
        # Constraint presence alone does not drain an old ClickHouse INSERT. Always execute the
        # native drain, including when resuming an already installed hold.
        _matches(fence.freeze(hold_id), wanted)
    comparison = verify(context, group, request=request, chunk_rows=chunk_rows, at=at)
    for fence, wanted in planned:
        _matches(fence.state(), wanted)
    return FrozenVerifyReport(
        comparison,
        tuple(wanted for _, wanted in planned),
        (perf_counter_ns() - started) // 1_000_000,
    )

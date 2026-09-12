"""Generation metadata for shared vectors; rows/calls use the existing SDK memory engine."""

from __future__ import annotations

from typing import Any

from _write_fence import MemoryFences

from sde.write_fence import EPOCH_COLUMN, FENCE_PREFIX, WriteFence


def bind_generation_metadata(engines: dict[str, Any], metadata: dict[str, Any]) -> None:
    for name, tables in metadata.items():
        backends: dict[str, MemoryFences] = {}
        for table, record in tables.items():
            backend = MemoryFences()
            backend.identity = name + "/" + table
            backend.column = "valid"
            epoch = record["epoch"]
            backend.constraints = {
                FENCE_PREFIX + "owner_" + record["project_id"]: "1",
                FENCE_PREFIX + "min_" + str(epoch): EPOCH_COLUMN + " >= " + str(epoch),
                FENCE_PREFIX + "max_" + str(epoch): EPOCH_COLUMN + " <= " + str(epoch),
            }
            backends[table] = backend

        def factory(
            table: str, *, project_id: str, backends: dict[str, MemoryFences] = backends
        ) -> WriteFence:
            return WriteFence(backends[table], table, project_id=project_id)

        engines[name].write_fence = factory
        engines[name].validate_schema = lambda _layout: None

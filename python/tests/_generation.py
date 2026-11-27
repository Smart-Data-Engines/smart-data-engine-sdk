"""Generation metadata for shared vectors; rows/calls use the existing SDK memory engine."""

from __future__ import annotations

from typing import Any

from _write_fence import MemoryFences

from sde.ownership import copy_marker
from sde.write_fence import EPOCH_COLUMN, FENCE_PREFIX, WriteFence


def copy_marks(placement: Any) -> dict[str, dict[str, str]]:
    """The marks ``prepare_schema`` writes on a map's copies (format contract §7r), by engine and
    table: what a fake engine of a provisioned deployment holds."""
    marks: dict[str, dict[str, str]] = {}
    if placement.contract < 4 or placement.project_id is None:
        return marks
    for group, spot in placement.groups.items():
        if spot.write_epoch is None:
            continue
        for material in spot.derived:
            for table in material.layout.tables.values():
                marks.setdefault(material.engine, {})[table] = copy_marker(
                    placement.project_id, group, table
                )
    return marks


def bind_generation_metadata(
    engines: dict[str, Any],
    metadata: dict[str, Any],
    *,
    record_fences: bool = False,
    marks: dict[str, dict[str, str]] | None = None,
) -> None:
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
            if record_fences:
                original = backend.done

                def note(*call: Any, original: Any = original, engine: Any = engines[name]) -> None:
                    original(*call)
                    operation, table_name, *arguments = call
                    engine.recorded.note(
                        engine.name, "fence_" + operation, table=table_name, arguments=arguments
                    )

                # Every fence call joins the engines' journal: the test's own hook, not an API.
                backend.done = note  # type: ignore[method-assign]
            backends[table] = backend

        def factory(
            table: str, *, project_id: str, backends: dict[str, MemoryFences] = backends
        ) -> WriteFence:
            return WriteFence(backends[table], table, project_id=project_id)

        engines[name].write_fence = factory
        # The real adapters return physical findings; a fake with no catalogue has none.
        engines[name].validate_schema = lambda _layout, keys=None: ()
        # Each table its own, and a copy's carrying its mark: a provisioned deployment (§7r).
        engine_marks = (marks or {}).get(name, {})

        def table_facts(
            names: list[str],
            *,
            engine: str = name,
            known: frozenset[str] = frozenset(tables),
            engine_marks: dict[str, str] = engine_marks,
        ) -> dict[str, tuple[frozenset[tuple[str, ...]], str | None]]:
            return {
                table: (
                    frozenset({("memory", engine, table)}) if table in known else frozenset(),
                    engine_marks.get(table),
                )
                for table in names
            }

        engines[name].table_facts = table_facts

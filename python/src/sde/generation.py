"""Wire vocabulary for generation-bearing maps, independent of drivers and placement classes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol, cast

from .errors import MigrationRefused

if TYPE_CHECKING:
    from .model import LogicalModel
    from .physical import PhysicalFinding
    from .placement import GroupPlacement, PhysicalLayout, PlacementMap
    from .session import Engine
    from .write_fence import WriteFence


class Fencable(Protocol):
    def write_fence(self, table: str, *, project_id: str) -> WriteFence: ...
    def validate_schema(
        self, layout: PhysicalLayout, *, keys: Mapping[str, Sequence[str]] | None = None
    ) -> tuple[PhysicalFinding, ...]: ...


DRAIN_TABLE = "__sde_fence_drains"
EPOCH_COLUMN = "__sde_write_epoch"
MAX_EPOCH = 9_007_199_254_740_991
GENERATIONS_SINCE = 4
"""The placement map contract that introduced ``project_id`` and per-group ``write_epoch``."""


def check_epoch(epoch: int) -> int:
    if type(epoch) not in (int, float) or not 1 <= epoch <= MAX_EPOCH or int(epoch) != epoch:
        raise MigrationRefused("write epoch must be a positive safe integer")
    return int(epoch)


def json_numbers(value: Any) -> Any:
    """JSON has one number type. Match integral JS numbers before checking/signing a v4 map.

    Canonical encoding itself remains strict. Nonintegral/nonfinite numbers survive this pass and
    are refused by the map's canonical boundary. Earlier map contracts retain their old behavior.
    """
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: json_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_numbers(item) for item in value]
    if isinstance(value, tuple):
        return tuple(json_numbers(item) for item in value)
    return value


def check_map_project(placement: PlacementMap, project_id: str | None) -> str | None:
    if placement.contract < 4:
        return None
    if project_id is None or project_id != placement.project_id:
        raise MigrationRefused(
            "this generation-bearing map needs its locally configured project_id; "
            "do not learn that identity from the supplied map"
        )
    if placement.fingerprint is None:
        raise MigrationRefused("generation-bearing sessions need an immutable loaded placement map")
    return project_id


def fenced_groups(placement: PlacementMap) -> dict[str, GroupPlacement]:
    """The groups whose writes carry a generation, by name: every group of a contract-4 or 5 map.

    These are the only groups a local operator acts on. A contract-6 group without a generation is
    on an engine that cannot fence writes, which no staging, cutover or index build can reach, so
    an operator neither needs a binding for that engine nor reads its tables.
    """
    return {
        name: spot
        for name, spot in sorted(placement.groups.items())
        if spot.write_epoch is not None
    }


def refuse_a_fencing_engine(group: str, engine_name: str, engine: object) -> None:
    """Contract 6: a group without a write generation must be on an engine that cannot fence.

    A map names its engines and carries no dialect, so this is answerable only where the adapters
    are - when a session opens and when a schema is prepared.
    """
    if callable(getattr(engine, "write_fence", None)):
        raise MigrationRefused(
            f"group {group} carries no write generation on {engine_name}, which fences writes; "
            f"a map we issue gives every group on such an engine a generation, so this one was "
            f"built for another engine"
        )


def validate_generations(
    model: LogicalModel,
    placement: PlacementMap,
    engines: Mapping[str, Engine],
    project_id: str | None,
) -> tuple[PhysicalFinding, ...]:
    """Check generations and columns; return how tables differ from the declared physical design.

    The physical design is reported rather than refused: a running application must not stop
    because a table's sort key or index differs from the map (requirement 3.6). Columns, types
    and generations still refuse.
    """
    if placement.contract < 4:
        return ()
    findings: list[PhysicalFinding] = []
    local_project = check_map_project(placement, project_id)
    assert local_project is not None
    if model.version != placement.model_version:
        raise MigrationRefused("the generation-bearing map names another session model")
    for name in sorted(placement.groups):
        spot = placement.groups[name]
        if spot.write_epoch is None:
            # Contract 6: a group with no write generation, on an engine that cannot fence. The
            # map carries no dialect, so this is the first place either half can be checked.
            for material in spot.all():
                engine = engines[material.engine]
                refuse_a_fencing_engine(name, material.engine, engine)
                validate = getattr(engine, "validate_schema", None)
                if callable(validate):
                    keys = {entity: model.entity(entity).key for entity in material.layout.tables}
                    findings.extend(validate(material.layout, keys=keys))
            continue
        for material in spot.all():
            factory = getattr(engines[material.engine], "write_fence", None)
            if not callable(factory) or not callable(
                getattr(engines[material.engine], "validate_schema", None)
            ):
                raise MigrationRefused(
                    f"engine {material.engine} does not implement write generations"
                )
            keys = {entity: model.entity(entity).key for entity in material.layout.tables}
            findings.extend(
                cast(Fencable, engines[material.engine]).validate_schema(
                    material.layout, keys=keys
                )
            )
            for table in sorted(material.layout.tables.values()):
                state = (
                    cast(Fencable, engines[material.engine])
                    .write_fence(table, project_id=local_project)
                    .state()
                )
                if not state.complete or state.epoch != spot.write_epoch:
                    raise MigrationRefused(
                        f"the write generation for {name} is not active in {material.engine}; "
                        "provision the signed map or load the current map before opening a session"
                    )
    return tuple(findings)


def stamp_values(
    placement: PlacementMap, group: str, values: Mapping[str, Any]
) -> Mapping[str, Any]:
    epoch = placement.placement_of(group).write_epoch
    # Reserved in every group of a generation-bearing map, including one without a generation of
    # its own (contract 6): a read strips the column from every row such a map returns.
    if placement.contract >= GENERATIONS_SINCE and EPOCH_COLUMN in values:
        raise MigrationRefused("the write-epoch column is reserved for the SDK")
    if epoch is None:
        return values
    return {**values, EPOCH_COLUMN: epoch}


def logical_row(placement: PlacementMap, row: Any) -> Any:
    if placement.contract >= 4 and isinstance(row, dict):
        return {key: value for key, value in row.items() if key != EPOCH_COLUMN}
    return row

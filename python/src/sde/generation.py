"""Wire vocabulary for generation-bearing maps, independent of drivers and placement classes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol, cast

from .errors import MigrationRefused

if TYPE_CHECKING:
    from .model import LogicalModel
    from .placement import PhysicalLayout, PlacementMap
    from .session import Engine
    from .write_fence import WriteFence


class Fencable(Protocol):
    def write_fence(self, table: str, *, project_id: str) -> WriteFence: ...
    def validate_schema(self, layout: PhysicalLayout) -> None: ...


DRAIN_TABLE = "__sde_fence_drains"
EPOCH_COLUMN = "__sde_write_epoch"
MAX_EPOCH = 9_007_199_254_740_991


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


def validate_generations(
    model: LogicalModel,
    placement: PlacementMap,
    engines: Mapping[str, Engine],
    project_id: str | None,
) -> None:
    if placement.contract < 4:
        return
    local_project = check_map_project(placement, project_id)
    assert local_project is not None
    if model.version != placement.model_version:
        raise MigrationRefused("the generation-bearing map names another session model")
    for name in sorted(placement.groups):
        spot = placement.groups[name]
        for material in spot.all():
            factory = getattr(engines[material.engine], "write_fence", None)
            if not callable(factory) or not callable(
                getattr(engines[material.engine], "validate_schema", None)
            ):
                raise MigrationRefused(
                    f"engine {material.engine} does not implement write generations"
                )
            cast(Fencable, engines[material.engine]).validate_schema(material.layout)
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


def stamp_values(
    placement: PlacementMap, group: str, values: Mapping[str, Any]
) -> Mapping[str, Any]:
    epoch = placement.placement_of(group).write_epoch
    if epoch is None:
        return values
    if EPOCH_COLUMN in values:
        raise MigrationRefused("the write-epoch column is reserved for the SDK")
    return {**values, EPOCH_COLUMN: epoch}


def logical_row(placement: PlacementMap, row: Any) -> Any:
    if placement.contract >= 4 and isinstance(row, dict):
        return {key: value for key, value in row.items() if key != EPOCH_COLUMN}
    return row

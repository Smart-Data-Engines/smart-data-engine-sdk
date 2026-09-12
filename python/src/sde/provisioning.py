"""Prepare physical schema using client-owned provisioning connections before opening runtime."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from .errors import MigrationRefused
from .generation import Fencable, check_map_project
from .groups import colocation_groups
from .model import LogicalModel
from .placement import PlacementMap

if TYPE_CHECKING:
    from .session import Engine


def prepare_schema(
    model: LogicalModel,
    placement: PlacementMap,
    engines: Mapping[str, Engine],
    *,
    project_id: str | None = None,
) -> None:
    """Apply the described schema and initial generation; never advance an existing generation.

    Credentials remain in these local adapters. The runtime is opened afterwards on separately
    configured connections, so it need not receive the provisioning role's ALTER capability.
    """
    if model.version != placement.model_version:
        raise MigrationRefused("schema preparation needs the model named by the placement map")
    local_project = check_map_project(placement, project_id)
    needed = sorted(
        {material.engine for spot in placement.groups.values() for material in spot.all()}
    )
    if any(name not in engines for name in needed):
        raise MigrationRefused("schema preparation is missing an engine named by the map")
    if placement.contract >= 4 and any(
        not callable(getattr(engines[name], "write_fence", None)) for name in needed
    ):
        raise MigrationRefused("schema preparation needs native write generations on every engine")
    for group in colocation_groups(model):
        spot = placement.placement_of(group.name)
        keys = {name: model.entity(name).key for name in group.members}
        for material in spot.all():
            engine = engines[material.engine]
            engine.ensure_schema(material.layout, keys=keys)
            if spot.write_epoch is not None:
                for table in sorted(material.layout.tables.values()):
                    assert local_project is not None
                    cast(Fencable, engine).write_fence(table, project_id=local_project).prepare(
                        spot.write_epoch
                    )

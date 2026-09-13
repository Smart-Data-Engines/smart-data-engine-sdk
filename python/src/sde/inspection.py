"""A local operator's data view, separate from an application session and its watermark."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from .errors import MigrationRefused
from .generation import check_map_project
from .model import LogicalModel
from .placement import PlacementMap

if TYPE_CHECKING:
    from .session import Engine


class MigrationView(Protocol):
    @property
    def model(self) -> LogicalModel: ...
    @property
    def placement(self) -> PlacementMap: ...
    @property
    def engines(self) -> Mapping[str, Engine]: ...
    @property
    def project_id(self) -> str | None: ...


@dataclass(frozen=True)
class InspectionContext:
    """Read a migration's physical data with local operator connections, without runtime writes.

    The before-map continues to identify the physical copies when their native epochs differ
    during maintenance. The operation must validate those actual epochs and barriers separately.
    Construction does not adopt a runtime map or advance any watermark.
    """

    model: LogicalModel
    placement: PlacementMap
    engines: Mapping[str, Engine]
    project_id: str

    def __post_init__(self) -> None:
        if self.placement.contract < 4:
            raise MigrationRefused(
                "operator inspection requires a generation-bearing placement map"
            )
        check_map_project(self.placement, self.project_id)
        if self.model.version != self.placement.model_version:
            raise MigrationRefused("operator inspection needs the model named by its map")
        missing = sorted(
            {
                material.engine
                for group in self.placement.groups.values()
                for material in group.all()
            }
            - set(self.engines)
        )
        if missing:
            raise MigrationRefused(f"operator inspection is missing engines {missing}")
        object.__setattr__(self, "engines", MappingProxyType(dict(self.engines)))

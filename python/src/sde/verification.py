"""Bind a comparison to one request, project and exact placement map, without carrying rows.

A matching row count is not evidence about a particular migration. The request is persisted by
the controller before comparison and echoed by the verifier only after checking its local session.
Its unpredictable id distinguishes repeated verification rounds; the project id comes from local
client configuration, so identical models/maps in two projects are not interchangeable evidence.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .errors import MigrationRefused

if TYPE_CHECKING:
    from .placement import PlacementMap

REQUEST_PROTOCOL = 1


def aware_time(value: str) -> datetime:
    pattern = (
        r"\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
        r"(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
    )
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise MigrationRefused("verification time must be an ISO timestamp with an offset")
    try:
        result = datetime.fromisoformat(value[:-1] + "Z" if value.endswith("z") else value)
    except (TypeError, ValueError) as exc:
        raise MigrationRefused("verification time must be an ISO timestamp with an offset") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise MigrationRefused("verification time must include its UTC offset")
    return result


def _hex(value: str, width: int, field: str) -> None:
    if not isinstance(value, str) or re.fullmatch(f"[0-9a-f]{{{width}}}", value) is None:
        raise MigrationRefused(f"verification {field} must be {width} lowercase hexadecimal digits")


def _name(value: str) -> None:
    if not isinstance(value, str) or not value:
        raise MigrationRefused("verification names must be nonempty strings")


@dataclass(frozen=True)
class VerificationRequest:
    request_id: str
    project_id: str
    model_version: str
    map_version: int
    map_fingerprint: str
    group: str
    source_engine: str
    source_id: str
    targets: tuple[tuple[str, str], ...]
    requested_at: str
    requires_signature: bool

    def __post_init__(self) -> None:
        for value, width, name in (
            (self.request_id, 32, "request_id"),
            (self.project_id, 32, "project_id"),
            (self.model_version, 16, "model_version"),
            (self.map_fingerprint, 64, "map_fingerprint"),
        ):
            _hex(value, width, name)
        if type(self.map_version) is not int or not 1 <= self.map_version <= 9_007_199_254_740_991:
            raise MigrationRefused("verification map_version must be a positive integer")
        if type(self.requires_signature) is not bool:
            raise MigrationRefused("verification requires_signature must be a boolean")
        for name in (self.group, self.source_engine, self.source_id):
            _name(name)
        if (
            not isinstance(self.targets, tuple)
            or not self.targets
            or any(not isinstance(target, tuple) or len(target) != 2 for target in self.targets)
        ):
            raise MigrationRefused("verification targets must be nonempty engine/id pairs")
        for engine, identity in self.targets:
            _name(engine)
            _name(identity)
            if identity == self.source_id:
                raise MigrationRefused("verification source cannot also be a target")
        if len({identity for _, identity in self.targets}) != len(self.targets):
            raise MigrationRefused("verification target ids must be unique")
        if self.targets != tuple(sorted(self.targets)):
            raise MigrationRefused("verification targets must be sorted by engine and id")
        aware_time(self.requested_at)

    def as_record(self) -> dict[str, Any]:
        return {
            "protocol": REQUEST_PROTOCOL,
            "request_id": self.request_id,
            "project_id": self.project_id,
            "model_version": self.model_version,
            "map_version": self.map_version,
            "map_fingerprint": self.map_fingerprint,
            "group": self.group,
            "source": {"engine": self.source_engine, "id": self.source_id},
            "targets": [{"engine": engine, "id": identity} for engine, identity in self.targets],
            "requested_at": self.requested_at,
            "requires_signature": self.requires_signature,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> VerificationRequest:
        fields = {
            "protocol",
            "request_id",
            "project_id",
            "model_version",
            "map_version",
            "map_fingerprint",
            "group",
            "source",
            "targets",
            "requested_at",
            "requires_signature",
        }
        if not isinstance(record, Mapping) or set(record) != fields:
            raise MigrationRefused("verification request has missing or unknown fields")
        if type(record["protocol"]) not in (int, float) or record["protocol"] != REQUEST_PROTOCOL:
            raise MigrationRefused("unsupported verification request protocol")
        source, targets = record["source"], record["targets"]
        if not isinstance(source, Mapping) or set(source) != {"engine", "id"}:
            raise MigrationRefused("verification source must contain exactly engine and id")
        if not isinstance(targets, list) or any(
            not isinstance(target, Mapping) or set(target) != {"engine", "id"} for target in targets
        ):
            raise MigrationRefused("verification targets must contain exactly engine and id")
        return cls(
            request_id=record["request_id"],
            project_id=record["project_id"],
            model_version=record["model_version"],
            map_version=_integer(record["map_version"], "map_version"),
            map_fingerprint=record["map_fingerprint"],
            group=record["group"],
            source_engine=source["engine"],
            source_id=source["id"],
            targets=tuple((target["engine"], target["id"]) for target in targets),
            requested_at=record["requested_at"],
            requires_signature=record["requires_signature"],
        )

    def check_session(self, placement: PlacementMap, *, project_id: str | None, group: str) -> None:
        if project_id != self.project_id:
            raise MigrationRefused(
                "verification request names another project, or this session has no project_id. "
                "Configure the project id from the client's enrollment, not from the request."
            )
        if group != self.group:
            raise MigrationRefused("verification request names another group")
        expected = verification_request(
            placement,
            group=group,
            project_id=project_id,
            request_id=self.request_id,
            requested_at=self.requested_at,
        )
        if self != expected:
            raise MigrationRefused(
                "verification request does not match this session's model, map, source or targets; "
                "no comparison was started"
            )

    def check_time(self, at: str) -> None:
        if aware_time(at) < aware_time(self.requested_at):
            raise MigrationRefused(
                "verification predates its request; check the verifier's clock and run the "
                "comparison for the current request"
            )


def verification_request(
    placement: PlacementMap,
    *,
    group: str,
    project_id: str,
    request_id: str,
    requested_at: str,
) -> VerificationRequest:
    if placement.fingerprint is None:
        raise MigrationRefused("verification needs a loaded, canonically encodable placement map")
    spot = placement.placement_of(group)
    return VerificationRequest(
        request_id=request_id,
        project_id=project_id,
        model_version=placement.model_version,
        map_version=placement.map_version,
        map_fingerprint=placement.fingerprint,
        group=group,
        source_engine=spot.source.engine,
        source_id=spot.source.id,
        targets=tuple(sorted((target.engine, target.id) for target in spot.also_write)),
        requested_at=requested_at,
        requires_signature=placement.signed,
    )


def check_project_id(value: str | None) -> None:
    if value is not None:
        _hex(value, 32, "project_id")


def _integer(value: Any, field: str) -> int:
    # JSON Schema integers include an integral JSON number such as 1.0. JavaScript has one
    # number type, so normalize that value here without ever truncating a fractional value.
    if (
        type(value) not in (int, float)
        or not 1 <= value <= 9_007_199_254_740_991
        or int(value) != value
    ):
        raise MigrationRefused(f"verification {field} must be a positive safe integer")
    return int(value)

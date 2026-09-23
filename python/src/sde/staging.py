"""Load the signed authorization to prepare one fresh copy while retaining its source."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .canonical import CanonicalError, canonical_bytes
from .cutover import _hex, _record, _signature
from .errors import MapError, MigrationRefused
from .generation import GENERATIONS_SINCE, MAX_EPOCH, check_map_project, json_numbers
from .groups import colocation_groups
from .layout import group_columns
from .model import LogicalModel
from .placement import PlacementMap, _verify_signature, load_map

STAGING_PROTOCOL = 1
"""A move: the fresh copy is prepared in another engine binding than the source."""
STAGING_RELAYOUT_PROTOCOL = 2
"""A relayout: the fresh copy is prepared in the source's own engine binding, under fresh table
names, so a new physical design takes over by the same staging and cutover as a move.

A protocol of its own rather than protocol 1 with a refusal lifted, because lifting a refusal is a
change of format: a packet a released library refuses would become one a newer library executes,
under the same number. Here an older operator refuses a relayout by its protocol, by name, and both
protocols stay strict - protocol 1 still refuses a copy in the source's binding, protocol 2 refuses
one anywhere else."""
_FIELDS = {
    "kind",
    "protocol",
    "stage_id",
    "project_id",
    "group",
    "current",
    "prepared",
    "signature",
}


def staging_table_name(stage_id: str, position: int) -> str:
    """Portable physical name: fresh stage id plus a one-based, code-point ordered entity index."""
    _hex(stage_id, 32, "stage_id")
    if type(position) is not int or not 1 <= position <= 999999:
        raise MigrationRefused("staging entity position must be an integer from 1 through 999999")
    return f"sde_m_{stage_id}_{position:06d}"


@dataclass(frozen=True)
class StagingPlan:
    stage_id: str
    project_id: str
    group: str
    current: PlacementMap
    prepared: PlacementMap
    verified_with: str | None
    fingerprint: str | None = field(default=None, init=False)
    _document: bytes = field(default=b"", init=False, repr=False)

    def _loaded(self) -> None:
        if self.fingerprint is None or not self._document:
            raise MigrationRefused("staging requires an immutable loaded authorization")

    def as_record(self) -> dict[str, Any]:
        self._loaded()
        value: dict[str, Any] = json.loads(self._document)
        return value

    def prepared_payload(self) -> bytes:
        return canonical_bytes(self.as_record()["prepared"])

    def check_current(self, current: PlacementMap) -> None:
        self._loaded()
        check_map_project(current, self.project_id)
        if not current.signed or current.fingerprint != self.current.fingerprint:
            raise MigrationRefused("staging authorization does not name the signed current map")


@dataclass(frozen=True, init=False)
class StagingReceipt:
    _payload: bytes

    def __init__(self, record: Mapping[str, Any]) -> None:
        object.__setattr__(
            self,
            "_payload",
            json.dumps(
                record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8"),
        )

    def as_record(self) -> dict[str, Any]:
        record: dict[str, Any] = json.loads(self._payload)
        return record


def _load(
    raw: Mapping[str, Any],
    model: LogicalModel,
    project_id: str,
    public_key: bytes | Mapping[str, bytes],
) -> StagingPlan:
    body = _record(json_numbers(deepcopy(raw)), "staging authorization")
    if set(body) != _FIELDS:
        raise MigrationRefused("staging authorization has missing or unknown fields")
    if (
        type(body["protocol"]) is not int
        or body["protocol"] not in (STAGING_PROTOCOL, STAGING_RELAYOUT_PROTOCOL)
        or body["kind"] != "sde-stage"
    ):
        raise MigrationRefused("unsupported staging authorization kind or protocol")
    protocol: int = body["protocol"]
    identity = _hex(body["stage_id"], 32, "stage_id")
    local = _hex(body["project_id"], 32, "project_id")
    if local != project_id:
        raise MigrationRefused("staging authorization belongs to another local project")
    group = body["group"]
    if not isinstance(group, str) or not group:
        raise MigrationRefused("staging group must be a nonempty string")
    _signature(body)
    verified = _verify_signature(body, public_key)
    maps = []
    for name in ("current", "prepared"):
        document = _record(body[name], name)
        _signature(document)
        for raw_group in _record(document.get("groups"), "groups").values():
            value = _record(raw_group, "group")
            copies = value.get("derived", [])
            if not isinstance(copies, list):
                raise MigrationRefused("staging derived copies must be an array")
            for raw_material in (value.get("source"), *copies):
                raw_layout = _record(
                    _record(raw_material, "materialization").get("layout"), "layout"
                )
                if raw_layout.get("auto"):
                    raise MigrationRefused("staging maps need explicit physical layouts")
        parsed = load_map(document, model=model, public_key=public_key, require_signature=True)
        # Contract 4 introduced the generations this protocol rests on; 5 adds physical design
        # and keeps them. Anything a newer library would read is refused by load_map already.
        if parsed.contract < GENERATIONS_SINCE:
            raise MigrationRefused(
                f"staging protocol {protocol} requires map contract {GENERATIONS_SINCE} or later"
            )
        check_map_project(parsed, project_id)
        for placement in parsed.groups.values():
            for material in placement.all():
                if not material.layout.tables or not material.layout.columns:
                    raise MigrationRefused("staging maps need explicit physical layouts")
        maps.append(parsed)
    current, prepared = maps
    # The prepared map may raise the contract, because the fresh copy is the one place a physical
    # design can first appear - a ClickHouse copy partitioned by month under a contract-4 current
    # map. It may not lower it: a lower number would tell an older library it may ignore keys that
    # the current map already relies on.
    if prepared.contract < current.contract:
        raise MigrationRefused("staging cannot lower the placement map contract")
    if current.map_version >= prepared.map_version:
        raise MigrationRefused("staging must allocate a newer prepared map")
    if group not in current.groups or set(current.groups) != set(prepared.groups):
        raise MigrationRefused("staging cannot add or remove colocation groups")
    old, new = current.groups[group], prepared.groups[group]
    if old.derived or old.also_write:
        raise MigrationRefused("staging begins with a source-only group")
    if len(new.derived) != 1 or new.also_write != new.derived:
        raise MigrationRefused("staging prepares exactly one maintained copy")
    epoch = old.write_epoch
    assert epoch is not None
    if epoch > MAX_EPOCH - 2 or new.write_epoch != epoch:
        raise MigrationRefused(
            "staging retains the source generation and needs two spare generations"
        )
    same = new.derived[0].engine == old.source.engine
    if protocol == STAGING_PROTOCOL and same:
        raise MigrationRefused("staging target must use another engine binding")
    if protocol == STAGING_RELAYOUT_PROTOCOL and not same:
        # The copy's tables are fresh stage names, and the map refuses a copy in the source's
        # engine that reuses a source table - so a relayout cannot be the source under a new name.
        raise MigrationRefused(
            "a relayout (staging protocol 2) prepares its copy in the source's own engine binding"
        )
    current_raw, prepared_raw = body["current"], body["prepared"]
    old_group, new_group = current_raw["groups"][group], prepared_raw["groups"][group]
    if set(old_group) != {"source", "write_epoch"} or set(new_group) != {
        "source",
        "write_epoch",
        "derived",
        "also_write",
    }:
        raise MigrationRefused("staging group shape is not source-only to one maintained copy")
    if canonical_bytes(old_group["source"]) != canonical_bytes(new_group["source"]):
        raise MigrationRefused("staging cannot change the existing source")
    stable = {
        key: value
        for key, value in current_raw.items()
        if key not in {"signature", "map_version", "groups", "contract"}
    }
    after = {
        key: value
        for key, value in prepared_raw.items()
        if key not in {"signature", "map_version", "groups", "contract"}
    }
    if canonical_bytes(stable) != canonical_bytes(after) or dict(current.routing) != dict(
        prepared.routing
    ):
        raise MigrationRefused("staging cannot change routing or other map attributes")
    for other in current.groups:
        if other != group and canonical_bytes(current_raw["groups"][other]) != canonical_bytes(
            prepared_raw["groups"][other]
        ):
            raise MigrationRefused("staging cannot change an unaffected group")
    members = next(value for value in colocation_groups(model) if value.name == group)
    expected = {
        entity: staging_table_name(identity, position)
        for position, entity in enumerate(sorted(members.members), start=1)
    }
    if dict(new.derived[0].layout.tables) != expected:
        raise MigrationRefused(
            "staging needs fresh physical names bound to its stage id and entity order"
        )
    columns = group_columns(model, members)
    for material in (old.source, new.derived[0]):
        if set(material.layout.tables) != set(columns) or set(material.layout.columns) != set(
            columns
        ):
            raise MigrationRefused("staging layouts must cover exactly the group's entities")
        if any(
            set(material.layout.columns[entity]) != set(fields)
            for entity, fields in columns.items()
        ):
            raise MigrationRefused("staging layout columns must match the logical group")
    used = {
        table
        for placed in current.groups.values()
        for material in placed.all()
        for table in material.layout.tables.values()
    }
    if used & set(expected.values()):
        raise MigrationRefused("staging cannot reuse a current physical name")
    plan = StagingPlan(identity, local, group, current, prepared, verified)
    object.__setattr__(plan, "_document", canonical_bytes(body))
    object.__setattr__(
        plan,
        "fingerprint",
        hashlib.sha256(
            canonical_bytes({key: value for key, value in body.items() if key != "signature"})
        ).hexdigest(),
    )
    return plan


def load_staging_plan(
    raw: Mapping[str, Any],
    *,
    model: LogicalModel,
    project_id: str,
    public_key: bytes | Mapping[str, bytes],
) -> StagingPlan:
    try:
        return _load(raw, model, project_id, public_key)
    except (MapError, CanonicalError, ValueError, TypeError, KeyError) as exc:
        raise MigrationRefused(f"staging authorization refused: {exc}") from exc

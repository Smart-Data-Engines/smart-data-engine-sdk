"""Load the signed authorization to advance one group's write generation, and nothing else.

A group's write generation is what every row written under a map carries, and what the engine's
own constraint admits. Advancing it by one, with the placement as it was, makes one fact true from
then on: a row that still carries an earlier generation has not changed since. That is what lets a
later cutover leave such rows to a comparison made before its pause, and read under the pause only
the rows written since (``docs/generation-advance.md``).

The packet names the map in force and the map that follows it, both signed. The second is the
first with a newer version and the group's generation raised by one: the same engines, tables,
copies, routes and every other group, byte for byte. Nothing in it moves a row, so there is no
verification request in it and no decision to take: executed, it has happened.
"""

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
from .model import LogicalModel
from .placement import PlacementMap, _verify_signature, load_map

__all__ = ["ADVANCE_PROTOCOL", "AdvancePlan", "load_advance_plan"]

ADVANCE_PROTOCOL = 1
_FIELDS = {
    "kind",
    "protocol",
    "advance_id",
    "project_id",
    "group",
    "pause_budget_ms",
    "before",
    "after",
    "signature",
}

ADVANCE = "advance"
"""The subject of every refusal of this packet. The helpers it shares with the cutover packet name
a cutover unless told otherwise."""

_NEEDS_LAYOUTS = "advance maps need explicit physical layouts"


@dataclass(frozen=True)
class AdvancePlan:
    advance_id: str
    project_id: str
    group: str
    before: PlacementMap
    after: PlacementMap
    pause_budget_ms: int
    verified_with: str | None
    fingerprint: str | None = field(default=None, init=False)
    _document: bytes = field(default=b"", init=False, repr=False)

    def _loaded(self) -> None:
        if self.fingerprint is None or not self._document:
            raise MigrationRefused("an advance requires an immutable loaded authorization")

    @property
    def source_epoch(self) -> int:
        """The generation the group's rows carry before the advance."""
        self._loaded()
        value = self.before.placement_of(self.group).write_epoch
        assert value is not None
        return value

    @property
    def next_epoch(self) -> int:
        """The generation every write of the group carries after it."""
        return self.source_epoch + 1

    def as_record(self) -> dict[str, Any]:
        self._loaded()
        value: dict[str, Any] = json.loads(self._document)
        return value

    def after_payload(self) -> bytes:
        """The signed map to publish, as its canonical bytes."""
        return canonical_bytes(self.as_record()["after"])

    def check_current(self, current: PlacementMap) -> None:
        self._loaded()
        check_map_project(current, self.project_id)
        if not current.signed or current.fingerprint != self.before.fingerprint:
            raise MigrationRefused("advance authorization does not name the signed current map")


def _load(
    raw: Mapping[str, Any],
    model: LogicalModel,
    project_id: str,
    public_key: bytes | Mapping[str, bytes],
) -> AdvancePlan:
    body = _record(json_numbers(deepcopy(raw)), "authorization", ADVANCE)
    if set(body) != _FIELDS:
        raise MigrationRefused("advance authorization has missing or unknown fields")
    if (
        type(body["protocol"]) is not int
        or body["protocol"] != ADVANCE_PROTOCOL
        or body["kind"] != "sde-advance"
    ):
        raise MigrationRefused("unsupported advance authorization kind or protocol")
    identity = _hex(body["advance_id"], 32, "advance_id", ADVANCE)
    local = _hex(body["project_id"], 32, "project_id", ADVANCE)
    if local != project_id:
        raise MigrationRefused("advance authorization belongs to another local project")
    group = body["group"]
    if not isinstance(group, str) or not group:
        raise MigrationRefused("advance group must be a nonempty string")
    budget = body["pause_budget_ms"]
    if type(budget) is not int or not 1 <= budget <= MAX_EPOCH:
        raise MigrationRefused("advance pause_budget_ms must be a positive safe integer")
    _signature(body, ADVANCE)
    verified = _verify_signature(body, public_key)
    maps = []
    for name in ("before", "after"):
        document = _record(body[name], name, ADVANCE)
        _signature(document, ADVANCE)
        # The operator fences the tables these maps name. A layout left to be derived would be
        # derived by whichever library loads the map, so nobody signed the tables it names.
        for raw_group in _record(document.get("groups"), "groups", ADVANCE).values():
            value = _record(raw_group, "group", ADVANCE)
            copies = value.get("derived", [])
            if not isinstance(copies, list):
                raise MigrationRefused("advance derived copies must be an array")
            for raw_material in (value.get("source"), *copies):
                raw_layout = _record(
                    _record(raw_material, "materialization", ADVANCE).get("layout"),
                    "layout",
                    ADVANCE,
                )
                if raw_layout.get("auto"):
                    raise MigrationRefused(_NEEDS_LAYOUTS)
        parsed = load_map(document, model=model, public_key=public_key, require_signature=True)
        if parsed.contract < GENERATIONS_SINCE:
            raise MigrationRefused(
                f"an advance requires map contract {GENERATIONS_SINCE} or later: it changes a "
                f"write generation, and an earlier map has none"
            )
        check_map_project(parsed, project_id)
        for placement in parsed.groups.values():
            for material in placement.all():
                if not material.layout.tables or not material.layout.columns:
                    raise MigrationRefused(_NEEDS_LAYOUTS)
        maps.append(parsed)
    before, after = maps
    before_raw, after_raw = body["before"], body["after"]
    if before.map_version >= after.map_version:
        raise MigrationRefused("an advance must allocate a newer map")
    if group not in before.groups:
        raise MigrationRefused(f"an advance names a group the map in force does not have: {group}")
    if set(before.groups) != set(after.groups):
        raise MigrationRefused("an advance cannot add or remove colocation groups")
    epoch = before.groups[group].write_epoch
    if epoch is None:
        raise MigrationRefused(
            f"group {group} carries no write generation, so there is none to advance: its engine "
            f"cannot fence writes"
        )
    if epoch >= MAX_EPOCH:
        raise MigrationRefused("an advance needs one available write generation")
    for other in before.groups:
        if other != group and canonical_bytes(before_raw["groups"][other]) != canonical_bytes(
            after_raw["groups"][other]
        ):
            raise MigrationRefused("an advance cannot change an unaffected group")
    old_group = _record(before_raw["groups"][group], "group", ADVANCE)
    new_group = _record(after_raw["groups"][group], "group", ADVANCE)
    if new_group.get("write_epoch") != epoch + 1:
        raise MigrationRefused("an advance raises the group's write generation by exactly one")
    placed = {key: value for key, value in old_group.items() if key != "write_epoch"}
    placed_after = {key: value for key, value in new_group.items() if key != "write_epoch"}
    if canonical_bytes(placed) != canonical_bytes(placed_after):
        raise MigrationRefused(
            "an advance cannot change where the group is placed or which copies it maintains"
        )
    stable = {
        key: value
        for key, value in before_raw.items()
        if key not in {"signature", "map_version", "groups"}
    }
    stable_after = {
        key: value
        for key, value in after_raw.items()
        if key not in {"signature", "map_version", "groups"}
    }
    if canonical_bytes(stable) != canonical_bytes(stable_after):
        raise MigrationRefused("an advance cannot change routing or other map attributes")
    plan = AdvancePlan(identity, local, group, before, after, budget, verified)
    object.__setattr__(plan, "_document", canonical_bytes(body))
    object.__setattr__(
        plan,
        "fingerprint",
        hashlib.sha256(
            canonical_bytes({key: value for key, value in body.items() if key != "signature"})
        ).hexdigest(),
    )
    return plan


def load_advance_plan(
    raw: Mapping[str, Any],
    *,
    model: LogicalModel,
    project_id: str,
    public_key: bytes | Mapping[str, bytes],
) -> AdvancePlan:
    """Verify and load a generation-advance packet, or refuse it whole.

    Signed by one of the caller's keys, for the caller's project and model, and holding two signed
    maps of which the second is the first but for its version and one group's generation.
    """
    try:
        return _load(raw, model, project_id, public_key)
    except (MapError, CanonicalError, ValueError, TypeError, KeyError) as exc:
        raise MigrationRefused(f"advance authorization refused: {exc}") from exc

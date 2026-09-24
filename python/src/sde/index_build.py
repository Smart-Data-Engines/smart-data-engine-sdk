"""Load the signed authorization to build indexes on the tables in force, in place.

A model's design that only adds indexes to a group's source used to run as a relayout: a fresh copy
with the index, then a cutover that copies and compares every row while the source's writes are
frozen. Measured on PostgreSQL 15.19 and ClickHouse 24.8.14.39 (24 September 2026), that pause is
linear in the table - 22.5 s at 300 000 rows - and a table of about 400 000 rows exceeds the 30 s
cutover budget and rolls back. Building the index on the live table moves no row and pauses no
write.

This authorization binds that build: the exact map in force and the next map, which differs from it
only by indexes added to one group's source. Nothing else may change, because nothing else can
change without a copy - and the tables, the write generation and every running process stay as they
are, which is what lets the build run without a barrier.
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
from .generation import GENERATIONS_SINCE, check_map_project, json_numbers
from .model import LogicalModel
from .placement import PlacementMap, _verify_signature, load_map

INDEX_PROTOCOL = 1
"""Indexes added to one group's source, built on the tables in force; no copy, no cutover."""

MAX_BUILD_BUDGET_MS = 86_400_000
"""A day. The budget bounds a build on a server that stopped answering; nothing is paused while it
runs, so it is not a pause budget and it is deliberately far longer than a cutover's."""

_FIELDS = {
    "kind",
    "protocol",
    "index_id",
    "project_id",
    "group",
    "current",
    "prepared",
    "build_budget_ms",
    "signature",
}


_SUBJECT = "index build"


def index_build_name(index_id: str, position: int) -> str:
    """The physical name of the ``position``-th index an in-place build adds (one-based)."""
    _hex(index_id, 32, "index_id", _SUBJECT)
    if type(position) is not int or not 1 <= position <= 999999:
        raise MigrationRefused("index build position must be an integer from 1 through 999999")
    return f"sde_i_{index_id}_{position:06d}"


@dataclass(frozen=True)
class IndexPlan:
    index_id: str
    project_id: str
    group: str
    current: PlacementMap
    prepared: PlacementMap
    build_budget_ms: int
    added: tuple[Mapping[str, Any], ...]
    """The new index definitions, in position order, exactly as the prepared map carries them."""
    verified_with: str | None
    fingerprint: str | None = field(default=None, init=False)
    _document: bytes = field(default=b"", init=False, repr=False)

    def _loaded(self) -> None:
        if self.fingerprint is None or not self._document:
            raise MigrationRefused("an index build requires an immutable loaded authorization")

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
            raise MigrationRefused("index build authorization does not name the signed current map")


@dataclass(frozen=True, init=False)
class IndexReceipt:
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


def _names(document: Mapping[str, Any]) -> set[str]:
    """Every table and index name any layout of a map document uses."""
    found: set[str] = set()
    for raw_group in document["groups"].values():
        for material in (raw_group["source"], *raw_group.get("derived", [])):
            layout = material["layout"]
            found.update(str(table) for table in layout.get("tables", {}).values())
            found.update(str(index["name"]) for index in layout.get("indexes", []) or [])
    return found


def _load(
    raw: Mapping[str, Any],
    model: LogicalModel,
    project_id: str,
    public_key: bytes | Mapping[str, bytes],
) -> IndexPlan:
    body = _record(json_numbers(deepcopy(raw)), "authorization", _SUBJECT)
    if set(body) != _FIELDS:
        raise MigrationRefused("index build authorization has missing or unknown fields")
    if (
        type(body["protocol"]) is not int
        or body["protocol"] != INDEX_PROTOCOL
        or body["kind"] != "sde-index"
    ):
        raise MigrationRefused("unsupported index build authorization kind or protocol")
    identity = _hex(body["index_id"], 32, "index_id", _SUBJECT)
    local = _hex(body["project_id"], 32, "project_id", _SUBJECT)
    if local != project_id:
        raise MigrationRefused("index build authorization belongs to another local project")
    group = body["group"]
    if not isinstance(group, str) or not group:
        raise MigrationRefused("index build group must be a nonempty string")
    budget = body["build_budget_ms"]
    if type(budget) is not int or not 1 <= budget <= MAX_BUILD_BUDGET_MS:
        raise MigrationRefused(
            f"index build budget must be an integer from 1 through {MAX_BUILD_BUDGET_MS} ms"
        )
    _signature(body, _SUBJECT)
    verified = _verify_signature(body, public_key)
    maps = []
    for name in ("current", "prepared"):
        document = _record(body[name], name, _SUBJECT)
        _signature(document, _SUBJECT)
        for raw_group in _record(document.get("groups"), "groups", _SUBJECT).values():
            value = _record(raw_group, "group", _SUBJECT)
            copies = value.get("derived", [])
            if not isinstance(copies, list):
                raise MigrationRefused("index build derived copies must be an array")
            for raw_material in (value.get("source"), *copies):
                raw_layout = _record(
                    _record(raw_material, "materialization", _SUBJECT).get("layout"),
                    "layout",
                    _SUBJECT,
                )
                if raw_layout.get("auto"):
                    raise MigrationRefused("index build maps need explicit physical layouts")
        parsed = load_map(document, model=model, public_key=public_key, require_signature=True)
        if parsed.contract < GENERATIONS_SINCE:
            raise MigrationRefused(
                f"index build protocol {INDEX_PROTOCOL} requires map contract "
                f"{GENERATIONS_SINCE} or later"
            )
        check_map_project(parsed, project_id)
        for placement in parsed.groups.values():
            for material in placement.all():
                if not material.layout.tables or not material.layout.columns:
                    raise MigrationRefused("index build maps need explicit physical layouts")
        maps.append(parsed)
    current, prepared = maps
    # The prepared map may raise the contract, because an index method first appears here - a
    # BRIN index under a contract-4 current map. It may not lower it: a lower number would tell an
    # older library it may ignore keys that the current map already relies on.
    if prepared.contract < current.contract:
        raise MigrationRefused("an index build cannot lower the placement map contract")
    if current.map_version >= prepared.map_version:
        raise MigrationRefused("an index build must allocate a newer prepared map")
    if group not in current.groups or set(current.groups) != set(prepared.groups):
        raise MigrationRefused("an index build cannot add or remove colocation groups")
    old, new = current.groups[group], prepared.groups[group]
    if old.derived or old.also_write or new.derived or new.also_write:
        raise MigrationRefused("an index build begins and ends with a source-only group")
    if new.write_epoch != old.write_epoch:
        # Nothing is fenced: the tables stay and every running process keeps writing to them.
        raise MigrationRefused("an index build keeps the source's write generation")
    current_raw, prepared_raw = body["current"], body["prepared"]
    old_group, new_group = current_raw["groups"][group], prepared_raw["groups"][group]
    if set(old_group) != {"source", "write_epoch"} or set(new_group) != {"source", "write_epoch"}:
        raise MigrationRefused("an index build group is a source and its write generation only")

    def without_indexes(material: Mapping[str, Any]) -> dict[str, Any]:
        layout = {key: value for key, value in material["layout"].items() if key != "indexes"}
        return {**material, "layout": layout}

    if canonical_bytes(without_indexes(old_group["source"])) != canonical_bytes(
        without_indexes(new_group["source"])
    ):
        raise MigrationRefused(
            "an index build changes nothing about the source but its indexes; a new key order, "
            "partition, table or engine is a relayout or a move"
        )
    kept = list(old_group["source"]["layout"].get("indexes", []) or [])
    after = list(new_group["source"]["layout"].get("indexes", []) or [])
    if len(after) <= len(kept) or canonical_bytes(after[: len(kept)]) != canonical_bytes(kept):
        raise MigrationRefused(
            "an index build keeps every index in force, in order, and adds at least one after them"
        )
    added = after[len(kept) :]
    for position, index in enumerate(added, start=1):
        if index.get("name") != index_build_name(identity, position):
            raise MigrationRefused(
                "new indexes need fresh names bound to the index build id and their position"
            )
    if _names(current_raw) & {str(index["name"]) for index in added}:
        raise MigrationRefused("an index build cannot reuse a name the current map uses")
    stable = {
        key: value
        for key, value in current_raw.items()
        if key not in {"signature", "map_version", "groups", "contract"}
    }
    changed = {
        key: value
        for key, value in prepared_raw.items()
        if key not in {"signature", "map_version", "groups", "contract"}
    }
    if canonical_bytes(stable) != canonical_bytes(changed) or dict(current.routing) != dict(
        prepared.routing
    ):
        raise MigrationRefused("an index build cannot change routing or other map attributes")
    for other in current.groups:
        if other != group and canonical_bytes(current_raw["groups"][other]) != canonical_bytes(
            prepared_raw["groups"][other]
        ):
            raise MigrationRefused("an index build cannot change an unaffected group")
    plan = IndexPlan(
        identity,
        local,
        group,
        current,
        prepared,
        budget,
        # From the loaded map, which freezes nested structures, not from the caller's dictionaries.
        tuple(new.source.layout.indexes[len(kept) :]),
        verified,
    )
    object.__setattr__(plan, "_document", canonical_bytes(body))
    object.__setattr__(
        plan,
        "fingerprint",
        hashlib.sha256(
            canonical_bytes({key: value for key, value in body.items() if key != "signature"})
        ).hexdigest(),
    )
    return plan


def load_index_plan(
    raw: Mapping[str, Any],
    *,
    model: LogicalModel,
    project_id: str,
    public_key: bytes | Mapping[str, bytes],
) -> IndexPlan:
    try:
        return _load(raw, model, project_id, public_key)
    except (MapError, CanonicalError, ValueError, TypeError, KeyError) as exc:
        raise MigrationRefused(f"index build authorization refused: {exc}") from exc

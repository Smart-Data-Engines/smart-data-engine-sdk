"""Load a signed local cutover authorization without touching an engine or adopting a map."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal

from .canonical import CanonicalError, canonical_bytes
from .errors import MapError, MigrationRefused
from .generation import GENERATIONS_SINCE, MAX_EPOCH, check_map_project, json_numbers
from .model import LogicalModel
from .placement import PlacementMap, _verify_signature, load_map
from .shapes import enumerate_shapes
from .verification import VerificationRequest

CUTOVER_PROTOCOL = 1
_FIELDS = {
    "kind",
    "protocol",
    "plan_id",
    "project_id",
    "group",
    "pause_budget_ms",
    "query_impact_digest",
    "verification",
    "before",
    "success",
    "abort",
    "signature",
}
Outcome = Literal["success", "abort"]


def _hex(value: Any, width: int, name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(f"[0-9a-f]{{{width}}}", value) is None:
        raise MigrationRefused(f"cutover {name} must be {width} lowercase hexadecimal digits")
    return value


def _positive(value: Any, name: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_EPOCH:
        raise MigrationRefused(f"cutover {name} must be a positive safe integer")
    return value


def _record(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MigrationRefused(f"cutover {name} must be an object")
    return value


def _signature(record: dict[str, Any]) -> None:
    signature = _record(record.get("signature"), "signature")
    if set(signature) not in ({"alg", "value"}, {"alg", "value", "key_id"}):
        raise MigrationRefused("cutover signatures have missing or unknown fields")
    value = signature.get("value")
    if signature.get("alg") != "ed25519" or not isinstance(value, str):
        raise MigrationRefused("cutover signatures must use ed25519 and canonical base64")
    try:
        decoded = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise MigrationRefused("cutover signatures must use ed25519 and canonical base64") from exc
    if len(decoded) != 64 or base64.b64encode(decoded).decode() != value:
        raise MigrationRefused("cutover signatures must use ed25519 and canonical base64")
    if "key_id" in signature and not isinstance(signature["key_id"], str):
        raise MigrationRefused("cutover signature key_id must be a string")


@dataclass(frozen=True)
class CutoverPlan:
    plan_id: str
    project_id: str
    group: str
    before: PlacementMap
    success: PlacementMap
    abort: PlacementMap
    verification: VerificationRequest
    pause_budget_ms: int
    query_impact_digest: str
    verified_with: str | None
    fingerprint: str | None = field(default=None, init=False)
    _document: bytes = field(default=b"", init=False, repr=False)

    def _loaded(self) -> None:
        if self.fingerprint is None or not self._document:
            raise MigrationRefused("cutover execution requires an immutable loaded plan")

    @property
    def source_epoch(self) -> int:
        self._loaded()
        value = self.before.placement_of(self.group).write_epoch
        assert value is not None
        return value

    @property
    def maintenance_epoch(self) -> int:
        return self.source_epoch + 1

    @property
    def activation_epoch(self) -> int:
        return self.source_epoch + 2

    def as_record(self) -> dict[str, Any]:
        self._loaded()
        result: dict[str, Any] = json.loads(self._document)
        return result

    def candidate_payload(self, outcome: Outcome) -> bytes:
        if outcome not in ("success", "abort"):
            raise MigrationRefused("cutover outcome must be success or abort")
        return canonical_bytes(self.as_record()[outcome])

    def check_current(self, current: PlacementMap) -> None:
        self._loaded()
        check_map_project(current, self.project_id)
        if not current.signed or current.fingerprint != self.before.fingerprint:
            raise MigrationRefused("cutover plan does not name the current placement map")


def _load(
    raw: Mapping[str, Any],
    *,
    model: LogicalModel,
    project_id: str,
    public_key: bytes | Mapping[str, bytes],
) -> CutoverPlan:
    body = _record(json_numbers(deepcopy(raw)), "plan")
    if set(body) != _FIELDS:
        raise MigrationRefused("cutover plan has missing or unknown fields")
    if type(body["protocol"]) is not int or body["protocol"] != CUTOVER_PROTOCOL:
        raise MigrationRefused("unsupported cutover plan protocol")
    if body["kind"] != "sde-cutover":
        raise MigrationRefused("unsupported cutover document kind")
    identity = _hex(body["plan_id"], 32, "plan_id")
    local = _hex(body["project_id"], 32, "project_id")
    if local != project_id:
        raise MigrationRefused("cutover plan belongs to another locally configured project")
    budget = _positive(body["pause_budget_ms"], "pause_budget_ms")
    approval = _hex(body["query_impact_digest"], 64, "query_impact_digest")
    group = body["group"]
    if not isinstance(group, str) or not group:
        raise MigrationRefused("cutover group must be a nonempty string")
    _signature(body)
    verified_with = _verify_signature(body, public_key)
    documents = {name: _record(body[name], name) for name in ("before", "success", "abort")}
    maps: dict[str, PlacementMap] = {}
    for name, document in documents.items():
        _signature(document)
        for raw_spot in _record(document.get("groups"), "groups").values():
            raw_spot = _record(raw_spot, "group")
            derived = raw_spot.get("derived", [])
            if not isinstance(derived, list):
                raise MigrationRefused("cutover derived copies must be an array")
            for raw_material in (raw_spot.get("source"), *derived):
                raw_material = _record(raw_material, "materialization")
                layout = _record(raw_material.get("layout"), "layout")
                if layout.get("auto"):
                    raise MigrationRefused("cutover maps require explicit physical layouts")
        parsed = load_map(document, model=model, public_key=public_key, require_signature=True)
        # Generations arrived in contract 4 and contract 5 keeps them. The three candidates share
        # every top-level attribute, contract included, which the comparison below enforces.
        if parsed.contract < GENERATIONS_SINCE:
            raise MigrationRefused(
                f"cutover protocol 1 requires placement map contract {GENERATIONS_SINCE} or later"
            )
        check_map_project(parsed, project_id)
        _positive(parsed.map_version, "map_version")
        maps[name] = parsed
    before, success, abort = (maps[name] for name in ("before", "success", "abort"))
    if not before.map_version < success.map_version < abort.map_version:
        raise MigrationRefused("cutover versions must increase from before to success to abort")
    request = VerificationRequest.from_record(body["verification"])
    if not request.requires_signature:
        raise MigrationRefused("cutover verification must require the signed before map")
    request.check_session(before, project_id=project_id, group=group)
    spot = before.placement_of(group)
    if len(spot.derived) != 1 or spot.also_write != spot.derived:
        raise MigrationRefused("cutover requires exactly one derived copy maintained by fan-out")
    source, target = spot.source, spot.derived[0]
    if source.engine == target.engine:
        raise MigrationRefused("cutover source and target must use different engine bindings")
    epoch = spot.write_epoch
    assert epoch is not None
    if epoch > MAX_EPOCH - 2:
        raise MigrationRefused("cutover needs two available write generations")
    affected = {shape.id for shape in enumerate_shapes(model) if shape.group == group}
    if any(before.routing.get(shape, source.id) != source.id for shape in affected):
        raise MigrationRefused("cutover before-map reads must still use the source")
    before_raw = documents["before"]
    expected_routes = {
        shape: value for shape, value in before.routing.items() if shape not in affected
    }
    stable = {
        key: value
        for key, value in before_raw.items()
        if key not in {"signature", "map_version", "groups", "routing"}
    }
    for name, material, terminal_epoch in (
        ("success", target, epoch + 2),
        ("abort", source, epoch + 1),
    ):
        document, parsed = documents[name], maps[name]
        unchanged = {
            key: value
            for key, value in document.items()
            if key not in {"signature", "map_version", "groups", "routing"}
        }
        if canonical_bytes(unchanged) != canonical_bytes(stable) or set(parsed.groups) != set(
            before.groups
        ):
            raise MigrationRefused("cutover cannot change other map attributes or groups")
        if dict(parsed.routing) != expected_routes:
            raise MigrationRefused("cutover terminal routing must preserve the unaffected groups")
        for other in before.groups:
            if other != group and canonical_bytes(document["groups"][other]) != canonical_bytes(
                before_raw["groups"][other]
            ):
                raise MigrationRefused("cutover cannot change an unaffected group")
        terminal = document["groups"][group]
        if set(terminal) != {"source", "write_epoch"} or terminal["write_epoch"] != terminal_epoch:
            raise MigrationRefused(
                "cutover terminal group must contain only its source and decision generation"
            )
        candidate = terminal["source"]
        if not isinstance(candidate.get("id"), str) or not candidate["id"]:
            raise MigrationRefused("cutover terminal source must have a nonempty string id")
        original = (
            before_raw["groups"][group]["source"]
            if name == "abort"
            else before_raw["groups"][group]["derived"][0]
        )
        expected = {
            key: value for key, value in original.items() if key not in {"id", "lag_budget_ms"}
        }
        actual = {key: value for key, value in candidate.items() if key != "id"}
        if (
            canonical_bytes(actual) != canonical_bytes(expected)
            or parsed.groups[group].source.engine != material.engine
        ):
            raise MigrationRefused(
                "cutover terminal map changed the authorized physical materialization"
            )
    plan = CutoverPlan(
        identity, local, group, before, success, abort, request, budget, approval, verified_with
    )
    document_bytes = canonical_bytes(body)
    unsigned = {key: value for key, value in body.items() if key != "signature"}
    object.__setattr__(plan, "_document", document_bytes)
    object.__setattr__(plan, "fingerprint", hashlib.sha256(canonical_bytes(unsigned)).hexdigest())
    return plan


def load_cutover_plan(
    raw: Mapping[str, Any],
    *,
    model: LogicalModel,
    project_id: str,
    public_key: bytes | Mapping[str, bytes],
) -> CutoverPlan:
    """Verify exact authorization and candidates. No I/O, reservation, activation or watermark."""
    try:
        return _load(raw, model=model, project_id=project_id, public_key=public_key)
    except (MapError, CanonicalError) as exc:
        raise MigrationRefused(f"cutover document refused: {exc}") from exc

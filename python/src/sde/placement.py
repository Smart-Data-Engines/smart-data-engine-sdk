"""The placement map: where every group lives, and where every operation goes.

The map is the only instruction this library takes from outside. It says which engine holds each
group, what the physical layout there is, and - optionally - which materialisation each operation
shape should be routed to. Because it decides where data is written, it is the one input that
refuses rather than degrades: a bad signature, a mismatched model version or an unknown contract
version all stop the library from starting.

Two things about signing are worth stating plainly, because open-sourcing the library changes how
they read.

Publishing this code does not weaken the map. The public key lives here, the private key lives in
the control plane, and a reader seeing that maps are verified is reassured rather than informed of a
weakness. A signature is not a secret.

And a map with no signature at all is a *valid* map. That is the no-account mode: write a map by
hand, point the library at it, and everything works with no key, no account and no network. It is a
documented, supported way to use this library rather than a gap - which is also the honest answer to
anyone asking what happens if they stop paying us. What is refused is the middle case: a map that
carries a signature, thereby claiming to come from us, when we have no key to check that claim
against.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .canonical import canonical_bytes
from .errors import MapError
from .logging import log
from .model import CONTRACT, LogicalModel

__all__ = [
    "GroupPlacement",
    "Materialization",
    "PhysicalLayout",
    "PlacementMap",
    "load_map",
]


@dataclass(frozen=True)
class PhysicalLayout:
    """What the group looks like inside one engine.

    This is ours to choose and ours to change, which is the entire reason the client's code never
    names a table. Everything here is derived by the planner and simply obeyed by the library.
    """

    tables: Mapping[str, str]
    columns: Mapping[str, Mapping[str, str]]
    indexes: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    partition_by: Mapping[str, str] = field(default_factory=dict)

    def table_for(self, entity: str) -> str:
        try:
            return self.tables[entity]
        except KeyError:
            raise MapError(
                f"the layout has no table for {entity!r}. The map claims to place a group that "
                "contains this entity, so this is a defect in the map rather than something the "
                "library can work around."
            ) from None


@dataclass(frozen=True)
class Materialization:
    """One physical copy of a group in one engine."""

    id: str
    engine: str
    layout: PhysicalLayout
    lag_budget_ms: int | None = None

    @property
    def is_source(self) -> bool:
        return self.lag_budget_ms is None


@dataclass(frozen=True)
class GroupPlacement:
    group: str
    source: Materialization
    derived: tuple[Materialization, ...] = ()

    def all(self) -> tuple[Materialization, ...]:
        return (self.source, *self.derived)

    def by_id(self, mat_id: str) -> Materialization:
        for mat in self.all():
            if mat.id == mat_id:
                return mat
        raise MapError(
            f"group {self.group!r} has no materialisation {mat_id!r}, but the routing table points "
            "at it. The map is internally inconsistent."
        )


@dataclass(frozen=True)
class PlacementMap:
    contract: int
    model_version: str
    map_version: int
    groups: Mapping[str, GroupPlacement]
    routing: Mapping[str, str]
    signed: bool

    def placement_of(self, group: str) -> GroupPlacement:
        try:
            return self.groups[group]
        except KeyError:
            raise MapError(
                f"no placement for group {group!r}. Every group in the model needs one: a group "
                "with "
                "nowhere to live is not a slow path, it is an unanswerable operation."
            ) from None


_AUTO = object()


def _layout(raw: Mapping[str, Any], where: str) -> PhysicalLayout | object:
    """Parse a layout, or report that it asked to be derived.

    ``{"auto": true}`` is what makes a hand-written map a few lines rather than a full schema
    written out by hand. Without it the no-account mode from requirement 12.5 would be technically
    true and practically unusable, which is the same as not having it.
    """
    if not isinstance(raw, dict):
        raise MapError(f"{where}: layout must be an object")
    if raw.get("auto") is True:
        if len(raw) != 1:
            raise MapError(
                f"{where}: a layout is either auto or explicit, not both. Two sources of truth for "
                "a "
                "schema is how a column ends up existing in one place and not the other."
            )
        return _AUTO
    tables = raw.get("tables")
    if not isinstance(tables, dict) or not tables:
        raise MapError(
            f"{where}: layout needs a non-empty 'tables' mapping entity -> table name, "
            'or {"auto": true} to have one derived from the model'
        )
    return PhysicalLayout(
        tables=dict(tables),
        columns={k: dict(v) for k, v in (raw.get("columns") or {}).items()},
        indexes=tuple(raw.get("indexes") or ()),
        partition_by=dict(raw.get("partition_by") or {}),
    )


def _materialization(raw: Mapping[str, Any], where: str, *, source: bool) -> Materialization:
    for required in ("id", "engine", "layout"):
        if required not in raw:
            raise MapError(f"{where}: materialisation is missing {required!r}")
    lag = raw.get("lag_budget_ms")
    if source and lag is not None:
        raise MapError(
            f"{where}: the source materialisation cannot have a lag budget. The source is where "
            "writes land, so it is by definition not behind anything."
        )
    if not source and lag is None:
        raise MapError(
            f"{where}: a derived materialisation needs lag_budget_ms. Without it nobody - not the "
            "client, not the monitoring - can tell whether it is healthy or hours behind."
        )
    layout = _layout(raw["layout"], where)
    return Materialization(
        id=str(raw["id"]),
        engine=str(raw["engine"]),
        layout=layout,  # type: ignore[arg-type]
        lag_budget_ms=None if lag is None else int(lag),
    )


def _verify_signature(raw: Mapping[str, Any], public_key: bytes) -> None:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover - depends on the install extra
        raise MapError(
            "this map is signed, but signature verification needs the 'signed' extra: "
            "pip install 'sde[signed]'. The base install stays dependency-free on purpose, because "
            "a library that goes into someone's application should not drag in cryptography unless "
            "it is actually verifying something."
        ) from exc

    signature = raw["signature"]
    if not isinstance(signature, dict) or signature.get("alg") != "ed25519":
        raise MapError("only ed25519 signatures are understood")
    try:
        value = base64.b64decode(signature["value"], validate=True)
    except Exception as exc:
        raise MapError("the signature is not valid base64") from exc

    payload = canonical_bytes({k: v for k, v in raw.items() if k != "signature"})
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(value, payload)
    except InvalidSignature as exc:
        raise MapError(
            "the map's signature does not verify. This is refused rather than warned about: the "
            "map "
            "decides where your data is written."
        ) from exc


def load_map(
    raw: Mapping[str, Any],
    *,
    model: LogicalModel | None = None,
    public_key: bytes | None = None,
    require_signature: bool = False,
) -> PlacementMap:
    """Parse and validate a placement map.

    ``model`` is optional only so that the conformance vectors can exercise parsing on its own; in
    an application it is always passed, because a map for a different model version has to be
    refused rather than half-applied.
    """
    if not isinstance(raw, dict):
        raise MapError("a placement map is an object")

    contract = raw.get("contract")
    if contract != CONTRACT:
        raise MapError(
            f"this map declares format contract {contract!r} and this library implements "
            "{CONTRACT}. "
            "Refusing rather than guessing: the difference between two contract versions is "
            "exactly "
            "the kind of thing that would otherwise be interpreted as a missing field meaning zero."
        )

    model_version = raw.get("model_version")
    if not isinstance(model_version, str) or not model_version:
        raise MapError("the map does not say which model version it is for")
    if model is not None and model.version != model_version:
        raise MapError(
            f"this map is for model version {model_version} and your declared model is "
            f"{model.version}. Something changed in your entities; ask for a new map rather than "
            "running against this one, because the difference cannot be guessed."
        )

    signature_present = "signature" in raw and raw["signature"] is not None
    if require_signature and not signature_present:
        raise MapError("a signature was required and this map has none")
    if signature_present:
        if public_key is None:
            raise MapError(
                "this map is signed, which is a claim that it came from us, and no public key was "
                "provided to check that claim. Either pass the key, or use an unsigned map - an "
                "unsigned map is a supported mode, an unverifiable claim is not."
            )
        _verify_signature(raw, public_key)
    else:
        log(
            "sde.map.unsigned",
            model_version=model_version,
            detail="running without an account; no telemetry will be sent",
        )

    groups_raw = raw.get("groups")
    if not isinstance(groups_raw, dict) or not groups_raw:
        raise MapError("the map places no groups")

    groups: dict[str, GroupPlacement] = {}
    for name, body in groups_raw.items():
        where = f"group {name!r}"
        if not isinstance(body, dict) or "source" not in body:
            raise MapError(f"{where}: needs a 'source' materialisation")
        source = _materialization(body["source"], f"{where}.source", source=True)
        derived = tuple(
            _materialization(d, f"{where}.derived[{i}]", source=False)
            for i, d in enumerate(body.get("derived") or ())
        )
        ids = [m.id for m in (source, *derived)]
        if len(set(ids)) != len(ids):
            raise MapError(f"{where}: two materialisations share an id")
        groups[name] = GroupPlacement(group=name, source=source, derived=derived)

    routing = raw.get("routing") or {}
    if not isinstance(routing, dict):
        raise MapError("'routing' must be a mapping from shape id to materialisation id")

    if model is not None:
        model_groups = {g.name: g for g in _model_groups(model)}
        missing = sorted(set(model_groups) - set(groups))
        if missing:
            raise MapError(
                f"the map does not place these groups: {missing}. Every group in the model needs a "
                "home before anything can run."
            )
        groups = {
            name: _resolve_auto(placement, model, model_groups[name])
            for name, placement in groups.items()
        }
    elif any(m.layout is _AUTO for p in groups.values() for m in p.all()):
        raise MapError(
            'a layout asked to be derived with {"auto": true}, but no model was supplied to derive '
            "it from. Pass model= to load_map()."
        )

    return PlacementMap(
        contract=contract,
        model_version=model_version,
        map_version=int(raw.get("map_version", 0)),
        groups=groups,
        routing={str(k): str(v) for k, v in routing.items()},
        signed=signature_present,
    )


def _resolve_auto(placement: GroupPlacement, model: LogicalModel, group: Any) -> GroupPlacement:
    """Fill in any layout that asked to be derived.

    Derivation lives in :mod:`sde.layout` and is the boring total function from a model to a schema
    that stores it. The interesting choices - indexes worth their cost, partitioning, a second
    materialisation - are the planner's and are not in this repository.
    """
    from .layout import default_layout

    def fill(mat: Materialization) -> Materialization:
        if mat.layout is not _AUTO:
            return mat
        return Materialization(
            id=mat.id,
            engine=mat.engine,
            layout=default_layout(model, group),
            lag_budget_ms=mat.lag_budget_ms,
        )

    return GroupPlacement(
        group=placement.group,
        source=fill(placement.source),
        derived=tuple(fill(m) for m in placement.derived),
    )


def _model_groups(model: LogicalModel) -> tuple[Any, ...]:
    # Imported late: groups imports model, and model must not import groups.
    from .groups import colocation_groups

    return colocation_groups(model)

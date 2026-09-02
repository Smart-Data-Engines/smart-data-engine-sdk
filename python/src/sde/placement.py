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


WATERMARK_TABLE = "sde_map_state"
"""The table this library keeps its own bookkeeping in - see :mod:`sde.watermark`.

Defined here rather than there because the reservation is a property of the map format, and because
``watermark.py`` imports this module: a layout naming this table is refused when the map is loaded.

A client entity called ``SdeMapState`` would derive this table name from the model, and the refusal
below catches that too - at map load, which for a map we issue means at issuance, since `issue()`
parses its own output with this parser. The message names the fix, which is to rename the entity.
"""

_AUTO = object()


def _layout(raw: Mapping[str, Any], where: str) -> PhysicalLayout | object:
    """Parse a layout, or report that it asked to be derived.

    ``{"auto": true}`` is what makes a hand-written map a few lines rather than a full schema
    written out by hand. Without it the no-account mode from requirement 12.5 would be technically
    true and practically unusable, which is the same as not having it.

    It derives a **PostgreSQL** layout, always, and that is a limit rather than a default worth
    changing quietly. A layout has no dialect in it and the map names an engine by *name*, not by
    dialect - deliberately, since reasoning from an engine's name is what this library refuses
    everywhere - so there is nothing here from which the right dialect could be known. A
    hand-written map for ClickHouse or for the orderbook engine therefore needs an explicit layout,
    and both of those engines refuse a PostgreSQL-derived one rather than applying it: ClickHouse
    because it carries indexes it has no B-tree for, the orderbook engine because the table is not
    the one name its storage has. Fails closed, in other words, but the message names the symptom.
    Making ``auto`` dialect-aware means a new key in a signed document, which is a loosening of the
    format and so a contract bump in every language at once - see section 11.
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
    reserved = sorted(
        entity for entity, table in tables.items() if str(table) == WATERMARK_TABLE
    )
    if reserved:
        raise MapError(
            f"{where}: {reserved} would be stored in a table called {WATERMARK_TABLE!r}, which "
            f"this library keeps its own bookkeeping in - the highest map version applied against "
            f"an engine, which is what stops an older map from being loaded over a newer one. A "
            f"client table under that name would be read as bookkeeping and written to as "
            f"bookkeeping. Rename the table; the name is yours to choose everywhere else."
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
            "pip install 'smart-data-engine[signed]'. The base install stays dependency-free on "
            "purpose, because "
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
            f"{CONTRACT}. Refusing rather than guessing: the difference between two contract "
            f"versions is exactly the kind of thing that would otherwise be interpreted as a "
            f"missing field meaning zero."
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
        # And the other direction. Checking only one of the two is how a map placing a group this
        # model does not have reached `model_groups[name]` below and came out as a bare KeyError -
        # a library exception with no explanation, on the path whose entire job is to explain.
        unknown = sorted(set(groups) - set(model_groups))
        if unknown:
            raise MapError(
                f"the map places groups this model does not have: {unknown}. The model version "
                "matched, so this is not a stale map: the two sides derived colocation groups "
                "differently, and a group nobody declared has no entities to hold."
            )
        groups = {
            name: _resolve_auto(placement, model, model_groups[name])
            for name, placement in groups.items()
        }
        for placement in groups.values():
            _refuse_shadowing(placement)
        _check_routing_targets(routing, groups, model)
    elif any(m.layout is _AUTO for p in groups.values() for m in p.all()):
        raise MapError(
            'a layout asked to be derived with {"auto": true}, but no model was supplied to derive '
            "it from. Pass model= to load_map()."
        )
    else:
        _check_routing_targets(routing, groups, None)

    return PlacementMap(
        contract=contract,
        model_version=model_version,
        map_version=int(raw.get("map_version", 0)),
        groups=groups,
        routing={str(k): str(v) for k, v in routing.items()},
        signed=signature_present,
    )


def _refuse_shadowing(placement: GroupPlacement) -> None:
    """Refuse a derived copy that is secretly the source.

    Two materialisations of one group in the same engine must not name the same tables. Found by a
    test rather than by thinking: with an auto layout on both, the source and a derived copy derive
    identical table names, so a copy placed in the same engine simply *is* the source. Reads would
    appear to work, the lag would always measure zero, and the second copy would exist only in the
    map. Refused rather than warned about, precisely because it looks like it works.

    Checked after auto layouts are resolved, since before that there are no table names to compare.
    """
    source_tables = set(placement.source.layout.tables.values())
    for candidate in placement.derived:
        if candidate.engine != placement.source.engine:
            continue
        shared = sorted(source_tables & set(candidate.layout.tables.values()))
        if shared:
            raise MapError(
                f"group {placement.group!r}: materialisation {candidate.id!r} is in the same "
                "engine "
                f"as the source and reuses its tables {shared}. That is not a copy of the group, "
                "it is the original with a second name in the map, so its lag would always read as "
                "zero and a read routed to it would silently be a read of the source."
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


def _model_shapes(model: LogicalModel) -> tuple[Any, ...]:
    # Late for the same reason: shapes imports groups, which imports model.
    from .shapes import enumerate_shapes

    return enumerate_shapes(model)


def _check_routing_targets(
    routing: Mapping[str, Any],
    groups: Mapping[str, GroupPlacement],
    model: LogicalModel | None,
) -> None:
    """Every routing entry must name a materialisation that exists, in the shape's own group.

    This used to be checked in ``GroupPlacement.by_id`` - that is, at the first read that routed
    through the broken entry. Deferring it there turns a mistake in a document we hand over into an
    error inside the client's request path, at a moment nobody can predict: only the shapes routing
    through that entry fail, so a staging run that never issues those operations is green and the
    map looks applied. Everything here is decidable at load, so it is decided at load.

    Two levels, because ``model`` is optional. Without a model: the target has to be an id declared
    somewhere in this map. With one: it has to be declared in the group the shape belongs to, which
    is the check that matters - ids are only unique *within* a group, so a target that exists in
    some other group would otherwise read the entity out of a copy that does not hold it.
    """
    declared = {name: {m.id for m in placement.all()} for name, placement in groups.items()}
    everywhere = {mat_id for ids in declared.values() for mat_id in ids}

    for shape_id, target in sorted(routing.items()):
        if not isinstance(target, str):
            raise MapError(
                f"the routing entry for shape {shape_id!r} is not a materialisation id. Routing "
                "maps a shape id to one id, and anything else is a table nobody can look up."
            )
        if target not in everywhere:
            raise MapError(
                f"the routing table sends shape {shape_id!r} to materialisation {target!r}, and no "
                f"group in this map declares one with that id. The map is internally inconsistent: "
                f"declared ids are {sorted(everywhere)}."
            )

    if model is None:
        return

    shapes = {shape.id: shape for shape in _model_shapes(model)}
    for shape_id, target in sorted(routing.items()):
        shape = shapes.get(shape_id)
        if shape is None:
            raise MapError(
                f"the routing table has an entry for shape {shape_id!r}, and this model does not "
                "produce that shape. The model version matched, so the two sides enumerated shapes "
                "differently - which is the divergence that puts one library's write in a table "
                "another library never looks at."
            )
        if target not in declared.get(shape.group, frozenset()):
            raise MapError(
                f"the routing table sends shape {shape_id!r} - which belongs to group "
                f"{shape.group!r} - to materialisation {target!r}, which that group does not "
                f"declare. Materialisation ids are unique only within a group, so this would read "
                f"{shape.entity} out of a copy that does not hold it."
            )

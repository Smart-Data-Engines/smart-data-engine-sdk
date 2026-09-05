"""Turning declarations into a canonical model, and the model into a version.

The canonical IR is the artefact every library has to agree on byte for byte. Its shape is described
in ``docs/format-contract.md`` and pinned by ``conformance/vectors/model/*``. Two rules from the
contract show up all over this file and are worth naming before you read it:

* Arrays are sorted wherever order carries no meaning - entities by name, fields by name, relations
  by ``(from, name, to)``. Nothing may depend on the order the client happened to declare things in,
  because that order is not part of their model.
* Where order *does* carry meaning, it is recorded as an explicit ``position`` inside each element
  rather than as a position in the array. Composite keys are the case that matters: ``(tenant, id)``
  and ``(id, tenant)`` are different keys, and a reader must not have to know that array order is
  significant here but not three lines above.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, get_args, get_origin, get_type_hints

from .canonical import canonical_bytes, digest16
from .entity import EntityDecl, Ref, registry
from .errors import DeclarationError
from .logging import log
from .types import resolve_type

__all__ = [
    "CONTRACT",
    "EntitySpec",
    "FieldSpec",
    "LogicalModel",
    "RelationSpec",
    "assemble",
    "build_model",
]

CONTRACT = 1
"""The **IR's** format version, and the one the conformance vectors are pinned to.

Separate from :data:`sde.placement.MAP_CONTRACT` since the placement map gained a key the IR did
not, and that separation is a correction rather than a convenience. One counter for two artefacts
means every artefact's version moves when any one of them changes - and because ``contract`` is
*inside* the IR, and ``model_version`` is a digest of the IR, bumping it for a change to the map
format would give every client a new model version, invalidate every issued map, and re-bless every
model vector. For a key in a different document.

The evidence that the split is right is the hand-written vector: ``model/001-single-entity`` was
typed out from the format contract to prove the document is implementable, and its digest is pinned
in CI. Adding ``also_write`` to the map does not move it, because nothing about the IR changed.
"""
"""Format contract version. Bumped when the IR shape or the encoding rules change."""


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str
    nullable: bool


@dataclass(frozen=True)
class RelationSpec:
    name: str
    source: str
    target: str


@dataclass(frozen=True)
class EntitySpec:
    name: str
    fields: tuple[FieldSpec, ...]
    key: tuple[str, ...]
    pii: tuple[str, ...]
    residency: str | None


@dataclass(frozen=True)
class LogicalModel:
    entities: tuple[EntitySpec, ...]
    relations: tuple[RelationSpec, ...]
    atomic: tuple[tuple[str, ...], ...]
    cost_ceiling: Mapping[str, str] | None
    ir: Mapping[str, Any]
    version: str

    def entity(self, name: str) -> EntitySpec:
        for spec in self.entities:
            if spec.name == name:
                return spec
        raise KeyError(name)

    @property
    def ir_bytes(self) -> bytes:
        return canonical_bytes(self.ir)


def _resolve_hints(decl: EntityDecl) -> dict[str, Any]:
    try:
        return get_type_hints(decl.cls, localns=dict(decl.localns), include_extras=True)
    except NameError as exc:
        raise DeclarationError(
            f"{decl.name} refers to a name that is not defined yet ({exc}). Entities may reference "
            "each other in any order, but every name has to exist by the time build_model() runs."
        ) from exc


def _split_fields(
    decl: EntityDecl, hints: Mapping[str, Any], known: set[str]
) -> tuple[list[FieldSpec], list[RelationSpec]]:
    fields: list[FieldSpec] = []
    relations: list[RelationSpec] = []
    for name, annotation in hints.items():
        if name.startswith("_"):
            continue
        if get_origin(annotation) is Ref:
            args = get_args(annotation)
            if len(args) != 1 or not isinstance(args[0], type):
                raise DeclarationError(
                    f"{decl.name}.{name} is a Ref without a single entity target"
                )
            target = args[0].__name__
            if target not in known:
                raise DeclarationError(
                    f"{decl.name}.{name} points at {target}, which is not a declared entity. Add "
                    "@sde.entity to it, or include it in the model you are building."
                )
            relations.append(RelationSpec(name=name, source=decl.name, target=target))
            continue
        neutral, nullable = resolve_type(annotation, field=name, entity=decl.name)
        fields.append(FieldSpec(name=name, type=neutral, nullable=nullable))
    return fields, relations


def _resolve_key(decl: EntityDecl, fields: list[FieldSpec]) -> tuple[str, ...]:
    names = {f.name for f in fields}
    if decl.key is not None:
        missing = [k for k in decl.key if k not in names]
        if missing:
            raise DeclarationError(
                f"{decl.name}.Meta.key names {missing}, which are not fields of {decl.name}. A "
                "relation cannot be part of a key: the key has to be storable in the entity itself."
            )
        if not decl.key:
            raise DeclarationError(f"{decl.name}.Meta.key is empty")
        return decl.key
    if "id" in names:
        return ("id",)
    raise DeclarationError(
        f"{decl.name} has no 'id' field and no Meta.key. Every entity needs a key: without one "
        "there "
        "is no way to address a row, no way to migrate it and no way to verify a migration moved "
        "it."
    )


def _normalise_atomic(
    decls: tuple[EntityDecl, ...], known: set[str]
) -> tuple[tuple[str, ...], ...]:
    """Turn pairwise ``atomic_with`` declarations into merged, sorted groups.

    ``atomic_with`` is symmetric even when written on one side only: if A must change atomically
    with B then B must change atomically with A, so a client declaring it once is enough and
    declaring it twice must not produce two different groups. Merging is transitive for the same
    reason - if A is atomic with B and B with C, all three have to commit together, and nothing else
    would be implementable on a single engine's transaction.
    """
    parent: dict[str, str] = {d.name: d.name for d in decls}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    touched: set[str] = set()
    for decl in decls:
        for other in decl.atomic_with:
            if other not in known:
                raise DeclarationError(
                    f"{decl.name}.Meta.atomic_with names {other!r}, which is not a declared entity"
                )
            if other == decl.name:
                raise DeclarationError(
                    f"{decl.name}.Meta.atomic_with names itself, which says nothing"
                )
            union(decl.name, other)
            touched.add(decl.name)
            touched.add(other)

    groups: dict[str, list[str]] = {}
    for name in sorted(touched):
        groups.setdefault(find(name), []).append(name)
    return tuple(sorted(tuple(sorted(members)) for members in groups.values()))


def build_model(
    *entities: type,
    cost_ceiling: Mapping[str, str] | None = None,
) -> LogicalModel:
    """Resolve declarations into a :class:`LogicalModel`.

    With no arguments, everything decorated with :func:`~sde.entity.entity` so far is used. Passing
    entities explicitly is what tests do, because a global registry and test isolation do not mix.

    ``cost_ceiling`` is ``{"amount": "500.00", "currency": "EUR"}`` - the amount is a *string* on
    purpose. It is money, so it is a decimal, and a decimal in the IR would have to be a float,
    which the canonical encoding refuses for good reason.
    """
    decls: tuple[EntityDecl, ...]
    if entities:
        collected: list[EntityDecl] = []
        for cls in entities:
            decl = getattr(cls, "__sde_decl__", None)
            if decl is None:
                raise DeclarationError(
                    f"{cls.__name__} is not an entity. Decorate it with @sde.entity."
                )
            collected.append(decl)
        decls = tuple(collected)
    else:
        decls = registry()

    if not decls:
        raise DeclarationError("no entities declared, so there is no model to build")

    known = {d.name for d in decls}

    specs: list[EntitySpec] = []
    relations: list[RelationSpec] = []
    for decl in decls:
        hints = _resolve_hints(decl)
        fields, rels = _split_fields(decl, hints, known)
        if not fields:
            raise DeclarationError(
                f"{decl.name} has no fields, only relations. An entity that stores nothing cannot "
                "be "
                "placed, and a relation-only entity is usually a join table that wants to be one."
            )
        bad_pii = [p for p in decl.pii if p not in {f.name for f in fields}]
        if bad_pii:
            raise DeclarationError(
                f"{decl.name}.Meta.pii names {bad_pii}, which are not fields of {decl.name}"
            )
        specs.append(
            EntitySpec(
                name=decl.name,
                fields=tuple(sorted(fields, key=lambda f: f.name)),
                key=_resolve_key(decl, fields),
                pii=tuple(sorted(decl.pii)),
                residency=decl.residency,
            )
        )
        relations.extend(rels)

    atomic = _normalise_atomic(decls, known)

    if cost_ceiling is not None:
        missing = {"amount", "currency"} - set(cost_ceiling)
        if missing:
            raise DeclarationError(
                f"cost_ceiling is missing {sorted(missing)}; it is "
                '{"amount": "500.00", "currency": "EUR"} with the amount as a string'
            )

    return assemble(
        entities=tuple(specs),
        relations=tuple(relations),
        atomic=atomic,
        cost_ceiling=cost_ceiling,
    )


def assemble(
    *,
    entities: tuple[EntitySpec, ...],
    relations: tuple[RelationSpec, ...],
    atomic: tuple[tuple[str, ...], ...],
    cost_ceiling: Mapping[str, str] | None,
) -> LogicalModel:
    """Build the IR and the version from already-resolved specs.

    Both entry points come through here: the decorator path in :func:`build_model`, and the neutral
    JSON path in :mod:`sde.testing.loader` that the conformance vectors use. That is not tidiness -
    if the vectors exercised a second implementation of this encoding, they would be verifying
    something no application ever runs, which is the most expensive kind of green test.
    """
    specs = tuple(sorted(entities, key=lambda s: s.name))
    rels = tuple(sorted(relations, key=lambda r: (r.source, r.name, r.target)))

    # Fields and pii are sorted **here**, by the name that ends up in the IR - not left in whatever
    # order the caller supplied. Both callers below happen to supply them sorted already, so for
    # years this was a no-op and the ordering rule lived in a convention rather than in the code.
    # Hashing broke the convention: it rebuilds an entity with digests for names while keeping the
    # original sequence, so the IR came out ordered by the *real* field names. Two consequences, one
    # of them a leak: the hashed IR encoded the alphabetical order of names it is supposed to hide,
    # and no library that had never seen those names could reproduce the bytes. TypeScript sorted,
    # Python did not, and the hashing vector is what made the disagreement visible.
    def ordered(spec: EntitySpec) -> EntitySpec:
        return replace(
            spec,
            fields=tuple(sorted(spec.fields, key=lambda f: f.name)),
            # `key` is deliberately not sorted: its order is positional and carries meaning, which
            # is why the IR writes an explicit index for it.
            pii=tuple(sorted(spec.pii)),
        )

    specs = tuple(ordered(spec) for spec in specs)

    ir: dict[str, Any] = {
        "contract": CONTRACT,
        "entities": [
            {
                "name": s.name,
                "fields": [
                    {"name": f.name, "type": f.type, "nullable": f.nullable} for f in s.fields
                ],
                # Explicit position: array order is not load-bearing anywhere else in the IR, so it
                # must not be here either.
                "key": [{"field": k, "position": i} for i, k in enumerate(s.key)],
                "pii": list(s.pii),
                "residency": s.residency,
            }
            for s in specs
        ],
        "relations": [
            {"name": r.name, "from": r.source, "to": r.target} for r in rels
        ],
        "atomic": [list(group) for group in atomic],
        "cost_ceiling": dict(cost_ceiling) if cost_ceiling is not None else None,
    }

    model = LogicalModel(
        entities=specs,
        relations=rels,
        atomic=atomic,
        cost_ceiling=dict(cost_ceiling) if cost_ceiling is not None else None,
        ir=ir,
        version=digest16(ir),
    )
    # Once per process. `model_version` is the identifier every other artefact is keyed on - a map
    # naming a different one is refused, and that refusal is the most common thing anybody will
    # ever ask us about - so the version this process computed has to be readable from the client's
    # own log rather than reconstructed from their source.
    log(
        "sde.model.built",
        model_version=model.version,
        entities=len(specs),
        groups=len(atomic),
    )
    return model

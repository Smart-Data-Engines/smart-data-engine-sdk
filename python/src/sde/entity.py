"""Declaring the logical model: entities, relations, and the four invariants.

The client declares entities and relations and nothing about storage. No table name, no column name,
no engine, no index. That absence is the product: because their code never names a table, we can
change the table, and move it, without touching their code.

What they *do* have to declare is the four things traffic cannot reveal. No amount of watching
queries tells you that two entities must change atomically, or that a column is personal data, or
that a row may not leave the EU. Those are stated once, here, next to the model they describe.

Annotations are resolved lazily, in :func:`~sde.model.build_model`, not when the decorator runs.
Client modules routinely use ``from __future__ import annotations``, which makes every annotation a
string, and forward references between entities are normal - ``Order`` referring to ``Payment``
declared below it. Resolving at decoration time would make declaration order significant, which is a
trap nobody expects from a declarative API.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from .errors import DeclarationError

__all__ = ["EntityDecl", "Ref", "clear_registry", "entity", "registry"]

T = TypeVar("T")


class Ref(Generic[T]):
    """A reference from one entity to another.

    ``user: Ref[User]`` declares a relation, not a field. It reaches the physical layout as a
    foreign key column, but the model does not say that and does not need to know it.

    A relation also has a second, larger effect: it joins the two entities into one colocation
    group, so they are placed in the same engine. That is what makes the group the unit of placement
    rather than the entity - see :mod:`sde.groups` for why that is a feature and not a limitation.
    """

    __slots__ = ()


@dataclass(frozen=True)
class EntityDecl:
    """What the decorator captured, before annotations are resolved."""

    name: str
    cls: type
    key: tuple[str, ...] | None
    pii: tuple[str, ...]
    residency: str | None
    atomic_with: tuple[str, ...]
    # Names visible where the class was declared. Annotations are resolved later, and
    # get_type_hints() looks them up in the class's *module* globals - which is wrong for an entity
    # declared inside a function, where the entity it references is a local. That is not just a test
    # artefact: declaring a model inside a factory function is a perfectly ordinary thing to do, and
    # without this it would fail with a NameError pointing at our internals.
    localns: Mapping[str, Any]


_REGISTRY: list[EntityDecl] = []


def registry() -> tuple[EntityDecl, ...]:
    """Everything declared so far, in declaration order."""
    return tuple(_REGISTRY)


def clear_registry() -> None:
    """Empty the registry. For tests, which need isolation between models."""
    _REGISTRY.clear()


def _read_meta(cls: type) -> dict[str, Any]:
    meta = getattr(cls, "Meta", None)
    if meta is None:
        return {}
    known = {"key", "pii", "residency", "atomic_with"}
    out: dict[str, Any] = {}
    for attr in dir(meta):
        if attr.startswith("_"):
            continue
        if attr not in known:
            raise DeclarationError(
                f"{cls.__name__}.Meta declares {attr!r}, which is not one of the four invariants "
                f"({', '.join(sorted(known - {'key'}))}) or 'key'. Everything else about storage "
                "is "
                "our decision, and a fifth knob here would be a change to what the product "
                "promises "
                "rather than a configuration option."
            )
        out[attr] = getattr(meta, attr)
    return out


def _as_names(value: Any, *, what: str, cls: type) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raise DeclarationError(
            f"{cls.__name__}.Meta.{what} is a single string. Use a list, even for one item, so "
            "that "
            "adding a second one later is not a change of shape."
        )
    names: list[str] = []
    for item in value:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, type):
            names.append(item.__name__)
        else:
            raise DeclarationError(
                f"{cls.__name__}.Meta.{what} contains {item!r}; expected an entity class or its "
                "name"
            )
    return tuple(names)


def entity(cls: type) -> type:
    """Declare a class as an entity.

    The class stays an ordinary class - this decorator records it and returns it unchanged, so
    dataclasses, Pydantic models and plain classes all work and the client keeps whatever
    constructor and validation they already had.
    """
    meta = _read_meta(cls)

    key = meta.get("key")
    if key is not None:
        if isinstance(key, str):
            raise DeclarationError(
                f"{cls.__name__}.Meta.key is a string. Use a list: a composite key is a list of "
                "fields, and a single-field key is a list of one, so the two cases have one shape."
            )
        key = tuple(str(k) for k in key)

    try:
        caller = sys._getframe(1)
        localns: Mapping[str, Any] = dict(caller.f_locals)
    except (AttributeError, ValueError):  # pragma: no cover - non-CPython
        # Losing this only costs the function-local case; module-level declarations resolve from
        # module globals in any interpreter.
        localns = {}

    decl = EntityDecl(
        name=cls.__name__,
        cls=cls,
        localns=localns,
        key=key,
        pii=_as_names(meta.get("pii"), what="pii", cls=cls),
        residency=meta.get("residency"),
        atomic_with=_as_names(meta.get("atomic_with"), what="atomic_with", cls=cls),
    )

    existing = [d for d in _REGISTRY if d.name == decl.name]
    if existing:
        raise DeclarationError(
            f"two entities are called {decl.name!r}. Entity names reach the canonical IR and the "
            "colocation graph, so they have to be unique within a model."
        )

    # Attached to the class rather than kept only in the registry, so that build_model() can accept
    # entity classes directly - which is what tests do, because a global registry and test isolation
    # do not mix.
    cls.__sde_decl__ = decl  # type: ignore[attr-defined]
    _REGISTRY.append(decl)
    return cls

"""Building a model from neutral JSON, so that conformance vectors can be shared.

Every library needs this. A vector cannot contain Python decorators or TypeScript classes, so the
declaration in a vector is plain JSON and each implementation needs a way to turn that JSON into
whatever its own model type is. It is a small amount of code and it is a requirement, not a
convenience: without it the vectors could not be common, and without common vectors four
implementations drift apart silently.

It deliberately does *not* re-implement the encoding. It produces the same specs the decorator path
produces and hands them to :func:`sde.model.assemble`. A loader that built its own IR would make the
vectors verify a code path no application ever executes, which is the most expensive kind of green
test - it looks like coverage and is the absence of it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..errors import DeclarationError
from ..model import (
    EntitySpec,
    FieldSpec,
    LogicalModel,
    RelationSpec,
    assemble,
    checked_cost_ceiling,
)
from ..types import NEUTRAL_TYPES

__all__ = ["model_from_neutral"]

_DECIMAL_PREFIX = "decimal("


def _check_type(neutral: str, where: str) -> str:
    if neutral in NEUTRAL_TYPES:
        return neutral

    # Anything beginning with "decimal" gets the decimal-specific message, including a bare
    # "decimal" with no parameters at all. A conformance vector caught this: the generic "not in the
    # vocabulary" message is true and useless, because the reader's actual mistake is that they left
    # off the precision, and telling them the word is unknown sends them looking for the right word.
    if neutral == "decimal" or neutral.startswith("decimal"):
        if neutral.startswith(_DECIMAL_PREFIX) and neutral.endswith(")"):
            body = neutral[len(_DECIMAL_PREFIX) : -1]
            parts = body.split(",")
            if len(parts) == 2 and all(p.strip().isdigit() for p in parts):
                digits, scale = (int(p) for p in parts)
                if body == f"{digits},{scale}" and digits >= 1 and 0 <= scale <= digits:
                    return neutral
        raise DeclarationError(
            f"{where}: {neutral!r} is not a well-formed decimal. The written form is "
            "decimal(digits,scale) - precision then scale, no spaces, both required. No spaces "
            "because whitespace inside a type name is exactly the sort of thing two libraries "
            "would "
            "disagree about, and both required because a decimal without precision is not a "
            "storable type in any engine we place data in."
        )

    raise DeclarationError(
        f"{where}: {neutral!r} is not in the neutral type vocabulary "
        f"({', '.join(sorted(NEUTRAL_TYPES))}, decimal(p,s))"
    )


def _check_shape(raw: Mapping[str, Any], name: str) -> None:
    """The shapes a person can plausibly hand this function, named rather than crashed on.

    The first one is the whole reason this exists. The IR and the neutral form are close enough to
    be confused - our own tests declared a model's IR to the control plane and got away with it for
    weeks, because nothing read the stored document back - and they differ exactly where a key is
    written. Handing the IR over used to reach a set membership test on a dict and raise
    ``TypeError: unhashable type: 'dict'`` from inside the encoder, which tells the reader nothing
    about the mistake they made. Vector ``errors/036``.
    """
    fields = raw.get("fields")
    if not isinstance(fields, list):
        raise DeclarationError(f"{name}: 'fields' is a list, and this one is {fields!r}")
    for field in fields:
        if not isinstance(field, Mapping) or not isinstance(field.get("name"), str):
            raise DeclarationError(f"{name}: a field is an object with a name, not {field!r}")
        if not isinstance(field.get("type"), str):
            raise DeclarationError(f"{name}.{field['name']}: a field needs a type")
        # Not `bool(...)`: TypeScript read this as `=== true`, so `1` and `"yes"` were nullable here
        # and required there - one declaration, two models. Found by the C++ library, which has to
        # give a value a type before it can read it.
        if "nullable" in field and not isinstance(field["nullable"], bool):
            raise DeclarationError(
                f"{name}.{field['name']}: 'nullable' is true or false, not {field['nullable']!r}"
            )

    key = raw.get("key")
    if key is not None and not isinstance(key, list):
        raise DeclarationError(f"{name}: 'key' is a list of field names, not {key!r}")
    for part in key or ():
        if isinstance(part, Mapping) and "field" in part and "position" in part:
            raise DeclarationError(
                f"{name}: 'key' holds {part!r}, which is the IR's key form rather than the neutral "
                "one. The IR records a position because array order is not load-bearing anywhere "
                "else in it; a declaration states a key as a list of field names, in order. If you "
                "meant to hand over a model you already built, sde.neutral_declaration() produces "
                "this document from it."
            )
        if not isinstance(part, str):
            raise DeclarationError(f"{name}: 'key' names fields as strings, not {part!r}")

    for plural in ("pii",):
        value = raw.get(plural)
        if value is not None and (
            not isinstance(value, list) or any(not isinstance(v, str) for v in value)
        ):
            raise DeclarationError(f"{name}: {plural!r} is a list of field names, not {value!r}")

    residency = raw.get("residency")
    if residency is not None and not isinstance(residency, str):
        raise DeclarationError(
            f"{name}: 'residency' is a jurisdiction's name or null, not {residency!r}"
        )


def _relations(data: Mapping[str, Any], names: set[str]) -> list[RelationSpec]:
    """``relations``, each one an object of three strings. A missing ``to`` used to be a bare
    ``KeyError`` from this loader - the failure section 7 names for a missing rule."""
    raw_relations = data.get("relations")
    if raw_relations is None:
        return []
    if not isinstance(raw_relations, list):
        raise DeclarationError(
            f"'relations' is a list of {{name, from, to}}, not {raw_relations!r}"
        )
    relations: list[RelationSpec] = []
    for raw in raw_relations:
        if not isinstance(raw, Mapping) or not all(
            isinstance(raw.get(key), str) for key in ("name", "from", "to")
        ):
            raise DeclarationError(
                f'a relation is {{"name", "from", "to"}}, each a string, not {raw!r}'
            )
        source, target = raw["from"], raw["to"]
        for side in (source, target):
            if side not in names:
                raise DeclarationError(f"relation {raw['name']!r} names unknown entity {side!r}")
        relations.append(RelationSpec(name=raw["name"], source=source, target=target))
    return relations


def _atomic(raw: Any, names: set[str]) -> tuple[tuple[str, ...], ...]:
    """Atomic groups as declared: names checked, members distinct, two at least, no overlap.

    Section 4 says the IR's groups are merged and transitive. This loader used to copy them as
    written, so two overlapping groups reached the IR unmerged - a different ``model_version`` from
    the same atomicity declared as one group, which is what `neutral_declaration` writes. Refused
    rather than merged here, so that no library has to guess how another one merged.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise DeclarationError(f"'atomic' is a list of groups of entity names, not {raw!r}")
    placed: set[str] = set()
    groups: list[tuple[str, ...]] = []
    for group in raw:
        if not isinstance(group, list) or not all(isinstance(member, str) for member in group):
            raise DeclarationError(f"an atomic group is a list of entity names, not {group!r}")
        members = tuple(sorted(group))
        unknown = [m for m in members if m not in names]
        if unknown:
            raise DeclarationError(f"atomic group names unknown entities {unknown}")
        if len(set(members)) != len(members) or len(members) < 2:
            raise DeclarationError(
                "an atomic group names two or more distinct entities; one that names fewer, or "
                "one twice, says nothing a placement could act on"
            )
        for member in members:
            if member in placed:
                raise DeclarationError(
                    f"atomic groups overlap on {member!r}. Atomicity is transitive, so "
                    "overlapping groups are one group: declare it once, with every member, as a "
                    "library's own neutral declaration does"
                )
            placed.add(member)
        groups.append(members)
    return tuple(sorted(groups))


def model_from_neutral(data: Mapping[str, Any]) -> LogicalModel:
    """Build a :class:`~sde.model.LogicalModel` from a vector's ``model.json``.

    The neutral form states keys as a plain list, because that is what a human writes. Turning it
    into the positioned form the IR uses is this library's job, which is the point: if the vector
    carried the positioned form we would be checking that we can copy JSON.

    The shape of the document is checked before anything is read out of it. Not defensiveness:
    this is the entry point a client's own tooling hits and the one a new implementation writes
    first, and a bare ``KeyError`` out of a dictionary lookup is the failure mode section 7 of the
    format contract already names for a missing rule - a library exception with no explanation, on
    the path whose whole job is to explain.
    """
    if not isinstance(data, Mapping):
        raise DeclarationError("a neutral model declaration is an object with an 'entities' list")
    raw_entities = data.get("entities")
    if not isinstance(raw_entities, list):
        raise DeclarationError("a neutral model declaration needs an 'entities' list")

    entities: list[EntitySpec] = []
    for raw in raw_entities:
        if not isinstance(raw, Mapping):
            raise DeclarationError(f"an entity is an object, and this one is {raw!r}")
        if not isinstance(raw.get("name"), str) or not raw["name"]:
            raise DeclarationError(f"an entity needs a name, and this one has {raw.get('name')!r}")
        name = raw["name"]
        _check_shape(raw, name)
        fields = tuple(
            FieldSpec(
                name=f["name"],
                type=_check_type(f["type"], f"{name}.{f['name']}"),
                nullable=f.get("nullable", False),
            )
            for f in raw.get("fields", ())
        )
        # No default for the key. This line used to read `raw.get("key") or ("id",)` and the
        # TypeScript port spelled the same thing with `??`, which differs on exactly one input:
        # `"key": []` invented a key here and stayed keyless there, so one declaration had two
        # model versions and no vector could see it. The rule is format-contract §4a now, and every
        # refusal about a declaration lives in `assemble`, where both front doors meet - this one
        # enforced a subset and the vectors therefore ran a weaker validator than any application.
        key = tuple(raw.get("key") or ())
        entities.append(
            EntitySpec(
                name=name,
                fields=tuple(sorted(fields, key=lambda f: f.name)),
                key=key,
                pii=tuple(sorted(raw.get("pii") or ())),
                residency=raw.get("residency"),
            )
        )

    names = {e.name for e in entities}
    relations = _relations(data, names)
    atomic = _atomic(data.get("atomic"), names)

    return assemble(
        entities=tuple(entities),
        relations=tuple(relations),
        atomic=atomic,
        cost_ceiling=checked_cost_ceiling(data.get("cost_ceiling")),
    )

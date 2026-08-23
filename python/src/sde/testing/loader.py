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
from ..model import EntitySpec, FieldSpec, LogicalModel, RelationSpec, assemble
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


def model_from_neutral(data: Mapping[str, Any]) -> LogicalModel:
    """Build a :class:`~sde.model.LogicalModel` from a vector's ``model.json``.

    The neutral form states keys as a plain list, because that is what a human writes. Turning it
    into the positioned form the IR uses is this library's job, which is the point: if the vector
    carried the positioned form we would be checking that we can copy JSON.
    """
    entities: list[EntitySpec] = []
    for raw in data.get("entities", ()):
        name = raw["name"]
        fields = tuple(
            FieldSpec(
                name=f["name"],
                type=_check_type(f["type"], f"{name}.{f['name']}"),
                nullable=bool(f.get("nullable", False)),
            )
            for f in raw.get("fields", ())
        )
        if not fields:
            raise DeclarationError(f"{name} has no fields")
        key = tuple(raw.get("key") or ("id",))
        known = {f.name for f in fields}
        missing = [k for k in key if k not in known]
        if missing:
            raise DeclarationError(f"{name}: key names {missing}, which are not fields")
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
    relations: list[RelationSpec] = []
    for raw in data.get("relations", ()):
        source, target = raw["from"], raw["to"]
        for side in (source, target):
            if side not in names:
                raise DeclarationError(f"relation {raw['name']!r} names unknown entity {side!r}")
        relations.append(RelationSpec(name=raw["name"], source=source, target=target))

    atomic_raw = data.get("atomic") or ()
    atomic = tuple(sorted(tuple(sorted(group)) for group in atomic_raw))
    for group in atomic:
        unknown = [m for m in group if m not in names]
        if unknown:
            raise DeclarationError(f"atomic group names unknown entities {unknown}")

    return assemble(
        entities=tuple(entities),
        relations=tuple(relations),
        atomic=atomic,
        cost_ceiling=data.get("cost_ceiling"),
    )

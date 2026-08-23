"""The neutral type vocabulary, and the mapping from Python's types onto it.

Why a vocabulary at all: ``Decimal`` in Python and ``BigDecimal`` in Java have to land on the same
bytes in the canonical IR, or the same model gets two versions and the control plane sees two
models. So no language's own type names ever reach the IR. Each library maps its host language onto
this closed set, and the mapping is part of the published contract rather than an implementation
detail.

The set:

    bool int32 int64 float32 float64 decimal(p,s) string bytes uuid date timestamp timestamptz json

``decimal`` is the only parameterised member, written ``decimal(12,2)`` - precision then scale, no
spaces. It is parameterised because a decimal without precision is not a storable type in any of the
engines we place data in, and asking the engine to pick would make the physical schema depend on
something the model did not say.

**Floats are in the vocabulary, and this needed a correction to the specification.** The first draft
of the format contract said "no floating point anywhere", conflating two different things. Canonical
*encoding* must contain no float literals, because their textual form differs between languages -
that rule stands and ``canonical.py`` enforces it. But a *field* may perfectly well be a float:
sensor readings are the obvious case, and IoT at scale is one of the segments this product is aimed
at. A field of type ``float64`` is recorded in the IR as the string ``"float64"``, so there is no
float literal involved and no conflict. Forbidding the type would have meant telling a client to
store temperatures as decimals, which is worse engineering than the rule was worth.

Two mappings are defaults rather than one-to-one, and both are documented here because a silent
default in a type mapping is the kind of thing that surfaces two years later as a timezone bug:

* ``int`` maps to ``int64``. Python's integers are unbounded, so any choice is a narrowing; 64 bits
  is what every target engine has a native type for. Use :data:`Int32` to say so explicitly.
* ``datetime`` maps to ``timestamptz``. A naive timestamp is a latent bug in a system that may move
  data between engines and regions, so the safe reading is the default and the narrow one has to be
  asked for by name with :data:`Timestamp`.

Anything with no mapping - a bare ``Decimal``, a custom class, ``complex`` - is a
:class:`~sde.errors.DeclarationError`. Guessing would produce a physical schema the model did not
ask for.
"""

from __future__ import annotations

import datetime as _dt
import decimal as _decimal
import types as _pytypes
import typing
import uuid as _uuid
from dataclasses import dataclass
from typing import Annotated, Any, Final, get_args, get_origin

from .errors import DeclarationError

__all__ = [
    "NEUTRAL_TYPES",
    "Float32",
    "Int32",
    "Json",
    "Timestamp",
    "precision",
    "resolve_type",
]

NEUTRAL_TYPES: Final[frozenset[str]] = frozenset(
    {
        "bool",
        "int32",
        "int64",
        "float32",
        "float64",
        "string",
        "bytes",
        "uuid",
        "date",
        "timestamp",
        "timestamptz",
        "json",
        # decimal is parameterised and validated by pattern, not by membership here.
    }
)


@dataclass(frozen=True)
class _Marker:
    """Base for the annotations that disambiguate a mapping."""

    kind: str


@dataclass(frozen=True)
class _Precision(_Marker):
    digits: int
    scale: int


def precision(digits: int, scale: int) -> _Precision:
    """Annotate a ``Decimal`` field with its precision and scale.

    ``total: Annotated[Decimal, precision(12, 2)]``

    Both are required. A decimal without them is not a storable type, and letting the engine choose
    would make the physical schema depend on something the model never stated.
    """
    if digits < 1 or scale < 0 or scale > digits:
        raise DeclarationError(
            f"precision({digits}, {scale}) is not a usable decimal: digits must be at least 1 and "
            "scale must be between 0 and digits"
        )
    return _Precision(kind="precision", digits=digits, scale=scale)


Int32 = Annotated[int, _Marker(kind="int32")]
"""A 32-bit integer, said explicitly. Bare ``int`` maps to ``int64``."""

Float32 = Annotated[float, _Marker(kind="float32")]
"""A single-precision float. Bare ``float`` maps to ``float64``."""

Timestamp = Annotated[_dt.datetime, _Marker(kind="naive")]
"""A timestamp without a zone. Bare ``datetime`` maps to ``timestamptz`` on purpose."""

Json = Annotated[object, _Marker(kind="json")]
"""An opaque JSON document. Use when the shape genuinely is not fixed, not to avoid declaring it."""

_SIMPLE: Final[dict[Any, str]] = {
    bool: "bool",
    int: "int64",
    float: "float64",
    str: "string",
    bytes: "bytes",
    _uuid.UUID: "uuid",
    _dt.date: "date",
    _dt.datetime: "timestamptz",
    dict: "json",
    list: "json",
}


def _markers(annotation: object) -> tuple[object, tuple[_Marker, ...]]:
    """Peel ``Annotated`` and ``Optional`` off an annotation.

    Returns the bare type plus any markers found. Nullability is handled by the caller, which needs
    to record it separately in the IR rather than as part of the type name.
    """
    markers: list[_Marker] = []
    current = annotation
    while get_origin(current) is Annotated:
        args = get_args(current)
        current = args[0]
        markers.extend(m for m in args[1:] if isinstance(m, _Marker))
    return current, tuple(markers)


def _is_optional(annotation: object) -> tuple[bool, object]:
    """Peel ``| None`` off, and refuse anything wider.

    ``str | None`` is a nullable string. ``int | str`` is not a field: it has no single physical
    representation, so there is no column we could create for it and no index we could put on it.
    Refusing here rather than falling through to the vocabulary check matters only for the error
    message, and the error message is the whole product for someone who mistyped an annotation.
    """
    origin = get_origin(annotation)
    if origin is typing.Union or origin is _pytypes.UnionType:
        args = get_args(annotation)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) != 1:
            raise DeclarationError(
                f"{annotation!r} is a union of several types. A field has one type; a union of two "
                "real types has no single physical representation, so it cannot be placed. Model "
                "it "
                "as separate nullable fields, or as json if the shape really is not fixed."
            )
        return len(non_none) != len(args), non_none[0]
    return False, annotation


def resolve_type(annotation: object, *, field: str, entity: str) -> tuple[str, bool]:
    """Map a Python annotation onto ``(neutral_type, nullable)``.

    Raises :class:`~sde.errors.DeclarationError` naming the field, because the reader has to fix
    their declaration and a traceback into this module tells them nothing.
    """
    nullable, inner = _is_optional(annotation)
    bare, markers = _markers(inner)
    # Optional may sit inside Annotated as well: Annotated[int | None, ...].
    nested_nullable, bare = _is_optional(bare)
    nullable = nullable or nested_nullable

    kinds = {m.kind for m in markers}

    if bare is _decimal.Decimal:
        found = [m for m in markers if isinstance(m, _Precision)]
        if not found:
            raise DeclarationError(
                f"{entity}.{field} is a Decimal without precision. Write "
                f"Annotated[Decimal, precision(digits, scale)] - a decimal without precision is "
                "not "
                "a storable type in any engine we place data in, and choosing for you would make "
                "the physical schema depend on something your model did not say."
            )
        p = found[0]
        return f"decimal({p.digits},{p.scale})", nullable

    if "json" in kinds:
        return "json", nullable
    if "int32" in kinds:
        return "int32", nullable
    if "float32" in kinds:
        return "float32", nullable
    if "naive" in kinds:
        if bare is not _dt.datetime:
            raise DeclarationError(
                f"{entity}.{field} is annotated as a naive timestamp but is not a datetime"
            )
        return "timestamp", nullable

    origin = get_origin(bare)
    if origin in (dict, list):
        return "json", nullable

    mapped = _SIMPLE.get(bare)
    if mapped is not None:
        return mapped, nullable

    raise DeclarationError(
        f"{entity}.{field} has type {bare!r}, which has no place in the neutral type vocabulary "
        f"({', '.join(sorted(NEUTRAL_TYPES))}, decimal(p,s)). Map it yourself - to a string, to "
        "json, to a decimal with stated precision - so that the choice is visible in the model "
        "instead of being guessed here."
    )

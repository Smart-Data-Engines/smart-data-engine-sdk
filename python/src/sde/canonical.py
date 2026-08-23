"""Canonical encoding: the one place where the cross-language contract lives.

Every SDE library has to produce byte-identical output from this. A definition amounting to
"whatever the Python implementation does" is not a definition, so the rules are spelled out here and
in ``docs/format-contract.md``, and the conformance vectors pin them.

The rules, in the order they are applied:

1. UTF-8, no byte order mark.
2. Object keys are NFC-normalised, then sorted by Unicode code point. Normalise first: sorting first
   would order ``é`` (U+00E9) and ``e`` + U+0301 differently while both normalise to the same key.
3. No insignificant whitespace. ``{"a":1,"b":[2,3]}`` and nothing else.
4. Every string is NFC-normalised.
5. Escaping is minimal and exhaustive: only ``"``, ``\\`` and C0 control characters are escaped,
   controls using the short forms where JSON defines them and ``\\u00XX`` otherwise. Everything else
   is emitted as raw UTF-8. This matters because JSON writers disagree: some escape all non-ASCII,
   some escape U+2028, some escape forward slashes. Any of those would change the hash.
6. Floating point values are rejected outright. Their textual form differs across languages and the
   difference is unfixable after the fact. Note the distinction that cost a spec revision: a *field*
   may have type ``float64``; the IR records the *name* of that type, which is a string. There is no
   float literal anywhere in the encoding.
7. Integers are emitted as their shortest decimal form, no leading ``+``, no exponent.

Ordering of arrays is not this module's business. Where order carries no meaning the caller sorts;
where it does, the caller records an explicit index in each element rather than relying on position.
"""

from __future__ import annotations

import hashlib
import unicodedata
from typing import Final

__all__ = ["CanonicalError", "canonical_bytes", "canonical_str", "digest16"]

# Short escape forms JSON defines. Everything else below 0x20 gets \u00XX.
_SHORT_ESCAPES: Final[dict[int, str]] = {
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
    0x22: '\\"',
    0x5C: "\\\\",
}


class CanonicalError(ValueError):
    """A value cannot be encoded canonically.

    Raised rather than coerced on purpose: silently accepting a float, a NaN or a non-string key
    would produce bytes that another implementation cannot reproduce, and the failure would surface
    much later as two versions of the same model.
    """


def _escape(text: str) -> str:
    normalised = unicodedata.normalize("NFC", text)
    out: list[str] = ['"']
    for char in normalised:
        code = ord(char)
        short = _SHORT_ESCAPES.get(code)
        if short is not None:
            out.append(short)
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _encode(value: object, path: str) -> str:
    # bool before int: bool is a subclass of int in Python and would otherwise encode as 1/0.
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, str):
        return _escape(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        raise CanonicalError(
            f"float at {path}: floating point is not representable in canonical form, "
            "because its textual form differs between languages. Use an integer, or a decimal "
            "string, or the name of a float type if you meant to describe a type."
        )
    if isinstance(value, (list, tuple)):
        items = [_encode(item, f"{path}[{i}]") for i, item in enumerate(value)]
        return "[" + ",".join(items) + "]"
    if isinstance(value, dict):
        pairs: list[tuple[str, object]] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalError(
                    f"non-string key {key!r} at {path}: object keys must be strings, because "
                    "key ordering is defined over Unicode code points"
                )
            pairs.append((unicodedata.normalize("NFC", key), item))
        # Sort after normalising, on the normalised key.
        pairs.sort(key=lambda pair: pair[0])
        seen: set[str] = set()
        parts: list[str] = []
        for key, item in pairs:
            if key in seen:
                raise CanonicalError(
                    f"duplicate key {key!r} at {path} after NFC normalisation: two keys that "
                    "differ "
                    "only in Unicode composition are the same key here"
                )
            seen.add(key)
            parts.append(_escape(key) + ":" + _encode(item, f"{path}.{key}"))
        return "{" + ",".join(parts) + "}"
    raise CanonicalError(
        f"{type(value).__name__} at {path} has no canonical form. The canonical encoding accepts "
        "only null, bool, int, str, list and dict; anything richer has to be reduced to those by "
        "the caller, so that the reduction is visible and testable."
    )


def canonical_str(value: object) -> str:
    """Canonical form as text. Prefer :func:`canonical_bytes` for hashing."""
    return _encode(value, "$")


def canonical_bytes(value: object) -> bytes:
    """Canonical form as UTF-8 bytes. This is what gets hashed and what vectors compare."""
    return canonical_str(value).encode("utf-8")


def digest16(value: object) -> str:
    """The identifier form used for ``model_version`` and ``shape.id``.

    Lowercase hex, first 8 bytes of SHA-256 over the canonical bytes. Sixteen characters is short
    enough to appear in logs and error messages without wrapping, and 64 bits of collision
    resistance is ample for the number of models and shapes one application declares.
    """
    return hashlib.sha256(canonical_bytes(value)).hexdigest()[:16]

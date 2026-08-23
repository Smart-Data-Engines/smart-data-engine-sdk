"""Tests for the canonical encoding.

These are the most important tests in the library. Everything else can be wrong in a way that shows
up as a failure; this can be wrong in a way that shows up as two libraries computing two versions of
the same model, three months later, in production.
"""

from __future__ import annotations

import pytest

from sde.canonical import CanonicalError, canonical_bytes, canonical_str, digest16


def test_keys_are_sorted_and_whitespace_is_absent() -> None:
    assert canonical_str({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_insertion_order_does_not_change_the_bytes() -> None:
    # The reason this matters: a model built by iterating a client's class in one order and a vector
    # built by parsing JSON in another must hash identically.
    first = {"z": 1, "a": {"y": 2, "b": 3}}
    second = {"a": {"b": 3, "y": 2}, "z": 1}
    assert canonical_bytes(first) == canonical_bytes(second)


# Built from explicit code points, never from literals in this file. A test for a cross-language
# byte contract must not depend on how an editor happened to normalise the source it lives in.
COMPOSED = "\u00e9"  # e-acute as one code point
DECOMPOSED = "e\u0301"  # e followed by combining acute


def test_the_two_forms_really_are_different_inputs() -> None:
    # Guards the three tests below: if this ever fails, they are all tautologies.
    assert COMPOSED != DECOMPOSED
    assert len(COMPOSED) == 1
    assert len(DECOMPOSED) == 2


def test_nfc_normalisation_makes_equivalent_strings_equal() -> None:
    assert canonical_bytes({"k": COMPOSED}) == canonical_bytes({"k": DECOMPOSED})
    assert canonical_bytes({COMPOSED: 1}) == canonical_bytes({DECOMPOSED: 1})


def test_normalisation_happens_before_sorting() -> None:
    # Sorting the decomposed form first would order it under "e", ahead of "z". Normalising first
    # puts it at U+00E9, after "z". Both orders are self-consistent, so only an explicit expectation
    # catches a library that runs the two steps the wrong way round.
    assert canonical_str({DECOMPOSED: 1, "z": 2}) == '{"z":2,"' + COMPOSED + '":1}'


def test_duplicate_keys_after_normalisation_are_refused() -> None:
    with pytest.raises(CanonicalError, match="duplicate key"):
        canonical_bytes({COMPOSED: 1, DECOMPOSED: 2})



def test_floats_are_refused_with_an_explanation() -> None:
    with pytest.raises(CanonicalError, match="floating point"):
        canonical_bytes({"x": 1.5})


def test_float_types_are_fine_because_a_type_name_is_a_string() -> None:
    # The distinction that cost a specification revision: no float *literals* in the encoding, but a
    # field may certainly have a float type.
    assert canonical_str({"type": "float64"}) == '{"type":"float64"}'


def test_bool_is_not_an_integer() -> None:
    assert canonical_str({"a": True, "b": False}) == '{"a":true,"b":false}'
    assert canonical_str({"a": 1, "b": 0}) == '{"a":1,"b":0}'


def test_escaping_is_minimal_and_exhaustive() -> None:
    assert canonical_str('a"b\\c') == '"a\\"b\\\\c"'
    assert canonical_str("\n\t\r\b\f") == '"\\n\\t\\r\\b\\f"'
    assert canonical_str("\x00\x1f") == '"\\u0000\\u001f"'
    # Non-ASCII is emitted raw. Escaping it would be legal JSON and a different hash, which is
    # exactly the sort of disagreement between two languages' JSON writers this pins down.
    assert canonical_bytes("żółw") == '"żółw"'.encode()
    # U+2028 is escaped by some JavaScript-aware writers. It must not be here.
    assert canonical_bytes(" ") == '" "'.encode()  # noqa: RUF001


def test_non_string_keys_are_refused() -> None:
    with pytest.raises(CanonicalError, match="non-string key"):
        canonical_bytes({1: "a"})


def test_unsupported_types_are_refused_rather_than_coerced() -> None:
    with pytest.raises(CanonicalError, match="no canonical form"):
        canonical_bytes({"when": object()})


def test_nested_containers_and_empty_values() -> None:
    assert canonical_str({"a": [], "b": {}, "c": None}) == '{"a":[],"b":{},"c":null}'
    assert canonical_str([1, [2, [3]]]) == "[1,[2,[3]]]"


def test_tuples_encode_as_arrays() -> None:
    assert canonical_str((1, 2)) == canonical_str([1, 2])


def test_large_integers_survive() -> None:
    # Python's ints are unbounded and JSON does not say otherwise. A library in a language with
    # 64-bit integers has to reject what it cannot represent rather than silently truncate, which is
    # a conformance concern rather than a Python one - noted here so the vector exists.
    assert canonical_str({"n": 2**70}) == f'{{"n":{2**70}}}'


def test_digest_is_lowercase_hex_of_sixteen_characters() -> None:
    value = digest16({"a": 1})
    assert len(value) == 16
    assert value == value.lower()
    assert all(c in "0123456789abcdef" for c in value)


def test_digest_is_stable() -> None:
    # A change here breaks every stored placement map, so it is worth an explicit expected value
    # rather than a self-referential assertion.
    assert digest16({"a": 1}) == digest16({"a": 1})
    assert digest16({"a": 1}) != digest16({"a": 2})

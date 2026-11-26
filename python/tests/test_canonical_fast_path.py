"""The canonical encoder's fast path for strings with nothing to escape writes the loop's bytes.

The loop over every character is the definition (format contract §1); the fast path only
recognises a string the loop would leave as it is. This holds the two equal for every code point to
U+2FFF alone and between other characters, for strings that change under NFC, and for the long and
mixed strings a journal holds - and the canonical vectors hold the bytes themselves.
"""

from __future__ import annotations

import unicodedata

from sde.canonical import _SHORT_ESCAPES, _escape, canonical_bytes


def loop(text: str) -> str:
    """The definition, character by character, as the encoder wrote every string before."""
    out = ['"']
    for char in unicodedata.normalize("NFC", text):
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


def test_every_code_point_to_u2fff_alone_and_between_letters() -> None:
    for code in range(0x3000):
        if 0xD800 <= code <= 0xDFFF:
            continue  # a lone surrogate is not a string a model holds; NFC refuses nothing here
        char = chr(code)
        for text in (char, f"ab{char}cd", f"{char}{char}"):
            assert _escape(text) == loop(text), hex(code)


def test_strings_that_change_under_nfc() -> None:
    for text in ("é", "Å", "Å", "ẛ̣", "zażółć gęślą jaźń", "ﬁ"):
        assert _escape(text) == loop(text), text


def test_long_and_mixed_strings() -> None:
    for text in ("a" * 4096, "a" * 4096 + "\n", '"' * 3, "\\" * 3, "tab\there", "\x7f\x80\xff",
                 "2026-11-19T10:00:00.123456Z", "{\"kind\": \"access\"}"):  # fmt: skip
        assert _escape(text) == loop(text), text


def test_the_bytes_of_a_document_are_unchanged() -> None:
    document = {"kind": "access", "payload": {"operator": "op", "note": 'say "hi"\n', "n": 3}}
    assert canonical_bytes(document) == (
        b'{"kind":"access","payload":{"n":3,"note":"say \\"hi\\"\\n","operator":"op"}}'
    )

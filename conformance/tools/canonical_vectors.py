"""Write the canonical-encoding vectors.

Separate from ``generate.py`` because these are not derived from a model: they feed a value straight
into the encoder and pin the bytes. That distinction turned out to matter.

The model vectors cover everything an application actually produces, but every object key in the IR is
fixed ASCII - ``name``, ``type``, ``entities`` - so the *object key* comparator is never exercised by
them. A mutation replacing code point ordering with JavaScript's default UTF-16 comparison passed the
entire conformance suite. Field names, which can be anything, reach the IR as array *elements*, and
the array comparator is a different call site.

So these exist, and 001 is the reason. Every expected value here is written by hand from
``docs/format-contract.md``, not produced by an implementation.

    python conformance/tools/canonical_vectors.py --i-am-changing-the-contract
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "conformance" / "vectors" / "canonical"

ASTRAL = "\U0001f600"  # surrogate pair in UTF-16; one code point here
PRIVATE_USE = ""  # inside the BMP, above the surrogate range
COMPOSED = "é"
DECOMPOSED = "é"

# (name, value, expected bytes or None, expected error or None, why)
CASES: list[tuple[str, object, str | None, dict[str, str] | None, str]] = [
    (
        "001-key-order-by-code-point",
        {ASTRAL: 1, PRIVATE_USE: 2, "a": 3},
        '{"a":3,"' + PRIVATE_USE + '":2,"' + ASTRAL + '":1}',
        None,
        "Object keys are ordered by Unicode code point, not by UTF-16 code unit. JavaScript's\n"
        "default string comparison is UTF-16, which places U+1F600 - a surrogate pair starting at\n"
        "0xD83D - before U+E000, while code point order places it after.\n"
        "\n"
        "This vector exists because a mutation proved the gap. Every object key in the model IR is\n"
        "fixed ASCII, so replacing the key comparator with a naive sort passed the entire suite.\n"
        "Field names do reach the IR, but as array elements, which is a different call site.",
    ),
    (
        "002-nfc-normalisation",
        {DECOMPOSED: 1},
        '{"' + COMPOSED + '":1}',
        None,
        "Every string is NFC-normalised, keys included. A decomposed e plus combining acute is\n"
        "emitted as the composed U+00E9.",
    ),
    (
        "003-normalise-before-sorting",
        {DECOMPOSED: 1, "z": 2},
        '{"z":2,"' + COMPOSED + '":1}',
        None,
        "Normalisation happens before sorting. Sorting first would order the decomposed form under\n"
        '"e", ahead of "z"; normalising first puts it at U+00E9, after "z". Both orders are\n'
        "self-consistent, so only an explicit expectation catches a library that runs the two steps\n"
        "the wrong way round.",
    ),
    (
        "004-minimal-escaping",
        {"k": 'a"b\\c\n\t/' + "ż" + " "},
        '{"k":"a\\"b\\\\c\\n\\t/' + "ż" + " " + '"}',
        None,
        "Escape only the quote, the backslash and C0 controls. Do not escape non-ASCII, do not\n"
        "escape the forward slash, and do not escape U+2028. Several widely used JSON writers escape\n"
        "some of those - JavaScript-aware ones usually escape U+2028 - and any of them changes the\n"
        "hash.",
    ),
    (
        "005-integer-forms",
        {"a": 0, "b": -1, "c": 9007199254740991},
        '{"a":0,"b":-1,"c":9007199254740991}',
        None,
        "Shortest decimal form: no plus sign, no exponent, no leading zeros. The largest value here\n"
        "is 2^53-1, the last integer JavaScript represents exactly. Above it a library must refuse\n"
        "rather than truncate, since truncated bytes are bytes no other language can reproduce.",
    ),
    (
        "006-no-insignificant-whitespace",
        {"a": [1, 2], "b": {}, "c": None, "d": True},
        '{"a":[1,2],"b":{},"c":null,"d":true}',
        None,
        "No space after a colon or a comma, no newlines, no trailing newline. Empty containers and\n"
        "null are written out rather than omitted.",
    ),
    (
        "007-floats-are-refused",
        {"x": 1.5},
        None,
        {"error": "CanonicalError", "match": "floating point"},
        "Floating point has no canonical form: its textual representation differs between languages\n"
        "and the difference is unfixable after the fact.\n"
        "\n"
        "This is about literals in the *encoding*, not about the type system. A field may perfectly\n"
        "well have type float64, which reaches the IR as the string \"float64\" - see\n"
        "model/003-type-vocabulary-and-unicode.",
    ),
    (
        "008-duplicate-key-after-normalisation",
        None,  # written by hand below: two keys that collide cannot be expressed in a dict
        None,
        {"error": "CanonicalError", "match": "duplicate key"},
        "Two keys differing only in Unicode composition are the same key. A structure containing\n"
        "both is an error rather than a last-one-wins, because which one wins would depend on\n"
        "iteration order.\n"
        "\n"
        "value.json holds both spellings literally, which no language's dictionary type can, so a\n"
        "runner has to feed the parsed JSON object straight to the encoder without round-tripping it\n"
        "through a native map first. Parsers differ here: some collapse the two keys, in which case\n"
        "the runner should skip this vector and say so rather than passing it.",
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-am-changing-the-contract", action="store_true")
    args = parser.parse_args()
    if not getattr(args, "i_am_changing_the_contract"):
        print(
            "Refusing to run. These vectors are the contract; regenerating one means bumping\n"
            "conformance/contract-version.txt. Pass --i-am-changing-the-contract if that is\n"
            "genuinely what you are doing.",
            file=sys.stderr,
        )
        return 2

    for name, value, expected, error, why in CASES:
        out = OUT / name
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)

        if name == "008-duplicate-key-after-normalisation":
            # Hand-written, because Python cannot hold both keys in one dict.
            raw = '{"' + COMPOSED + '": 1, "' + DECOMPOSED + '": 2}'
            io.open(out / "value.json", "w", encoding="utf-8").write(raw)
        else:
            io.open(out / "value.json", "w", encoding="utf-8").write(
                json.dumps(value, ensure_ascii=False)
            )

        if expected is not None:
            io.open(out / "bytes.json", "w", encoding="utf-8").write(expected)
        if error is not None:
            io.open(out / "expected.json", "w", encoding="utf-8").write(
                json.dumps(error, indent=2) + "\n"
            )
        io.open(out / "why.txt", "w", encoding="utf-8").write(why + "\n")
        print(f"  wrote canonical/{name}")

    print("\nHand-written expectations. Review the diff: this is the contract.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

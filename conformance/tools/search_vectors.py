"""Generate the `search` family: what a token is, and when a row matches a query (contract 7l).

Every case is written by the Python reference and checked, unchanged, by every other library. The
texts are the edges where engines and languages disagree with each other - separators, case in
and out of ASCII, the Kelvin sign, the dotted capital I, the sharp s, a decomposed accent -
because a rule nobody tested at its edges is a rule each implementation finishes on its own.

Every character outside ASCII is written as an escape, here and in the vectors, so that a reader
sees which code point a case holds: the Kelvin sign and the letter K look the same on a page.

    python conformance/tools/search_vectors.py [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python/src"))
import sde  # noqa: E402
from sde.search import SearchRefused, matches, query_tokens, tokens  # noqa: E402
from sde.testing.loader import model_from_neutral  # noqa: E402

VECTORS = ROOT / "conformance" / "vectors"
OUT = VECTORS / "search"


def _tokens(text: str) -> dict[str, Any]:
    return {"kind": "tokens", "text": text}


def _match(row: dict[str, Any], query: str, fields: list[str]) -> dict[str, Any]:
    return {"kind": "match", "row": row, "query": query, "fields": fields}


TWO_FIELDS = {"title": "Red apple", "notes": "fresh fruit"}

CASES: list[tuple[str, dict[str, Any], str]] = [
    (
        "001-separators",
        _tokens("foo_bar-baz qux's"),
        "underscore, hyphen, apostrophe and space all separate; only letters and digits make "
        "a token",
    ),
    (
        "002-letters-and-digits",
        _tokens("a1b2 3.14"),
        "letters and digits run together; a decimal point separates",
    ),
    (
        "003-case-beyond-ascii",
        _tokens("\u0141\u00f3d\u017a \u0141\u00d3D\u0179"),
        "lower case is not an ASCII rule: the Polish capitals fold as A does",
    ),
    (
        "004-sharp-s-is-not-ss",
        _match({"t": "Stra\u00dfe"}, "strasse", ["t"]),
        "the rule is case-insensitive equality, not linguistics: the sharp s has no one-letter "
        "lower case other than itself",
    ),
    (
        "005-kelvin-sign",
        _tokens("5\u212am and 5Km"),
        "the Kelvin sign is a letter whose simple lower case is k, which an engine lower-casing "
        "ASCII only would miss",
    ),
    (
        "006-dotted-capital-i",
        _tokens("\u0130stanbul"),
        "U+0130's full lower case is two characters; its simple mapping, i, is the rule",
    ),
    (
        "007-decomposed-accent",
        _match({"t": "e\u0301clair"}, "\u00e9clair", ["t"]),
        "NFC first: a decomposed e-acute is the same token as a composed one",
    ),
    (
        "008-query-without-a-token",
        _match({"t": "anything"}, "--- !!! ___", ["t"]),
        "a query with no letter or digit is refused rather than matching every row or none",
    ),
    (
        "009-every-token-in-some-field",
        _match(TWO_FIELDS, "APPLE fresh", ["title", "notes"]),
        "each query token may be found in a different chosen field",
    ),
    (
        "010-a-field-not-chosen",
        _match(TWO_FIELDS, "apple fresh", ["title"]),
        "a token found only in a field that was not chosen does not count",
    ),
    (
        "011-a-run-without-separators",
        _match({"t": "\u65e5\u672c\u8a9e\u30c6\u30ad\u30b9\u30c8"}, "\u65e5\u672c", ["t"]),
        "a script written without spaces is one token: a prefix of it does not match",
    ),
    (
        "012-greek-and-superscripts",
        _tokens("\u03a9MEGA x\u00b2+y\u00b2"),
        "Greek folds like Latin; a superscript digit is a number and stays in its token",
    ),
    (
        "013-null-and-non-text-fields",
        _match({"t": None, "n": 42}, "42", ["t", "n"]),
        "only text is searched: a null or a number in a chosen field holds no token",
    ),
    (
        "014-repeated-query-tokens",
        _match({"t": "one two"}, "one ONE one", ["t"]),
        "a token repeated in the query is one token",
    ),
]


NOTES = {
    "entities": [
        {
            "name": "Note",
            "fields": [
                {"name": "id", "type": "int64"},
                {"name": "title", "type": "string"},
                {"name": "at", "type": "timestamptz"},
            ],
            "key": ["id"],
        }
    ],
    "relations": [],
    "atomic": [],
}


def _notes_map(model: sde.LogicalModel, routing: dict[str, str]) -> dict[str, Any]:
    layout = {"tables": {"Note": "note"}}
    return {
        "contract": 1,
        "model_version": model.version,
        "map_version": 1,
        "groups": {
            "Note": {
                "source": {"id": "Note@pg", "engine": "pg-main", "layout": layout},
                "derived": [
                    {"id": "Note@ch", "engine": "ch-1", "layout": layout, "lag_budget_ms": 30000}
                ],
            }
        },
        "routing": routing,
    }


def neighbours() -> dict[str, dict[str, Any]]:
    """The cases this family adds to `routing/` and `errors/`, by path under the vectors."""
    sde.clear_registry()
    model = model_from_neutral(NOTES)
    shape = {(s.kind, s.fields): s.id for s in sde.enumerate_shapes(model)}
    search, scan = shape[("search", ())], shape[("full_scan", ())]
    routed = _notes_map(model, {scan: "Note@ch", shape[("range_read", ("at",))]: "Note@ch"})
    return {
        "routing/003-a-search-goes-to-the-source": {
            "model.json": NOTES,
            "map.json": routed,
            "cases.json": [
                {"shape": scan, "expect": "Note@ch"},
                {"shape": search, "expect": "Note@pg"},
                {"shape": search, "fresh": True, "expect": "Note@pg"},
                {"shape": search, "in_write_transaction": True, "expect": "Note@pg"},
            ],
        },
        "errors/083-routing-names-a-search": {
            "model.json": NOTES,
            "map.json": _notes_map(model, {search: "Note@pg"}),
            "expected.json": {
                "error": "MapError",
                "match": "always goes to the group's source",
                "stage": "map",
                "why": (
                    "A search always goes to the group's source, so a routing entry for one says "
                    "nothing a library would follow - and a library that predates the kind refuses "
                    "the entry as a shape it does not enumerate. Refusing it here too, even naming "
                    "the source, keeps one stored map meaning the same thing to both (section 8)."
                ),
            },
        },
    }


def outcome(case: dict[str, Any]) -> dict[str, Any]:
    if case["kind"] == "tokens":
        return {"tokens": tokens(case["text"])}
    try:
        query = query_tokens(case["query"])
    except SearchRefused:
        return {"refused": "SearchRefused"}
    return {"query_tokens": list(query), "match": matches(case["row"], query, case["fields"])}


def render(value: Any) -> str:
    # ASCII escapes, so that a vector's bytes say exactly which code points it holds.
    return json.dumps(value, indent=2, ensure_ascii=True, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if a vector would change")
    args = parser.parse_args()
    changed = []
    planned: dict[Path, dict[str, str]] = {}
    for name, case, why in CASES:
        planned[OUT / name] = {
            "case.json": render(case),
            "expected.json": render(outcome(case)),
            "why.txt": why + "\n",
        }
    for name, documents in neighbours().items():
        planned[VECTORS / name] = {
            filename: json.dumps(body, indent=2) + "\n" for filename, body in documents.items()
        }
    for directory, files in planned.items():
        for filename, text in files.items():
            path = directory / filename
            if path.exists() and path.read_text(encoding="utf-8") == text:
                continue
            changed.append(str(path.relative_to(ROOT)))
            if not args.check:
                directory.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
    if args.check and changed:
        print("search vectors would change:", *changed, sep="\n  ")
        return 1
    written = "unchanged" if not changed else f"{len(changed)} files written"
    print(f"{len(planned)} vectors from the search rule, {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

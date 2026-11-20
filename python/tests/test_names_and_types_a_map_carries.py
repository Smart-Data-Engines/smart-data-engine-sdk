"""What a layout may name and spell (format contract §7a), held in one place for every library.

The rules are written three times, in Python, TypeScript and C++, and once more in the contract. The
shared vectors (``errors/144`` to ``146``, ``schema/021`` to ``023``) run the same cases through all
three; this holds what vectors cannot: that the vocabularies are the same words everywhere, and that
every type a library derives and every type the vectors render is within the grammar - a grammar
that refused one of those would break maps that load today.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sde.errors import DeclarationError
from sde.layout import ELASTICSEARCH_TYPES, ORDERBOOK_TYPES, _column_type
from sde.physical import UNSAFE_IN_NAMES, physical_name_refusal
from sde.schema import CLICKHOUSE_TYPE_WORDS, POSTGRES_TYPE_WORDS, column_type_refusal

ROOT = Path(__file__).resolve().parents[2]
VECTORS = ROOT / "conformance" / "vectors"
REFUSED = "is not a column type this library renders"


def _words(path: str, start: str, end: str) -> set[str]:
    text = (ROOT / path).read_text(encoding="utf-8")
    block = text[text.index(start) :]
    return set(re.findall(r"[\"']([A-Za-z][A-Za-z0-9]*)[\"']", block[: block.index(end)]))


def _contract_row(vocabulary: str) -> set[str]:
    text = (ROOT / "docs" / "format-contract.md").read_text(encoding="utf-8")
    (row,) = [line for line in text.splitlines() if line.startswith(f"| {vocabulary} | `")]
    return set(row.split("`")[1].split())


@pytest.mark.parametrize(
    ("name", "words", "vocabulary"),
    [
        ("POSTGRES_TYPE_WORDS", POSTGRES_TYPE_WORDS, "PostgreSQL"),
        ("CLICKHOUSE_TYPE_WORDS", CLICKHOUSE_TYPE_WORDS, "ClickHouse"),
    ],
)
def test_every_library_and_the_contract_spell_a_type_with_the_same_words(
    name: str, words: frozenset[str], vocabulary: str
) -> None:
    assert _words("typescript/src/schema.ts", f"export const {name}", "])") == words
    assert _words("cpp/src/schema.cpp", f"{name} = {{", "};") == words
    assert _contract_row(vocabulary) == words
    # The control: a word list read from the wrong place is not the same words.
    assert _words("cpp/src/schema.cpp", "CLICKHOUSE_TYPE_WORDS = {", "};") != (
        _words("cpp/src/schema.cpp", "POSTGRES_TYPE_WORDS = {", "};")
    )


NEUTRAL = ["bool", "int32", "int64", "float32", "float64", "string", "uuid", "date", "timestamp",
           "timestamptz", "decimal(12,2)", "decimal(18,0)", "decimal(38,10)"]  # fmt: skip


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_every_type_a_library_derives_is_within_the_grammar(dialect: str) -> None:
    derived = []
    for neutral in [*NEUTRAL, "bytes", "json"]:
        try:
            derived.append(_column_type(neutral, dialect))
        except DeclarationError:  # ClickHouse has no bytes or json, by design
            assert dialect == "clickhouse" and neutral in ("bytes", "json")
    assert len(derived) >= len(NEUTRAL)
    assert [kind for kind in derived if column_type_refusal(kind)] == []
    nullable = [f"Nullable({kind})" for kind in derived]
    assert [kind for kind in nullable if column_type_refusal(kind)] == []


def _layouts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        if isinstance(value.get("tables"), dict):
            yield value
        for item in value.values():
            yield from _layouts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _layouts(item)


def _types(columns: Any) -> Iterator[str]:
    if isinstance(columns, dict):
        for types in columns.values():
            if isinstance(types, dict):
                yield from (kind for kind in types.values() if isinstance(kind, str))


# Elasticsearch and the orderbook engine spell a layout's types their own way - a mapping type and
# the neutral one after a slash, a C type - and no statement carries them as text: a mapping is
# JSON, and the orderbook engine's schema is fixed. The grammar is the SQL renderers'.
OTHER_SPELLINGS = {*ELASTICSEARCH_TYPES.values(), *ORDERBOOK_TYPES.values()}


def _sql(kind: str) -> bool:
    return kind not in OTHER_SPELLINGS and not kind.startswith("long/decimal(")


def test_every_type_the_vectors_render_in_sql_is_within_the_grammar() -> None:
    """Each type a vector renders is accepted, and each case that expects the refusal has a type
    the grammar refuses - the vectors that refuse carry their type in the map itself (``021``,
    ``022``) or in a case's own columns (``023``)."""
    accepted: set[str] = set()
    refusals: list[set[str]] = []
    for path in sorted(VECTORS.rglob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if path.name == "cases.json" and isinstance(document, list):
            for case in document:
                if isinstance(case, dict) and "layout_columns" in case:
                    types = set(_types(case["layout_columns"]))
                    if case.get("match") == REFUSED:
                        refusals.append(types)
                    else:
                        accepted |= types
            continue
        vector = path.parent
        refuses = vector.parent.name == "schema" and vector.name.startswith(("021-", "022-"))
        for layout in _layouts(document):
            types = set(_types(layout.get("columns")))
            if refuses:
                refusals.append(types)
            else:
                accepted |= types
    other = {kind for kind in accepted if not _sql(kind)}
    accepted -= other
    assert len(accepted) > 30
    assert [kind for kind in sorted(accepted) if column_type_refusal(kind)] == []
    assert len(refusals) >= 15
    assert [types for types in refusals if all(column_type_refusal(k) is None for k in types)] == []
    # The control: the types left out are the other engines', and there are some of each.
    assert {"keyword", "long/timestamptz", "char*", "int64_t"} <= other


def test_a_type_of_128_characters_is_the_longest() -> None:
    """The bound, at its edge. Not a vector: no type of the two vocabularies is 128 characters long
    without spaces its server would not keep, and a vector's statement is one a server runs."""
    longest = "character" + " " * 107 + "varying(255)"
    assert len(longest) == 128
    assert column_type_refusal(longest) is None
    assert column_type_refusal(longest + " ") is not None  # the control: one more is refused


@pytest.mark.parametrize("character", sorted(UNSAFE_IN_NAMES))
def test_a_physical_name_holding_any_of_the_three_is_refused(character: str) -> None:
    assert physical_name_refusal(f"order{character}line") is not None
    assert physical_name_refusal("order_line") is None  # the control
    # Quoting characters stay a quoting rule's business (`schema/011`), not this one's.
    assert physical_name_refusal('a"b`c\\d') is None

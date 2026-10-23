"""Full-text search: one rule for what a token is and when a row matches, in every engine.

``Session.search`` returns the rows of an entity whose chosen text fields contain every token of a
query. The answer has to be the same whichever engine holds the group and whichever language the
library is written in, so the rule is part of the format contract (``docs/format-contract.md``,
search) and the vectors in ``conformance/vectors/search/`` pin it:

- **A token** is a maximal run of characters, in the text normalised to NFC, whose general category
  is a letter (L*) or a number (N*). Everything else separates tokens: spaces, punctuation, ``_``,
  ``-``, ``'``, symbols, combining marks that NFC did not compose.
- **Two tokens match** when they are equal after each character is replaced by its *simple*
  lower-case mapping - one character for one, without context. ``U+0130`` (``İ``) is the one
  character whose full lower case is two characters; its simple mapping, ``i``, is used, as Java
  and Lucene do. ``ß`` has no single-character lower case different from itself, so ``strasse``
  does not match ``Straße``: the rule is a case-insensitive equality, not a linguistic one.
- **A row matches** when every token of the query matches some token of some chosen field.

**The engine supplies candidates; this module decides.** Elasticsearch finds candidates in its
inverted index with an analyzer that splits at least where this rule does and lower-cases at least
what it does, so it can only return more rows than match, never fewer. PostgreSQL and ClickHouse
have no such index here and supply rows in key order. Their own lower-casing - ``lower()`` under the
``C`` collation changes ASCII only, and the Kelvin sign is a letter that lower-cases to ``k`` - is
never trusted with the answer, because a candidate an engine wrongly leaves out is a wrong result
nobody would see.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol, cast

from .query import QueryRefused, ReadPlan

__all__ = [
    "DEFAULT_MAX_EXAMINED",
    "MAX_EXAMINED",
    "MAX_SEARCH_LIMIT",
    "SearchPage",
    "SearchRefused",
    "TextIndex",
    "fold",
    "matches",
    "query_tokens",
    "text_index",
    "tokens",
]

MAX_SEARCH_LIMIT: Final = 1000
"""The most rows one page of a search returns."""

DEFAULT_MAX_EXAMINED: Final = 100_000
"""How many rows one search call examines, at most, unless the caller says otherwise."""

MAX_EXAMINED: Final = 1_000_000
"""The most a caller may let one search call examine."""


class SearchRefused(QueryRefused):
    """A search that cannot be run as asked - no token, no text field, a bad limit - refused before
    any engine is asked anything."""


@dataclass(frozen=True)
class SearchPage:
    """One page of a search, in key order.

    ``next_after`` is the position to continue from, and ``None`` only when nothing is left to
    examine. A page cut short by the examined bound holds fewer rows than the limit and still has
    a position, so a short page is not the last one - the rule ``scan`` already follows.
    ``examined`` is how many rows were checked to fill it.
    """

    rows: tuple[dict[str, Any], ...]
    next_after: Mapping[str, Any] | None
    examined: int


class TextIndex(Protocol):
    """An adapter whose engine finds candidates for a search in an index of its own."""

    def search_rows(
        self, table: str, plan: ReadPlan, query: Sequence[str], fields: Sequence[str]
    ) -> list[dict[str, Any]]:
        """One page of ``plan`` - key order, ``after``, ``limit`` plus one - restricted to rows
        that hold every token of ``query`` in one of ``fields``, by the engine's index. The
        library checks every row it returns, so a row too many costs time and a row too few is
        a wrong answer."""
        ...


def text_index(engine: object) -> TextIndex | None:
    """The adapter, when its engine has a text index (``search_rows``); otherwise ``None``."""
    return cast(TextIndex, engine) if callable(getattr(engine, "search_rows", None)) else None


def _is_word(char: str) -> bool:
    return unicodedata.category(char)[0] in ("L", "N")


def _fold_char(char: str) -> str:
    if char == "\u0130":
        return "i"
    lower = char.lower()
    return lower if len(lower) == 1 else char


def fold(token: str) -> str:
    """The token with each character replaced by its simple lower-case mapping."""
    return "".join(_fold_char(char) for char in token)


def tokens(text: str) -> list[str]:
    """The tokens of ``text``, in order, folded, duplicates kept."""
    found: list[str] = []
    current: list[str] = []
    for char in unicodedata.normalize("NFC", text):
        if _is_word(char):
            current.append(_fold_char(char))
        elif current:
            found.append("".join(current))
            current = []
    if current:
        found.append("".join(current))
    return found


def query_tokens(text: str) -> tuple[str, ...]:
    """The distinct folded tokens of a query, sorted; refused when there are none."""
    if not isinstance(text, str):
        raise SearchRefused("a search query is text")
    found = tuple(sorted(set(tokens(text))))
    if not found:
        raise SearchRefused(
            "the query has no token - no letter or digit - so it would match every row or none, "
            "and neither is an answer to a question"
        )
    return found


def matches(row: Mapping[str, Any], query: Sequence[str], fields: Iterable[str]) -> bool:
    """Whether every token of ``query`` (already folded) appears in one of ``fields`` of ``row``."""
    present: set[str] = set()
    for name in fields:
        value = row.get(name)
        if isinstance(value, str):
            present.update(tokens(value))
    return all(token in present for token in query)

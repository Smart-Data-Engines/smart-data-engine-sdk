"""Search against the engines: candidates in key order, and the rule - not the engine - decides.

The rows are the cases an engine's own text handling gets wrong: a decomposed accent, the Kelvin
sign, the dotted capital I and the sharp s. Each engine supplies them unfiltered in key order, so a
row only the contract's rule can tell apart is still found, and the same rows are found in both.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from test_runtime_privileges_live import runtime_roles

import sde

ROWS = [
    {"id": 1, "title": "Red apple", "body": "fresh fruit"},
    {"id": 2, "title": "e\u0301clair", "body": None},
    {"id": 3, "title": "\u00e9CLAIR", "body": "a 5\u212a run"},
    {"id": 4, "title": "\u0130stanbul", "body": "Stra\u00dfe"},
    {"id": 5, "title": "red_apple-pie", "body": "x"},
    {"id": 6, "title": "Plum", "body": "red"},
    {"id": 7, "title": "\u65e5\u672c\u8a9e", "body": "apple"},
]


@contextmanager
def fixture(dialect: str, *, hashed: bool = False) -> Iterator[tuple[Any, ...]]:
    with runtime_roles(dialect) as role:
        sde.clear_registry()

        @sde.entity
        class Note:
            id: int
            title: str
            body: str | None

        model = sde.build_model(Note)
        names = None
        if hashed:
            model, names = sde.hash_identifiers(model, b"s" * 32)
        group = sde.colocation_groups(model)[0]
        entity = model.entities[0].name
        layout = sde.default_layout(model, group, dialect=dialect)
        raw_layout = {
            "tables": dict(layout.tables),
            "columns": {k: dict(v) for k, v in layout.columns.items()},
        }
        placement = sde.load_map(
            {
                "contract": 3,
                "model_version": model.version,
                "map_version": 1,
                "groups": {
                    group.name: {"source": {"id": "source", "engine": "db", "layout": raw_layout}}
                },
            },
            model=model,
        )
        role.operator.ensure_schema(layout, keys={entity: model.entities[0].key})
        role.grant(layout.table_for(entity))
        recorder = sde.Recorder(model.version)
        session = sde.Session(
            model, placement, {"db": role.runtime}, recorder=recorder, names=names
        )
        # Saved out of key order: the page order is the engine's, by key.
        session.save_many("Note", list(reversed(ROWS)))
        recorder.roll()
        yield session, recorder, model, names


def ids(page: sde.SearchPage) -> list[int]:
    return [row["id"] for row in page.rows]


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
@pytest.mark.parametrize("hashed", [False, True])
def test_the_rule_finds_what_an_engine_would_fold_differently(dialect: str, hashed: bool) -> None:
    with fixture(dialect, hashed=hashed) as (session, _, _, _):
        everything = session.search("Note", "eclair \u00e9clair")
        assert ids(everything) == [], "e and e-acute are different letters"
        assert ids(session.search("Note", "\u00c9CLAIR")) == [2, 3]
        assert ids(session.search("Note", "5k")) == [3]
        assert ids(session.search("Note", "istanbul")) == [4]
        assert ids(session.search("Note", "strasse")) == []
        assert ids(session.search("Note", "STRASSE stra\u00dfe")) == []
        assert ids(session.search("Note", "stra\u00dfe")) == [4]
        assert ids(session.search("Note", "red apple")) == [1, 5]
        assert ids(session.search("Note", "red", fields=["body"])) == [6]
        assert ids(session.search("Note", "\u65e5\u672c")) == []
        page = session.search("Note", "apple")
        assert ids(page) == [1, 5, 7]
        assert page.rows[0] == {"id": 1, "title": "Red apple", "body": "fresh fruit"}
        assert page.examined == len(ROWS) and page.next_after is None


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_pages_walk_the_table_once_whatever_bounds_them(dialect: str) -> None:
    with fixture(dialect) as (session, _, _, _):
        for limit, bound in [(1, 2), (2, 3), (1, 1), (100, 7)]:
            found: list[int] = []
            examined = 0
            after = None
            for _ in range(20):
                page = session.search("Note", "apple", limit=limit, max_examined=bound, after=after)
                assert len(page.rows) <= limit and page.examined <= bound
                found += ids(page)
                examined += page.examined
                if page.next_after is None:
                    break
                after = page.next_after
            assert found == [1, 5, 7], (limit, bound)
            assert examined == len(ROWS), (limit, bound)


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
@pytest.mark.parametrize("hashed", [False, True])
def test_the_window_names_the_fields_as_the_model_does_and_never_the_text(
    dialect: str, hashed: bool
) -> None:
    with fixture(dialect, hashed=hashed) as (session, recorder, model, names):
        session.search("Note", "apple", fields=["title"])
        session.search("Note", "zzzyzzx")
        window = recorder.roll()
        assert window is not None
        document = window.as_record(model)
        (group,) = document["groups"].values()
        (entry,) = [shape for shape in group["shapes"] if shape["kind"] == "search"]
        assert entry["examined"] == 2 * len(ROWS)
        # The model's own names, as filtered_on reports them: digests when the client hashes.
        title, body = (
            (names.field("Note", "title"), names.field("Note", "body")) if hashed
            else ("title", "body")
        )
        looked = [(item["fields"], item["calls"]) for item in entry["searched_on"]]
        assert looked == sorted([(sorted([body, title]), 1), ([title], 1)])
        text = str(document)
        assert "zzzyzzx" not in text and "apple" not in text
        if hashed:
            assert "title" not in text and "body" not in text

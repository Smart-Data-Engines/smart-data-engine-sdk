"""Session.search: the rule decides, the engine supplies candidates, the page is bounded."""

from __future__ import annotations

import json
from typing import Any

import pytest

import sde
from sde.query import ReadPlan
from sde.testing.memory import MemoryEngine

NOTES = [
    (1, "Red apple", "fresh fruit"),
    (2, "Green pear", "fresh"),
    (3, "red APPLE pie", "baked"),
    (4, "Plum", None),
    (5, "Apple juice", "red label"),
    (6, "Kiwi", "green"),
    (7, "Red apple", "last row"),
]


class Scanned(MemoryEngine):
    """An engine without a text index: rows in key order, a page at a time."""

    def __init__(self) -> None:
        super().__init__()
        self.pages: list[int] = []

    def select_rows(self, table: str, plan: ReadPlan) -> list[dict[str, Any]]:
        self.pages.append(plan.limit)
        names = [column.name for column in plan.order]
        rows = sorted(self.tables.get(table, []), key=lambda row: tuple(row[n] for n in names))
        if plan.after is not None:
            rows = [row for row in rows if tuple(row[n] for n in names) > plan.after]
        return [dict(row) for row in rows[: plan.limit + 1]]


class Indexed(Scanned):
    """An engine with a text index that is generous: it also supplies rows that do not match."""

    def __init__(self, extra: frozenset[int] = frozenset()) -> None:
        super().__init__()
        self.extra = extra
        self.asked: list[tuple[tuple[str, ...], tuple[str, ...], int]] = []

    def select_rows(self, table: str, plan: ReadPlan) -> list[dict[str, Any]]:
        raise AssertionError("an engine with a text index is asked through it")

    def search_rows(
        self, table: str, plan: ReadPlan, query: Any, fields: Any
    ) -> list[dict[str, Any]]:
        self.asked.append((tuple(query), tuple(fields), plan.limit))
        hits = [
            row
            for row in Scanned.select_rows(self, table, ReadPlan(
                plan.columns, plan.filters, plan.order, plan.descending, plan.after, 10_000
            ))
            if row["id"] in self.extra or sde.search.matches(row, tuple(query), tuple(fields))
        ]
        return hits[: plan.limit + 1]


def fixture(
    engine: MemoryEngine | None = None, *, routed_scan: bool = False
) -> tuple[sde.Session, MemoryEngine, MemoryEngine, sde.Recorder, Any]:
    sde.clear_registry()

    @sde.entity
    class Note:
        id: int
        title: str
        body: str | None

    @sde.entity
    class Counter:
        id: int
        hits: int

    model = sde.build_model(Note, Counter)
    shapes = {(shape.entity, shape.kind): shape.id for shape in sde.enumerate_shapes(model)}
    layout = {
        "tables": {"Note": "note", "Counter": "counter"},
        "columns": {
            "Note": {"id": "bigint", "title": "text", "body": "text"},
            "Counter": {"id": "bigint", "hits": "bigint"},
        },
    }
    document: dict[str, Any] = {
        "contract": 3,
        "model_version": model.version,
        "map_version": 1,
        "groups": {
            name: {
                "source": {"id": "source", "engine": "source", "layout": layout},
                "derived": [
                    {"id": "copy", "engine": "copy", "layout": layout, "lag_budget_ms": 30000}
                ],
            }
            for name in ("Counter", "Note")
        },
    }
    if routed_scan:
        document["routing"] = {shapes[("Note", "full_scan")]: "copy"}
    placement = sde.load_map(document, model=model)
    source = engine if engine is not None else Scanned()
    copy = Scanned()
    for number, title, body in NOTES:
        source.tables.setdefault("note", []).append({"id": number, "title": title, "body": body})
    recorder = sde.Recorder(model.version)
    session = sde.Session(
        model, placement, {"source": source, "copy": copy}, recorder=recorder
    )
    return session, source, copy, recorder, (model, document, shapes)


def ids(page: sde.SearchPage) -> list[int]:
    return [row["id"] for row in page.rows]


def walk(session: sde.Session, text: str, **options: Any) -> tuple[list[int], int, int]:
    found: list[int] = []
    pages = examined = 0
    after = None
    while True:
        page = session.search("Note", text, after=after, **options)
        found += ids(page)
        pages += 1
        examined += page.examined
        if page.next_after is None:
            return found, pages, examined
        after = page.next_after


def test_every_token_in_some_chosen_field_in_key_order() -> None:
    session, _, _, _, _ = fixture()
    page = session.search("Note", "apple RED")
    assert ids(page) == [1, 3, 5, 7]
    assert page.next_after is None
    assert page.examined == len(NOTES)
    assert page.rows[0] == {"id": 1, "title": "Red apple", "body": "fresh fruit"}
    # The same text in fewer fields: "red" is only in the body of row 5.
    assert ids(session.search("Note", "apple red", fields=["title"])) == [1, 3, 7]
    assert ids(session.search("Note", "fresh", fields=["body"])) == [1, 2]


def test_a_full_page_carries_the_position_of_its_last_row() -> None:
    session, _, _, _, _ = fixture()
    first = session.search("Note", "apple", limit=2)
    assert ids(first) == [1, 3]
    assert first.next_after == {"id": 3}
    second = session.search("Note", "apple", limit=2, after=first.next_after)
    assert ids(second) == [5, 7]
    # The last match is the last row: nothing is left to examine, so there is no position.
    assert second.next_after is None


def test_the_examined_bound_cuts_a_page_short_and_the_walk_still_sees_every_row() -> None:
    session, source, _, _, _ = fixture()
    page = session.search("Note", "apple", max_examined=2)
    assert ids(page) == [1]
    assert page.examined == 2
    assert page.next_after == {"id": 2}, "a short page is not the last page"
    assert isinstance(source, Scanned) and source.pages[-1] == 2, "read only what may be examined"
    found, pages, examined = walk(session, "apple", max_examined=2)
    assert found == [1, 3, 5, 7]
    assert examined == len(NOTES), "no row examined twice, none skipped"
    assert pages == 4


def test_a_limit_met_on_the_last_candidate_of_a_page_keeps_reading_next_time() -> None:
    session, _, _, _, _ = fixture()
    found, _, examined = walk(session, "red", limit=1, max_examined=3)
    assert found == [1, 3, 5, 7]
    assert examined == len(NOTES)


def test_a_text_index_is_asked_for_what_the_page_needs_and_its_extra_rows_are_dropped() -> None:
    index = Indexed(extra=frozenset({2, 4, 6}))
    session, _, _, _, _ = fixture(index)
    page = session.search("Note", "Apple", limit=3)
    assert ids(page) == [1, 3, 5]
    assert page.next_after == {"id": 5}
    assert index.asked[0] == (("apple",), ("body", "title"), 3)
    # Rows 2 and 4 were supplied, examined and refused: each refill asks for what the page still
    # needs, which is one row a time here. An exact index - the token fields of section 7l - never
    # needs a refill.
    assert page.examined == 5
    assert [limit for _, _, limit in index.asked] == [3, 1, 1]


def test_a_search_reads_the_source_even_where_scans_are_routed_to_a_copy() -> None:
    session, source, copy, _, _ = fixture(routed_scan=True)
    assert isinstance(source, Scanned) and isinstance(copy, Scanned)
    session.scan("Note")
    assert copy.pages and not source.pages
    assert ids(session.search("Note", "pear")) == [2]
    assert source.pages


def test_a_map_routing_a_search_is_refused_by_a_library_that_knows_the_kind() -> None:
    _, _, _, _, (model, document, shapes) = fixture()
    for target in ("copy", "source"):
        routed = dict(document, routing={shapes[("Note", "search")]: target})
        with pytest.raises(sde.MapError, match="search always goes to the group's source"):
            sde.load_map(routed, model=model)


def test_a_search_route_in_a_map_loaded_without_its_model_is_not_followed() -> None:
    # Without the model a loader cannot tell a search's identifier from any other, so the entry
    # loads; the router is what keeps the search on the source.
    _, source, copy, _, (model, document, shapes) = fixture()
    assert isinstance(source, Scanned) and isinstance(copy, Scanned)
    placement = sde.load_map(dict(document, routing={shapes[("Note", "search")]: "copy"}))
    session = sde.Session(model, placement, {"source": source, "copy": copy})
    assert ids(session.search("Note", "pear")) == [2]
    assert source.pages and not copy.pages


def test_only_an_entity_with_text_has_a_search_shape() -> None:
    _, _, _, _, (model, _, shapes) = fixture()
    assert ("Note", "search") in shapes
    assert ("Counter", "search") not in shapes
    search = next(s for s in sde.enumerate_shapes(model) if s.kind == "search")
    assert search.fields == ()
    assert "search" in sde.SHAPE_KINDS and "search" in sde.SOURCE_ONLY_KINDS


@pytest.mark.parametrize(
    ("entity", "text", "options", "match"),
    [
        ("Note", "", {}, "no token"),
        ("Note", "--- ...", {}, "no token"),
        ("Note", 42, {}, "query is text"),
        ("Note", "apple", {"fields": []}, "non-empty list"),
        ("Note", "apple", {"fields": "title"}, "non-empty list"),
        ("Note", "apple", {"fields": [1]}, "non-empty list"),
        ("Note", "apple", {"fields": ["id", "title"]}, r"string only, not in \['id'\]"),
        ("Note", "apple", {"fields": ["missing"]}, "declares no field named"),
        ("Note", "apple", {"limit": 0}, "limit"),
        ("Note", "apple", {"limit": 1001}, "limit"),
        ("Note", "apple", {"limit": True}, "limit"),
        ("Note", "apple", {"max_examined": 0}, "max_examined"),
        ("Note", "apple", {"max_examined": 1_000_001}, "max_examined"),
        ("Note", "apple", {"max_examined": False}, "max_examined"),
        ("Note", "apple", {"after": {"title": "x"}}, "ordering key"),
        ("Counter", "apple", {}, "no field of type string"),
    ],
)
def test_a_search_that_cannot_run_is_refused_before_any_engine_is_asked(
    entity: str, text: Any, options: dict[str, Any], match: str
) -> None:
    session, source, copy, _, _ = fixture()
    assert isinstance(source, Scanned) and isinstance(copy, Scanned)
    with pytest.raises(sde.QueryRefused, match=match) as refused:
        session.search(entity, text, **options)
    if "after" not in options:
        assert isinstance(refused.value, sde.SearchRefused)
    assert source.pages == [] and copy.pages == []


def test_the_window_reports_what_was_examined_and_where_never_the_text() -> None:
    session, _, _, recorder, (model, _, shapes) = fixture()
    session.search("Note", "zebracrossing")
    session.search("Note", "apple", fields=["title"], limit=1)
    window = recorder.roll()
    assert window is not None
    document = window.as_record(model)
    entry = next(e for e in document["groups"]["Note"]["shapes"] if e["kind"] == "search")
    assert entry["id"] == shapes[("Note", "search")]
    assert entry["calls"] == 2 and entry["rows"] == 1 and entry["fields"] == []
    assert entry["examined"] == len(NOTES) + 1
    assert entry["searched_on"] == [
        {"fields": ["body", "title"], "calls": 1},
        {"fields": ["title"], "calls": 1},
    ]
    assert document["groups"]["Note"]["shape_mix"] == {"search": 1.0}
    text = json.dumps(document)
    assert "zebracrossing" not in text and "apple" not in text

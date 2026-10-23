"""The Elasticsearch adapter against a server: every value back exactly, pages, counts, search.

Set SDE_ELASTICSEARCH_DSN to run, for example
``elasticsearch+https://elastic:sde@127.0.0.1:59200/?ca_cert=%2Fpath%2Fhttp_ca.crt`` -
``make es-up`` starts one and prints it. Each test creates indexes under a fresh suffix and deletes
them, so runs do not see each other.
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID, uuid4

import pytest

import sde
from sde.engines.elasticsearch import ElasticsearchEngine

DSN = os.environ.get("SDE_ELASTICSEARCH_DSN")
pytestmark = pytest.mark.skipif(DSN is None, reason="set SDE_ELASTICSEARCH_DSN to run")

T0 = datetime(2026, 10, 23, 8, 0, 0, 123456, tzinfo=UTC)


@contextmanager
def fixture(*, hashed: bool = False) -> Iterator[tuple[sde.Session, ElasticsearchEngine, Any]]:
    assert DSN is not None
    sde.clear_registry()

    @sde.entity
    class Reading:
        id: int
        station: str
        note: str | None
        at: datetime
        local: sde.Timestamp | None
        day: date
        ref: UUID
        amount: Annotated[Decimal, sde.precision(18, 4)]
        level: float
        ok: bool

    model = sde.build_model(Reading)
    names = None
    if hashed:
        model, names = sde.hash_identifiers(model, b"e" * 32)
    group = sde.colocation_groups(model)[0]
    entity = model.entities[0].name
    layout = sde.default_layout(model, group, dialect="elasticsearch")
    table = "sde_test_" + uuid4().hex[:12]
    raw = {"tables": {entity: table}, "columns": {k: dict(v) for k, v in layout.columns.items()}}
    placement = sde.load_map(
        {
            "contract": 3,
            "model_version": model.version,
            "map_version": 1,
            "groups": {group.name: {"source": {"id": "es", "engine": "es", "layout": raw}}},
        },
        model=model,
    )
    engine = ElasticsearchEngine(DSN)
    engine.connect()
    try:
        engine.ensure_schema(
            placement.groups[group.name].source.layout, keys={entity: model.entities[0].key}
        )
        session = sde.Session(model, placement, {"es": engine}, names=names)
        yield session, engine, model
    finally:
        engine._request("DELETE", "/" + table)
        engine.close()


def rows() -> list[dict[str, Any]]:
    return [
        {
            "id": number,
            "station": ["WAW", "KRK", "WAW", "GDN"][number % 4],
            "note": [None, "Red apple", "e\u0301clair", "5\u212a run", "\u0130stanbul"][number % 5],
            "at": T0.replace(microsecond=number),
            "local": None if number % 3 == 0 else datetime(1, 1, 1, 0, 0, 0, number),
            "day": [date(1, 1, 1), date(9999, 12, 31), date(2026, 10, 23)][number % 3],
            "ref": UUID(int=number),
            "amount": Decimal("-99999999999999.9999") if number == 1 else Decimal(number) / 4,
            "level": number / 10,
            "ok": number % 2 == 0,
        }
        for number in range(1, 13)
    ]


@pytest.mark.parametrize("hashed", [False, True])
def test_every_value_comes_back_as_it_was_written(hashed: bool) -> None:
    with fixture(hashed=hashed) as (session, _engine, _model):
        written = rows()
        session.save("Reading", written[0])
        session.save_many("Reading", written[1:])
        for row in written:
            assert session.get("Reading", {"id": row["id"]}) == row
        assert session.get("Reading", {"id": 999}) is None
        assert session.count("Reading") == len(written)


def test_a_second_save_of_a_key_replaces_the_first() -> None:
    with fixture() as (session, _engine, _model):
        first = rows()[0]
        session.save("Reading", first)
        session.save("Reading", {**first, "station": "POZ", "note": None})
        assert session.get("Reading", {"id": first["id"]})["station"] == "POZ"
        assert session.count("Reading") == 1


def pages(session: sde.Session, **options: Any) -> list[int]:
    found: list[int] = []
    after = None
    for _ in range(50):
        page = session.scan("Reading", after=after, **options)
        found += [row["id"] for row in page.rows]
        if page.next_after is None:
            return found
        after = page.next_after
    raise AssertionError("pagination did not terminate")


@pytest.mark.parametrize("size", [1, 2, 5, 12])
def test_pages_and_counts_follow_the_contract_order_with_nulls_last(size: int) -> None:
    with fixture() as (session, _engine, _model):
        written = rows()
        session.save_many("Reading", written)
        assert pages(session, limit=size) == list(range(1, 13))
        for descending in (False, True):
            present = sorted(
                [row for row in written if row["note"] is not None],
                key=lambda row: (row["note"], row["id"]),
                reverse=descending,
            )
            absent = sorted(
                [row for row in written if row["note"] is None],
                key=lambda row: row["id"],
                reverse=descending,
            )
            assert pages(session, limit=size, order_by="note", descending=descending) == [
                row["id"] for row in present + absent
            ]
        waw = [row["id"] for row in written if row["station"] == "WAW"]
        assert pages(session, limit=size, where={"station": "WAW"}) == waw
        assert session.count("Reading", where={"station": "WAW"}) == len(waw)
        assert session.count("Reading", where={"note": None}) == sum(
            row["note"] is None for row in written
        )
        bounded = sde.Range("at", low=T0.replace(microsecond=3), high=T0.replace(microsecond=7))
        assert pages(session, limit=size, bounds=bounded) == [3, 4, 5, 6]
        assert session.count("Reading", bounds=bounded) == 4


def test_a_search_reads_only_the_rows_that_hold_its_tokens() -> None:
    with fixture() as (session, _engine, _model):
        written = rows()
        session.save_many("Reading", written)
        expected = [row["id"] for row in written if row["note"] == "e\u0301clair"]
        page = session.search("Reading", "\u00c9CLAIR")
        assert [row["id"] for row in page.rows] == expected
        assert page.examined == len(expected), "the index supplied only rows that match"
        assert [r["id"] for r in session.search("Reading", "5k").rows] == [
            row["id"] for row in written if row["note"] == "5\u212a run"
        ]
        assert [r["id"] for r in session.search("Reading", "istanbul waw").rows] == [
            row["id"] for row in written
            if row["note"] == "\u0130stanbul" and row["station"] == "WAW"
        ]
        assert session.search("Reading", "waw", fields=["note"]).rows == ()
        walked: list[int] = []
        after = None
        while True:
            page = session.search("Reading", "red apple", limit=1, after=after)
            walked += [row["id"] for row in page.rows]
            if page.next_after is None:
                break
            after = page.next_after
        assert walked == [row["id"] for row in written if row["note"] == "Red apple"]


def test_a_read_by_key_returns_only_a_document_that_holds_that_key() -> None:
    """A document under a key's _id that holds another key - written by something else, or a
    collision - is not that row, and a read by key does not return it."""
    from sde.engines.elasticsearch import document_id

    with fixture() as (session, engine, model):
        group = sde.colocation_groups(model)[0]
        table = session._placement.groups[group.name].source.layout.tables["Reading"]
        written = rows()[0]
        session.save("Reading", written)
        stray = dict(engine._request("GET", f"/{table}/_doc/{document_id([written['id']])}")[1])
        stray["_source"]["id"] = 999
        status, _ = engine._request(
            "PUT", f"/{table}/_doc/{document_id([7])}?refresh=wait_for", stray["_source"]
        )
        assert status in (200, 201)
        assert session.get("Reading", {"id": 7}) is None
        assert session.get("Reading", {"id": written["id"]}) == written


def test_an_index_that_exists_with_other_fields_is_refused_not_changed() -> None:
    with fixture() as (session, engine, model):
        group = sde.colocation_groups(model)[0]
        source = session._placement.groups[group.name].source
        changed = dict(source.layout.columns["Reading"])
        changed["note"] = "long"
        other = sde.PhysicalLayout(tables=dict(source.layout.tables), columns={"Reading": changed})
        with pytest.raises(sde.EngineError, match="changes no field in place"):
            engine.ensure_schema(other, keys={"Reading": ["id"]})


def test_the_map_watermark_moves_forward_only() -> None:
    with fixture() as (_session, engine, _model):
        engine._request("DELETE", "/sde_map_state")
        try:
            assert engine.map_watermark() is None
            engine.record_map_version(3, model_version="0" * 16)
            engine.record_map_version(5, model_version="0" * 16)
            assert engine.map_watermark() == 5
        finally:
            engine._request("DELETE", "/sde_map_state")


def test_a_key_scoped_to_the_layouts_indexes_is_all_the_adapter_needs() -> None:
    """The privileges docs/engine-connections.md names, and not one more: no cluster privilege."""
    assert DSN is not None
    table = "sde_test_" + uuid4().hex[:12]
    admin = ElasticsearchEngine(DSN)
    admin.connect()
    status, key = admin._request(
        "POST",
        "/_security/api_key",
        {
            "name": "sde-least-" + table,
            "role_descriptors": {
                "app": {
                    "cluster": [],
                    "indices": [
                        {
                            "names": [table, "sde_map_state"],
                            "privileges": ["create_index", "index", "read", "view_index_metadata"],
                        }
                    ],
                }
            },
        },
    )
    assert status == 200, key
    parts = urllib.parse.urlsplit(DSN)
    query = urllib.parse.urlencode(
        [("api_key", key["encoded"]), *urllib.parse.parse_qsl(parts.query)]
    )
    host = parts.netloc.rpartition("@")[2]
    scoped = urllib.parse.urlunsplit((parts.scheme, host, parts.path, query, ""))
    sde.clear_registry()

    @sde.entity
    class Note:
        id: int
        title: str

    model = sde.build_model(Note)
    layout = sde.PhysicalLayout(tables={"Note": table}, columns={"Note": {"id": "long",
                                                                         "title": "keyword"}})
    engine = ElasticsearchEngine(scoped)
    try:
        engine.connect()
        engine.ensure_schema(layout, keys={"Note": ["id"]})
        placement = sde.load_map(
            {
                "contract": 3,
                "model_version": model.version,
                "map_version": 1,
                "groups": {"Note": {"source": {"id": "es", "engine": "es", "layout": {
                    "tables": {"Note": table}, "columns": {"Note": dict(layout.columns["Note"])},
                }}}},
            },
            model=model,
        )
        session = sde.Session(model, placement, {"es": engine})
        session.save_many("Note", [{"id": 1, "title": "Red apple"}, {"id": 2, "title": "Pear"}])
        assert session.get("Note", {"id": 2}) == {"id": 2, "title": "Pear"}
        assert [row["id"] for row in session.search("Note", "apple").rows] == [1]
        assert session.count("Note") == 2
        engine.map_watermark()
    finally:
        engine.close()
        admin._request("DELETE", "/_security/api_key", {"ids": [key["id"]]})
        admin._request("DELETE", "/" + table)
        admin.close()


def test_tls_is_verified_against_the_given_authority_only() -> None:
    assert DSN is not None
    parts = urllib.parse.urlsplit(DSN)
    if parts.scheme != "elasticsearch+https":
        pytest.skip("the configured server is not TLS")
    without = urllib.parse.urlunsplit(parts._replace(query=""))
    with pytest.raises(sde.EngineError, match="could not connect"):
        ElasticsearchEngine(without).connect()


def test_a_decimal_bound_with_more_digits_than_the_column_is_answered() -> None:
    """``>= 0.005`` on a scale of 4 is a question about the column, answered as PostgreSQL and
    ClickHouse answer it, not refused (second review, finding 8)."""
    with fixture() as (session, _engine, _model):
        written = rows()
        session.save_many("Reading", written)
        bound = Decimal("2.50005")
        wanted = [row["id"] for row in written if row["amount"] >= bound]
        assert pages(session, limit=5, bounds=sde.Range("amount", low=bound)) == wanted
        below = [row["id"] for row in written if row["amount"] < bound]
        assert pages(session, limit=5, bounds=sde.Range("amount", high=bound)) == below
        assert session.count("Reading", where={"amount": Decimal("2.50005")}) == 0
        assert session.count("Reading", where={"amount": Decimal("2.5")}) == 1


def test_a_float32_reads_back_as_the_value_it_is_filtered_on() -> None:
    assert DSN is not None
    sde.clear_registry()

    @sde.entity
    class Gauge:
        id: int
        x: sde.Float32

    model = sde.build_model(Gauge)
    table = "sde_test_" + uuid4().hex[:12]
    layout = sde.PhysicalLayout(tables={"Gauge": table}, columns={"Gauge": {"id": "long",
                                                                             "x": "float"}})
    engine = ElasticsearchEngine(DSN)
    engine.connect()
    try:
        engine.ensure_schema(layout, keys={"Gauge": ["id"]})
        placement = sde.load_map(
            {"contract": 3, "model_version": model.version, "map_version": 1,
             "groups": {"Gauge": {"source": {"id": "es", "engine": "es", "layout": {
                 "tables": {"Gauge": table}, "columns": {"Gauge": {"id": "long", "x": "float"}},
             }}}}},
            model=model,
        )
        session = sde.Session(model, placement, {"es": engine})
        session.save("Gauge", {"id": 1, "x": 0.99999999})
        row = session.get("Gauge", {"id": 1})
        assert row == {"id": 1, "x": 1.0}
        assert session.count("Gauge", where={"x": 1.0}) == 1
        assert session.count("Gauge", bounds=sde.Range("x", low=1.0)) == 1
        assert session.count("Gauge", bounds=sde.Range("x", high=1.0)) == 0
    finally:
        engine._request("DELETE", "/" + table)
        engine.close()


def test_an_index_with_a_parameter_this_library_never_sets_is_refused() -> None:
    """Every type matching is not enough: a normalizer changes what equality means (second
    review, finding 4). Refused when the schema is checked and when a session reads the index."""
    assert DSN is not None
    table = "sde_test_" + uuid4().hex[:12]
    layout = sde.PhysicalLayout(tables={"Note": table}, columns={"Note": {"id": "long",
                                                                         "body": "keyword"}})
    from sde.schema import elasticsearch_mapping

    mapping = elasticsearch_mapping(layout, "Note", {"Note": ["id"]})
    mapping["properties"]["body"]["ignore_above"] = 5
    engine = ElasticsearchEngine(DSN)
    engine.connect()
    try:
        status, _ = engine._request("PUT", "/" + table, {"mappings": mapping})
        assert status == 200
        with pytest.raises(sde.EngineError, match="would not create"):
            engine.validate_schema(layout, keys={"Note": ["id"]})
        with pytest.raises(sde.EngineError, match="would not create"):
            engine.insert(table, {"id": 1, "body": "x"})
        other = ElasticsearchEngine(DSN)
        other.connect()
        try:
            with pytest.raises(sde.EngineError, match="would not create"):
                other.get(table, {"id": 1})
        finally:
            other.close()
    finally:
        engine._request("DELETE", "/" + table)
        engine.close()


def test_an_index_made_for_another_entity_is_refused() -> None:
    assert DSN is not None
    table = "sde_test_" + uuid4().hex[:12]
    columns = {"id": "long", "body": "keyword"}
    other = sde.PhysicalLayout(tables={"Other": table}, columns={"Other": columns})
    mine = sde.PhysicalLayout(tables={"Note": table}, columns={"Note": columns})
    engine = ElasticsearchEngine(DSN)
    engine.connect()
    try:
        engine.ensure_schema(other, keys={"Other": ["id"]})
        with pytest.raises(sde.EngineError, match="exists for Other"):
            engine.ensure_schema(mine, keys={"Note": ["id"]})
    finally:
        engine._request("DELETE", "/" + table)
        engine.close()

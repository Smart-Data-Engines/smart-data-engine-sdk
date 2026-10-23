"""The Elasticsearch adapter without a server: its connection profile, its values and its _id."""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import sde
from sde.engines.elasticsearch import (
    KEYWORD_BYTES,
    WATERMARK_MAPPING,
    ElasticsearchEngine,
    document_id,
    parse_dsn,
    search_tokens,
    stored,
)
from sde.schema import schema_statements
from sde.testing.loader import model_from_neutral


def test_a_profile_is_read_as_written_and_never_printed_with_its_secret(tmp_path: Any) -> None:
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    path = str(ca).replace("/", "%2F")
    connection = parse_dsn(f"elasticsearch+https://app:p%40ss:w@search.example.com/?ca_cert={path}")
    assert (connection.host, connection.port, connection.tls) == ("search.example.com", 9200, True)
    assert connection.ca_cert == str(ca)
    assert connection.authorization == "Basic YXBwOnBAc3M6dw=="
    assert "p@ss" not in repr(connection) and "YXBw" not in repr(connection)
    keyed = parse_dsn("elasticsearch+https://[::1]:9243?api_key=Zm9vOmJhcg==&connect_timeout=2.5")
    assert keyed.authorization == "ApiKey Zm9vOmJhcg==" and keyed.port == 9243
    assert keyed.connect_timeout == 2.5 and keyed.send_receive_timeout == 30
    plain = parse_dsn("elasticsearch+http://elastic:x@127.0.0.1:9200")
    assert plain.tls is False


@pytest.mark.parametrize(
    ("dsn", "match"),
    [
        ("https://host:9200", "starts"),
        ("elasticsearch+http://elastic:x@search.example.com:9200", "loopback"),
        ("elasticsearch+http://10.0.0.5:9200", "loopback"),
        ("elasticsearch+https://host/?verify=false", "disabling"),
        ("elasticsearch+https://host/?unknown=1", "unknown"),
        ("elasticsearch+https://host/?connect_timeout=1&connect_timeout=2", "twice"),
        ("elasticsearch+https://host/?connect_timeout=0", "between"),
        ("elasticsearch+https://host/?connect_timeout=nan", "number of seconds"),
        ("elasticsearch+https://host/?connect_timeout=1e1", "number of seconds"),
        ("elasticsearch+https://[127.0.0.1]:9200", "IPv6|URL|port|host"),
        ("elasticsearch+http://:secret@127.0.0.1:9200", "nonempty"),
        ("elasticsearch+http://@127.0.0.1:9200", "nonempty"),
        ("elasticsearch+https://:secret@host/?api_key=Zm9v", "not both"),
        ("elasticsearch+https://host/?api_key=Zm9vY", "encoded key"),
        ("elasticsearch+https://us%zzer:pw@host/", "percent-encoded"),
        ("elasticsearch+https://user:%ff@host/", "percent-encoded"),
        ("elasticsearch+https://host/?ca_cert=relative.pem", "absolute"),
        ("elasticsearch+https://host/?ca_cert=%2Fno%2Fsuch%2Ffile.pem", "readable"),
        ("elasticsearch+https://u:p@host/?api_key=Zm9v", "not both"),
        ("elasticsearch+https://host/?api_key=not%20base64!", "encoded key"),
        ("elasticsearch+https://host/index", "no path"),
        ("elasticsearch+https://host/#x", "fragment"),
        ("elasticsearch+https://host:99999", "port"),
        ("elasticsearch+https://a:b@host /", "whitespace"),
        ("elasticsearch+http://127.0.0.1/?ca_cert=%2Fetc%2Fhosts", "belong to"),
        ("elasticsearch+https://[fe80::1%25eth0]:9200", "zone"),
    ],
)
def test_a_profile_is_refused_whole_before_any_socket(dsn: str, match: str) -> None:
    with pytest.raises(sde.EngineError, match=match):
        ElasticsearchEngine(dsn)


@pytest.mark.parametrize(
    ("kind", "value", "expected"),
    [
        ("boolean", True, True),
        ("integer", -(2**31), -(2**31)),
        ("long", 2**63 - 1, 2**63 - 1),
        ("double", 0.1, 0.1),
        ("keyword", "\u0141\u00f3d\u017a", "\u0141\u00f3d\u017a"),
        (
            "keyword/uuid",
            "0000000A-0000-0000-0000-000000000001",
            "0000000a-0000-0000-0000-000000000001",
        ),
        ("date", date(1, 1, 1), "0001-01-01"),
        ("long/timestamp", datetime(1969, 12, 31, 23, 59, 59, 999999), -1),
        ("long/timestamptz", datetime(1970, 1, 1, 1, tzinfo=timezone(timedelta(hours=1))), 0),
        (
            "long/timestamptz",
            datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
            253402300799999999,
        ),
        ("long/decimal(12,2)", Decimal("-12.30"), -1230),
        ("long/decimal(18,0)", 999999999999999999, 999999999999999999),
        ("long/decimal(18,18)", Decimal("0.000000000000000001"), 1),
        ("keyword", None, None),
    ],
)
def test_a_value_is_stored_exactly(kind: str, value: Any, expected: Any) -> None:
    assert stored(kind, value) == expected


@pytest.mark.parametrize(
    ("kind", "value", "match"),
    [
        ("boolean", 1, "bool"),
        ("integer", 2**31, "outside int32"),
        ("long", True, "int"),
        ("double", float("nan"), "NaN"),
        ("keyword", "x" * (KEYWORD_BYTES + 1), "cannot index"),
        ("keyword", "\u00e9" * (KEYWORD_BYTES // 2 + 1), "cannot index"),
        ("keyword/uuid", "not-a-uuid", "uuid"),
        ("date", datetime(2026, 1, 1), "date"),
        ("long/timestamptz", date(2026, 1, 1), "datetime"),
        ("long/decimal(12,2)", Decimal("1.005"), "fractional"),
        ("long/decimal(4,2)", Decimal("100.00"), "4 digits"),
        ("long/decimal(12,2)", 1.5, "never a float"),
        ("long/decimal(12,2)", Decimal("NaN"), "finite"),
    ],
)
def test_a_value_that_would_change_is_refused_before_it_is_sent(
    kind: str, value: Any, match: str
) -> None:
    with pytest.raises(sde.EngineError, match=match):
        stored(kind, value)


def test_the_document_id_is_the_digest_of_the_stored_key_and_nothing_else() -> None:
    one = document_id([7, "a"])
    assert one == document_id([7, "a"]) and len(one) == 64
    assert one != document_id(["a", 7]) and one != document_id([7, "a "])
    assert document_id(["7"]) != document_id([7]) and document_id([True]) != document_id(["true"])
    assert document_id(["ab", "c"]) != document_id(["a", "bc"]), "every value is framed"
    # Not normalised: two keys that differ in how an accent is composed are two rows in every
    # other engine, so they are two documents here.
    assert document_id(["e\u0301"]) != document_id(["\u00e9"])
    assert search_tokens("Red red RED apple") == ["apple", "red"]
    assert search_tokens(None) == []


def test_a_layout_renders_one_strict_index_per_entity_and_no_physical_design() -> None:
    sde.clear_registry()

    @sde.entity
    class Note:
        id: int
        title: str
        at: datetime

    model = sde.build_model(Note)
    group = sde.colocation_groups(model)[0]
    layout = sde.default_layout(model, group, dialect="elasticsearch")
    (statement,) = schema_statements(layout, keys={"Note": ["id"]}, dialect="elasticsearch")
    assert statement == (
        '{"index":"note","mappings":{"_meta":{"sde":{"entity":"Note","key":["id"]}},'
        '"dynamic":"strict","properties":{"__sde_tokens":{"properties":{"title":'
        '{"type":"keyword"}}},"at":{"meta":{"sde_type":"timestamptz"},"type":"long"},'
        '"id":{"type":"long"},"title":{"type":"keyword"}}}}'
    )
    assert not sde.can_store("bytes", dialect="elasticsearch")
    assert not sde.can_store("json", dialect="elasticsearch")
    assert not sde.can_store("decimal(19,2)", dialect="elasticsearch")
    assert sde.can_store("decimal(18,18)", dialect="elasticsearch")


@pytest.mark.parametrize(
    ("entity", "match"),
    [("_Hidden", "cannot start"), ("Odd#Name", "cannot contain"), ("Has Space", "cannot contain")],
)
def test_an_entity_whose_name_cannot_be_an_index_cannot_be_laid_out(
    entity: str, match: str
) -> None:
    model = model_from_neutral(
        {
            "entities": [{"name": entity, "fields": [{"name": "id", "type": "int64"}],
                          "key": ["id"]}],
            "relations": [],
            "atomic": [],
        }
    )
    group = sde.colocation_groups(model)[0]
    with pytest.raises(sde.DeclarationError, match=match):
        sde.default_layout(model, group, dialect="elasticsearch")


def test_the_facts_say_what_the_adapter_does() -> None:
    facts = sde.engine_facts("elasticsearch")
    assert facts["transactions"] is False
    assert facts["write_generations"] is True
    assert facts["reads"] == {
        "get": True, "scan": True, "count": True, "summarize": False, "search": True,
    }
    assert facts["text_index"] is True
    assert callable(ElasticsearchEngine.write_fence)
    assert not hasattr(ElasticsearchEngine, "summarize_rows")
    assert callable(ElasticsearchEngine.search_rows)
    with pytest.raises(sde.EngineError, match="no multi-document transactions"):
        ElasticsearchEngine("elasticsearch+http://127.0.0.1:9200").transaction()


def test_a_decimal_is_exact_whatever_the_callers_decimal_context() -> None:
    """``Decimal.scaleb`` rounds to the thread's context; at a precision of ten a value with
    eighteen digits was stored rounded, and two keys shared one _id (second review, finding 1)."""
    from decimal import localcontext

    from sde.engines.elasticsearch import _read_value

    with localcontext() as context:
        context.prec = 10
        assert stored("long/decimal(18,2)", Decimal("1234567890123456.78")) == 123456789012345678
        assert stored("long/decimal(18,2)", Decimal("-0.10")) == -10
        back = _read_value("long/decimal(18,2)", 123456789012345678)
        assert back == Decimal("1234567890123456.78") and str(back) == "1234567890123456.78"
        assert str(_read_value("long/decimal(12,4)", -5)) == "-0.0005"
        assert document_id([stored("long/decimal(18,2)", Decimal("1234567890123456.78"))]) != (
            document_id([stored("long/decimal(18,2)", Decimal("1234567890123456.79"))])
        )
    with pytest.raises(sde.EngineError, match="18 digits"):
        stored("long/decimal(18,2)", Decimal("1E+999999999"))
    with pytest.raises(sde.EngineError, match="fractional"):
        stored("long/decimal(18,2)", Decimal("1E-999999999"))
    assert stored("long/decimal(18,2)", Decimal("1.500000000000")) == 150


@pytest.mark.parametrize(
    ("value", "operation", "bound"),
    [
        (Decimal("0.005"), "ge", 1),
        (Decimal("0.005"), "lt", 1),
        (Decimal("-0.005"), "ge", 0),
        (Decimal("0.01"), "ge", 1),
        (Decimal("0.005"), "eq", None),
        (Decimal("0.01"), "eq", 1),
        (Decimal("1E+30"), "ge", 10**18),
        (Decimal("-1E+30"), "lt", -(10**18)),
        (Decimal("1E+30"), "eq", None),
        (Decimal("1E+16"), "eq", None),
        (Decimal("-1.234"), "ge", -123),
        (Decimal("1.234"), "lt", 124),
        (Decimal("9999999999999999.995"), "ge", 10**18),
        (Decimal("1E-999999999"), "ge", 1),
    ],
)
def test_a_decimal_predicate_is_a_question_about_the_column(
    value: Decimal, operation: str, bound: int | None
) -> None:
    from sde.engines.elasticsearch import _decimal_bound

    assert _decimal_bound("long/decimal(18,2)", value, operation) == bound


def test_a_float32_is_kept_as_the_float32_the_engine_indexes() -> None:
    assert stored("float", 0.99999999) == 1.0
    assert stored("float", 0.1) == 0.10000000149011612
    assert stored("double", 0.1) == 0.1
    with pytest.raises(sde.EngineError, match="outside float32"):
        stored("float", 1e39)
    with pytest.raises(sde.EngineError, match="outside float64"):
        stored("double", 10**400)


def test_a_token_longer_than_a_term_is_refused_although_its_text_is_not() -> None:
    from sde.engines.elasticsearch import document

    text = "\u023a" * 16383  # 32,766 bytes, folding to three bytes a letter
    assert len(text.encode("utf-8")) == KEYWORD_BYTES
    with pytest.raises(sde.EngineError, match="once normalised and folded"):
        document({"id": "long", "t": "keyword"}, ["id"], {"id": 1, "t": text})
    with pytest.raises(sde.EngineError, match="lone surrogate"):
        stored("keyword", "\ud800")


def test_an_answer_some_shard_did_not_contribute_to_is_refused() -> None:
    whole = ElasticsearchEngine._whole
    whole({"timed_out": False, "_shards": {"total": 2, "failed": 0}}, "a page")
    with pytest.raises(sde.EngineError, match="partial"):
        whole({"timed_out": True, "_shards": {"failed": 0}}, "a page")
    with pytest.raises(sde.EngineError, match="1 failed shard"):
        whole({"timed_out": False, "_shards": {"failed": 1}}, "a count")
    refused = ElasticsearchEngine._write_error(
        400, {"error": {"type": "illegal_argument_exception", "reason": "Preview: 'secret'"}}
    )
    assert refused == "400 illegal_argument_exception" and "secret" not in refused


def test_every_read_asks_the_engine_to_fail_rather_than_answer_in_part() -> None:
    """A shard that fails or times out fails the search (``allow_partial_search_results=false``),
    and an answer that still says it is partial is refused - a page, a count and the watermark."""
    from sde.query import ReadColumn, ReadPlan
    from sde.schema import elasticsearch_mapping

    layout = sde.PhysicalLayout(tables={"Note": "note"}, columns={"Note": {"id": "long"}})
    mapping = elasticsearch_mapping(layout, "Note", {"Note": ["id"]})
    column = ReadColumn("id", "int64")
    plan = ReadPlan((column,), (), (column,), False, None, 10)
    sent: list[str] = []

    def answering(partial: dict[str, Any]) -> Any:
        def request(method: str, path: str, body: Any = None, **_: Any) -> tuple[int, Any]:
            sent.append(path)
            if path == "/sde_map_state/_mapping":
                return 200, {"sde_map_state": {"mappings": WATERMARK_MAPPING}}
            if path.endswith("/_mapping"):
                return 200, {"note": {"mappings": mapping}}
            if "/_count" in path:
                return 200, {"count": 3, **partial}
            return 200, {"hits": {"hits": []}, **partial}
        return request

    whole = {"timed_out": False, "_shards": {"total": 1, "successful": 1, "failed": 0}}
    for partial, refused in (
        (whole, False),
        ({"timed_out": True, "_shards": {"failed": 0}}, True),
        ({"timed_out": False, "_shards": {"failed": 1}}, True),
    ):
        engine = ElasticsearchEngine("elasticsearch+http://127.0.0.1:9200")
        engine._request = answering(partial)  # type: ignore[method-assign]
        calls: tuple[Callable[[], object], ...] = (
            functools.partial(engine.select_rows, "note", plan),
            functools.partial(engine.search_rows, "note", plan, ("x",), ()),
            functools.partial(engine.count_rows, "note", plan),
            engine.map_watermark,
        )
        for call in calls:
            if refused:
                with pytest.raises(sde.EngineError, match="partial"):
                    call()
            else:
                call()
    searches = [path for path in sent if "_search" in path]
    assert searches
    assert all(path.endswith("?allow_partial_search_results=false") for path in searches)


def test_a_refused_write_names_the_error_type_and_never_the_value() -> None:
    """The engine's reason for refusing a document can quote its value (second review, item 5)."""
    from sde.schema import elasticsearch_mapping

    layout = sde.PhysicalLayout(tables={"Note": "note"},
                                columns={"Note": {"id": "long", "body": "keyword"}})
    mapping = elasticsearch_mapping(layout, "Note", {"Note": ["id"]})
    refusal = {"error": {"type": "document_parsing_exception",
                         "reason": "Preview of field's value: 'secret'"}}

    def request(method: str, path: str, body: Any = None, **_: Any) -> tuple[int, Any]:
        if path.endswith("/_mapping"):
            return 200, {"note": {"mappings": mapping}}
        return 400, refusal

    engine = ElasticsearchEngine("elasticsearch+http://127.0.0.1:9200")
    engine._request = request  # type: ignore[method-assign]
    row = {"id": 1, "body": "secret"}
    for call in (functools.partial(engine.insert, "note", row),
                 functools.partial(engine.insert_many, "note", [row])):
        with pytest.raises(sde.EngineError) as caught:
            call()
        assert "document_parsing_exception" in str(caught.value)
        assert "secret" not in str(caught.value)



FENCE_PROCESSORS = Path(__file__).resolve().parents[2] / "conformance" / "fence-processors.json"
"""Written by conformance/tools/elasticsearch_vectors.py and read by the TypeScript tests too."""


def test_a_fence_pipeline_renders_the_same_processor_in_both_languages() -> None:
    from sde.engines._write_fences import fence_processor

    table = json.loads(FENCE_PROCESSORS.read_text(encoding="utf-8"))
    assert len(table["cases"]) == 5
    for name, constraints in table["cases"].items():
        assert fence_processor(constraints) == table["processors"][name], name


class _FenceTransport:
    """An Elasticsearch that answers the fence backend's reads from a dict, and records writes."""

    def __init__(self, *, final_pipeline: str | None = None, version: int | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.final_pipeline = final_pipeline
        self.version = version

    def __call__(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        self.sent.append((method, path))
        if path == "/notes/_settings" and method == "GET":
            index: dict[str, Any] = {"uuid": "u1"}
            if self.final_pipeline is not None:
                index["final_pipeline"] = self.final_pipeline
            return 200, {"notes": {"settings": {"index": index}}}
        if path == "/notes/_mapping":
            return 200, {"notes": {"mappings": {"properties": {"id": {"type": "long"}}}}}
        if path.startswith("/_ingest/pipeline/") and method == "GET":
            if self.version is None:
                return 404, {}
            from sde.engines._write_fences import fence_processor

            return 200, {"__sde_f_notes": {
                "version": self.version,
                "_meta": {"sde": {"contract": 1, "index": "notes", "uuid": "u1",
                                  "constraints": {}}},
                "processors": [fence_processor({})],
            }}
        return 200, {"acknowledged": True}


@pytest.mark.parametrize("name", ["a/../_cluster", "Upper", "_x", "a b", "a?x=1"])
def test_a_fence_refuses_a_name_that_is_not_an_index_before_any_request(name: str) -> None:
    from sde.engines._write_fences import ElasticsearchFences

    transport = _FenceTransport()
    fences = ElasticsearchFences(transport)
    calls = (
        lambda: fences.metadata(name),
        lambda: fences.add_column(name),
        lambda: fences.add_constraint(name, "__sde_f_setup", "0"),
        lambda: fences.drop_constraint(name, "__sde_f_setup"),
        lambda: fences.drain(name, project_id="0" * 32, hold="setup"),
    )
    for call in calls:
        with pytest.raises(sde.MigrationRefused, match="not an Elasticsearch index name"):
            call()
    assert transport.sent == []


def test_a_fence_is_not_installed_beside_another_final_pipeline() -> None:
    from sde.engines._write_fences import ElasticsearchFences

    with pytest.raises(sde.MigrationRefused, match="runs another final pipeline"):
        ElasticsearchFences(_FenceTransport(final_pipeline="theirs")).metadata("notes")


def test_a_fence_change_carries_the_version_it_read() -> None:
    """A second, uncoordinated executor is refused (409), not overwritten."""
    from sde.engines._write_fences import ElasticsearchFences

    transport = _FenceTransport(version=5)
    ElasticsearchFences(transport).add_constraint("notes", "__sde_f_setup", "0")
    puts = [path for method, path in transport.sent if method == "PUT"]
    assert "/_ingest/pipeline/__sde_f_notes?if_version=5" in puts


def _bookkeeping_engine(answers: dict[tuple[str, str], tuple[int, Any]]) -> tuple[Any, list[Any]]:
    engine = ElasticsearchEngine("elasticsearch+http://127.0.0.1:9200")
    sent: list[Any] = []

    def request(method: str, path: str, body: Any = None, **_: Any) -> tuple[int, Any]:
        sent.append((method, path))
        answer = answers.get((method, path.split("?")[0]))
        if answer is None:
            return 200, {"hits": {"hits": []}, "timed_out": False, "_shards": {"failed": 0}}
        if isinstance(answer, list):
            return answer.pop(0)  # type: ignore[no-any-return]
        return answer

    engine._request = request  # type: ignore[method-assign]
    return engine, sent


def test_bookkeeping_is_read_before_anything_creates_it() -> None:
    """A login that only reads and writes `sde_map_state` needs no `create_index`: it is created
    only where it is missing, as the SQL adapters look in their catalogue before CREATE."""
    from sde.engines.elasticsearch import BACKFILL_MAPPING

    watermark = {"sde_map_state": {"mappings": WATERMARK_MAPPING}}
    engine, sent = _bookkeeping_engine({
        ("GET", "/sde_map_state/_mapping"): (200, watermark),
        ("GET", "/sde_backfill_state/_mapping"): [
            (404, {}), (200, {"sde_backfill_state": {"mappings": BACKFILL_MAPPING}})],
        ("PUT", "/sde_backfill_state"): (400, {"error": {
            "type": "resource_already_exists_exception"}}),
    })
    assert engine.map_watermark() is None
    assert ("PUT", "/sde_map_state") not in sent
    # Missing, then created by somebody else in between: what they created is read and compared.
    assert engine.backfill_marker(materialization="copy", entity="Note") == 0
    assert [entry for entry in sent if "sde_backfill_state" in entry[1]][:3] == [
        ("GET", "/sde_backfill_state/_mapping"), ("PUT", "/sde_backfill_state"),
        ("GET", "/sde_backfill_state/_mapping")]


@pytest.mark.parametrize("name", ["sde_map_state", "sde_backfill_state"])
def test_bookkeeping_under_another_mapping_is_refused_rather_than_read(name: str) -> None:
    """`map_version` as a keyword would make the watermark the lexicographic maximum."""
    other = {"dynamic": "strict", "properties": {"map_version": {"type": "keyword"}}}
    engine, sent = _bookkeeping_engine({("GET", f"/{name}/_mapping"): (200, {name: {
        "mappings": other}})})
    call = engine.map_watermark if name == "sde_map_state" else functools.partial(
        engine.backfill_marker, materialization="copy", entity="Note")
    with pytest.raises(sde.EngineError, match="mapping this library would not create"):
        call()
    assert all(method == "GET" for method, _ in sent)


def test_a_staging_marker_is_accepted_beside_the_mapping_and_nothing_else_is() -> None:
    """A staging's index names its creator in `_meta.sde.stage`, in the protocol's form."""
    from sde.schema import elasticsearch_mapping

    layout = sde.PhysicalLayout(tables={"Note": "note"}, columns={"Note": {"id": "long"}})
    marker = "sde.stage1." + "1" * 32 + "." + "2" * 32 + "." + "3" * 16
    for stage, accepted in ((marker, True), (marker + "x", False), (7, False),
                            ("sde.stage1.short", False)):
        mapping = elasticsearch_mapping(layout, "Note", {"Note": ["id"]})
        mapping["_meta"]["sde"]["stage"] = stage
        engine, _ = _bookkeeping_engine({("GET", "/note/_mapping"): (200, {"note": {
            "mappings": mapping}})})
        if accepted:
            assert engine.validate_schema(layout, keys={"Note": ["id"]}) == ()
        else:
            with pytest.raises(sde.EngineError, match="staging marker"):
                engine.validate_schema(layout, keys={"Note": ["id"]})


def test_a_barrier_makes_what_it_drained_visible_before_it_returns() -> None:
    """The refresh comes after the block every shard confirmed, and a failed one is an error."""
    from sde.engines._write_fences import ElasticsearchFences

    class Draining(_FenceTransport):
        def __init__(self, refreshed: Any) -> None:
            super().__init__()
            self.refreshed = refreshed

        def __call__(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
            if path == "/notes/_block/write":
                self.sent.append((method, path))
                return 200, {"acknowledged": True, "shards_acknowledged": True,
                             "indices": [{"name": "notes", "blocked": True}]}
            if path == "/notes/_refresh":
                self.sent.append((method, path))
                return self.refreshed  # type: ignore[no-any-return]
            return super().__call__(method, path, body)

    failed = Draining((200, {"_shards": {"total": 2, "successful": 0, "failed": 1}}))
    with pytest.raises(sde.EngineError, match="could not be made visible"):
        ElasticsearchFences(failed).drain("notes", project_id="0" * 32, hold="a" * 32)
    order = [path for _, path in failed.sent if path.endswith(("_block/write", "_refresh"))]
    assert order == ["/notes/_block/write", "/notes/_refresh"]

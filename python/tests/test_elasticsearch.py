"""The Elasticsearch adapter without a server: its connection profile, its values and its _id."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

import sde
from sde.engines.elasticsearch import (
    KEYWORD_BYTES,
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
        ("elasticsearch+https://host/?connect_timeout=nan", "between"),
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
    assert facts["transactions"] is False and facts["write_generations"] is False
    assert facts["reads"] == {
        "get": True, "scan": True, "count": True, "summarize": False, "search": True,
    }
    assert facts["text_index"] is True
    assert not hasattr(ElasticsearchEngine, "write_fence")
    assert not hasattr(ElasticsearchEngine, "summarize_rows")
    assert callable(ElasticsearchEngine.search_rows)
    with pytest.raises(sde.EngineError, match="no multi-document transactions"):
        ElasticsearchEngine("elasticsearch+http://127.0.0.1:9200").transaction()

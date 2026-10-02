"""Each engine fact against the adapter it describes: a fact that drifts from the code fails here.

The facts are what the control plane tells a model about an engine before it decides placement
(``sde/facts.py``). What only a running engine can show - PostgreSQL opening a transaction, and what
each engine does with a second save under one key - is held by ``test_engine_facts_live.py`` and by
the orderbook slices; the orderbook's scan and write unit by ``test_orderbook_tcp.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

import sde
from sde.engines.clickhouse import ClickHouseEngine
from sde.engines.orderbook import OrderbookEngine
from sde.engines.postgres import PostgresEngine
from sde.errors import EngineError
from sde.query import QueryRefused, count_engine, query_engine, summary_engine


def adapters() -> dict[str, Any]:
    """One unconnected adapter per dialect. Nothing here needs a connection, and none is opened."""
    return {
        "postgres": PostgresEngine("postgresql://unused@127.0.0.1:1/unused"),
        "clickhouse": ClickHouseEngine("clickhouse://unused@127.0.0.1:1/unused"),
        "orderbook": OrderbookEngine("/nonexistent/sde-facts"),
    }


def test_there_are_facts_for_exactly_the_dialects_this_library_has_adapters_for() -> None:
    assert set(adapters()) == set(sde.DIALECTS)
    for dialect in sde.DIALECTS:
        facts = sde.engine_facts(dialect)
        assert facts["dialect"] == dialect
        assert facts["facts_version"] == sde.FACTS_VERSION
    with pytest.raises(ValueError, match="no engine facts for dialect 'mysql'"):
        sde.engine_facts("mysql")


def test_the_document_is_the_callers_to_change() -> None:
    facts = sde.engine_facts("orderbook")
    facts["fixed_shape"]["fields"]["price"] = "decimal(12,2)"
    facts["reads"]["count"] = True
    again = sde.engine_facts("orderbook")
    assert again["fixed_shape"]["fields"]["price"] == "int64"
    assert again["reads"]["count"] is False


def test_a_fixed_schema_is_the_layouts_own_shape_and_its_eligibility_rule() -> None:
    for dialect in sde.DIALECTS:
        facts = sde.engine_facts(dialect)
        assert (facts["schema"] == "fixed") is (dialect in sde.FIXED_SCHEMA)
        assert ("fixed_shape" in facts) is (facts["schema"] == "fixed")
    shape = sde.engine_facts("orderbook")["fixed_shape"]
    assert shape == {
        "table": sde.ORDERBOOK_TABLE,
        "entities": 1,
        "fields": dict(sde.ORDERBOOK_SHAPE),
        "key": list(sde.ORDERBOOK_KEY),
    }
    fields = dict(shape["fields"])
    assert sde.fixed_schema_mismatch({"DepthLevel": fields}, dialect="orderbook") is None
    two = sde.fixed_schema_mismatch({"A": fields, "B": fields}, dialect="orderbook")
    assert two is not None and "stores one thing" in two
    decimal = sde.fixed_schema_mismatch(
        {"DepthLevel": {**fields, "price": "decimal(12,2)"}}, dialect="orderbook"
    )
    assert decimal is not None and "price" in decimal


def test_write_generations_are_the_adapters_write_fences() -> None:
    for dialect, adapter in adapters().items():
        fences = callable(getattr(adapter, "write_fence", None))
        assert sde.engine_facts(dialect)["write_generations"] is fences, dialect


def test_an_engine_without_transactions_refuses_one_at_opening() -> None:
    for dialect, adapter in adapters().items():
        if sde.engine_facts(dialect)["transactions"]:
            continue  # PostgreSQL opens one only on a connection: test_engine_facts_live.py
        with pytest.raises(EngineError, match="no multi-statement transactions"):
            adapter.transaction()
    assert [d for d in sde.DIALECTS if sde.engine_facts(d)["transactions"]] == ["postgres"]


def test_reads_are_what_the_query_layer_accepts_from_each_adapter() -> None:
    checks = {"get": query_engine, "scan": query_engine, "count": count_engine}
    checks["summarize"] = summary_engine
    for dialect, adapter in adapters().items():
        reads = sde.engine_facts(dialect)["reads"]
        assert set(reads) == set(checks), dialect
        for read, check in checks.items():
            if reads[read]:
                check(adapter)
            else:
                with pytest.raises(QueryRefused, match="over its history"):
                    check(adapter)

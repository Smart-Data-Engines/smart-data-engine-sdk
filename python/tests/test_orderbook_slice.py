"""The orderbook engine, live, and the four measurements the fake in the adapter tests encodes.

`test_orderbook_adapter.py` tests everything the adapter *decides*, against a fake, so it runs
everywhere. What a fake cannot check is whether the engine still behaves the way it did when the
adapter was written - and four of those behaviours are load-bearing:

  1. a write is invisible to a query until `flush()`;
  2. a single-level write always lands at level 0, because level is an index inside an update;
  3. two writes with the same key both persist, with the same sequence number too;
  4. a sequence number round-trips, so the 0-means-unknown conversion is about *old libraries*
     rather than about every row.

Each of those is an assertion here. If the engine changes, this file fails and the fake stops being
a description of something true - which is the failure mode a fake normally hides.

    cmake -S . -B build && cmake --build build -j$(nproc)
    OB_LIB_PATH=$PWD/build/liborderbook_shared.so \\
    PYTHONPATH=$PWD/python \\
    SDE_ORDERBOOK=1 pytest tests/test_orderbook_slice.py
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

import sde
from sde.engines.orderbook import OrderbookEngine
from sde.errors import EngineError
from sde.testing.loader import model_from_neutral

ENABLED = os.environ.get("SDE_ORDERBOOK") == "1"

pytestmark = pytest.mark.skipif(
    not ENABLED,
    reason="set SDE_ORDERBOOK=1, OB_LIB_PATH and PYTHONPATH to run the orderbook slice. The "
    "engine's Python client is not on PyPI and its shared library is built from C++, so this "
    "cannot run everywhere - which is why the adapter's own decisions are tested against a fake "
    "and only the engine's measured behaviour is tested here",
)

SYMBOL = "BTCUSDT"
EXCHANGE = "binance"
TS = 1_735_689_600_000_000_000


@pytest.fixture()
def model() -> sde.LogicalModel:
    """An entity that *is* the orderbook shape, because nothing else can be placed there."""
    sde.clear_registry()
    return model_from_neutral(
        {
            "name": "market",
            "entities": [
                {
                    "name": "DepthLevel",
                    "fields": [
                        {
                            "name": name,
                            "type": kind,
                            "nullable": name == "sequence_number",
                        }
                        for name, kind in sde.ORDERBOOK_SHAPE.items()
                    ],
                    "key": list(sde.ORDERBOOK_KEY),
                }
            ],
            "relations": [],
            "atomic": [],
        }
    )


@pytest.fixture()
def engine(tmp_path: Any) -> Iterator[OrderbookEngine]:
    adapter = OrderbookEngine(str(tmp_path / "ob"))
    adapter.connect()
    try:
        yield adapter
    finally:
        adapter.close()


@pytest.fixture()
def prepared(engine: OrderbookEngine, model: sde.LogicalModel) -> OrderbookEngine:
    group = sde.colocation_groups(model)[0]
    layout = sde.default_layout(model, group, dialect="orderbook")
    engine.ensure_schema(layout, keys={"DepthLevel": sde.ORDERBOOK_KEY})
    return engine


def _values(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "symbol": SYMBOL,
        "exchange": EXCHANGE,
        "timestamp_ns": TS,
        "side": "bid",
        "level": 0,
        "price": 5_000_000,
        "quantity": 3,
        "order_count": 1,
        "sequence_number": 41,
    }
    values.update(overrides)
    return values


# ── The four measurements ───────────────────────────────────────────────────────────────────────


def test_a_write_is_invisible_until_flush(prepared: OrderbookEngine) -> None:
    """Measurement 1, and the reason reads flush lazily instead of trusting the caller."""
    prepared.insert_levels(
        sde.ORDERBOOK_TABLE,
        symbol=SYMBOL,
        exchange=EXCHANGE,
        side="bid",
        timestamp_ns=TS,
        levels=((5_000_000, 3, 1),),
    )
    # Straight at the client library, bypassing the adapter's lazy flush, because that is the
    # behaviour being measured.
    raw = prepared._ob.query(f"SELECT * FROM '{SYMBOL}'.'{EXCHANGE}'")
    assert raw == [], "if this ever returns the row, the lazy flush on reads is dead weight"

    assert len(prepared.levels(symbol=SYMBOL, exchange=EXCHANGE)) == 1


def test_a_single_level_write_lands_at_level_zero(prepared: OrderbookEngine) -> None:
    """Measurement 2, and the reason `insert` refuses any other level.

    The engine's write API has no level parameter: a price's level is its index within one update.
    So writing a row that says level 3 would store it at 0 and the read would disagree with the
    write, which is why the adapter refuses instead.
    """
    prepared.insert(sde.ORDERBOOK_TABLE, _values(level=0))
    rows = prepared.levels(symbol=SYMBOL, exchange=EXCHANGE)
    assert [row["level"] for row in rows] == [0]

    with pytest.raises(EngineError, match="has no level parameter"):
        prepared.insert(sde.ORDERBOOK_TABLE, _values(level=3, timestamp_ns=TS + 1))


def test_a_multi_level_update_numbers_levels_by_position(prepared: OrderbookEngine) -> None:
    prepared.insert_levels(
        sde.ORDERBOOK_TABLE,
        symbol=SYMBOL,
        exchange=EXCHANGE,
        side="bid",
        timestamp_ns=TS,
        levels=((5_000_000, 3, 1), (4_999_900, 7, 2), (4_999_800, 11, 4)),
    )
    rows = prepared.levels(symbol=SYMBOL, exchange=EXCHANGE)
    assert [(row["level"], row["price"]) for row in rows] == [
        (0, 5_000_000),
        (1, 4_999_900),
        (2, 4_999_800),
    ]


def test_two_writes_with_the_same_key_both_persist(prepared: OrderbookEngine) -> None:
    """Measurement 3, and the reason `get` refuses rather than picking one.

    This engine is an append-only log of depth updates and does not enforce a key - not even with
    the same sequence number. ClickHouse has the same absence and a way out (`FINAL` over
    `ReplacingMergeTree`); here there is none, so a `get` that answered with one of these would be
    a read that lies about uniqueness and the client could not see it happen.
    """
    for price in (5_000_000, 4_999_900):
        prepared.insert_levels(
            sde.ORDERBOOK_TABLE,
            symbol=SYMBOL,
            exchange=EXCHANGE,
            side="bid",
            timestamp_ns=TS,
            levels=((price, 1, 1),),
            sequence_number=41,
        )
    rows = prepared.levels(symbol=SYMBOL, exchange=EXCHANGE)
    assert len(rows) == 2
    assert {row["level"] for row in rows} == {0}, "both at level 0: the key is violated"

    key = {
        "symbol": SYMBOL,
        "exchange": EXCHANGE,
        "timestamp_ns": TS,
        "side": "bid",
        "level": 0,
    }
    with pytest.raises(EngineError, match="2 rows in orderbook share the key"):
        prepared.get(sde.ORDERBOOK_TABLE, key)


def test_a_sequence_number_round_trips(prepared: OrderbookEngine) -> None:
    """Measurement 4.

    The 0-means-unknown conversion has to be about an *old* shared library, not about every row.
    If this fails, either the library predates the seven-field cursor - `ob_result_next_seq`, added
    with engine issue #65 - or the engine stopped storing the number, and the difference matters:
    the first is a stale build and the second would make the field useless.
    """
    prepared.insert(sde.ORDERBOOK_TABLE, _values(sequence_number=41))
    rows = prepared.levels(symbol=SYMBOL, exchange=EXCHANGE)
    assert rows[0]["sequence_number"] == 41, (
        "a stale liborderbook_shared.so without ob_result_next_seq reads every sequence number as "
        "0; rebuild before concluding the engine changed"
    )


# ── The adapter against the engine ──────────────────────────────────────────────────────────────


def test_the_fixed_shape_is_accepted_and_nothing_is_created(
    engine: OrderbookEngine, model: sde.LogicalModel
) -> None:
    group = sde.colocation_groups(model)[0]
    layout = sde.default_layout(model, group, dialect="orderbook")
    engine.ensure_schema(layout, keys={"DepthLevel": sde.ORDERBOOK_KEY})
    assert (
        sde.schema_statements(layout, keys={"DepthLevel": sde.ORDERBOOK_KEY}, dialect="orderbook")
        == ()
    )


def test_a_row_written_through_the_map_comes_back_with_every_field(
    prepared: OrderbookEngine,
) -> None:
    prepared.insert(sde.ORDERBOOK_TABLE, _values())
    row = prepared.get(
        sde.ORDERBOOK_TABLE,
        {
            "symbol": SYMBOL,
            "exchange": EXCHANGE,
            "timestamp_ns": TS,
            "side": "bid",
            "level": 0,
        },
    )
    assert row == _values()


def test_a_range_read_is_addressed_by_symbol_and_bounded_by_time(
    prepared: OrderbookEngine,
) -> None:
    for offset in range(5):
        prepared.insert(sde.ORDERBOOK_TABLE, _values(timestamp_ns=TS + offset, price=100 + offset))

    within = prepared.levels(symbol=SYMBOL, exchange=EXCHANGE, start_ns=TS + 1, end_ns=TS + 3)
    assert [row["timestamp_ns"] for row in within] == [TS + 1, TS + 2, TS + 3], (
        "both ends inclusive, because the engine's BETWEEN is"
    )

    capped = prepared.levels(symbol=SYMBOL, exchange=EXCHANGE, limit=2)
    assert len(capped) == 2


def test_another_book_is_a_different_address_and_not_a_filter(prepared: OrderbookEngine) -> None:
    """There is no scan across books here, and that is a property rather than a limitation."""
    prepared.insert(sde.ORDERBOOK_TABLE, _values())
    prepared.insert(sde.ORDERBOOK_TABLE, _values(symbol="ETHUSDT", price=300_000))

    assert len(prepared.levels(symbol=SYMBOL, exchange=EXCHANGE)) == 1
    assert len(prepared.levels(symbol="ETHUSDT", exchange=EXCHANGE)) == 1


def test_transactions_are_refused_against_the_real_engine_too(prepared: OrderbookEngine) -> None:
    refused = pytest.raises(EngineError, match="no multi-statement transactions")
    with refused, prepared.transaction():
        pass

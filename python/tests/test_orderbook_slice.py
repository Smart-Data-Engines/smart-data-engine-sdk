"""The orderbook engine, live, and the measurements the fakes in the adapter tests encode.

``test_orderbook_adapter.py`` and ``test_orderbook_tcp.py`` test everything the adapter *decides*,
against fakes, so they run everywhere. What a fake cannot check is whether the engine still behaves
the way it did when the adapter was written - and these behaviours are load-bearing:

  1. a write is invisible to a query until a flush (in-process; a server also flushes on its own
     tick, so over TCP the same fact is that a read must ask for one);
  2. a single-level write always lands at level 0, because level is an index inside an update;
  3. two writes with the same key both persist;
  4. in-process a chosen sequence number round-trips; over TCP the server numbers every update of a
     book itself and refuses a number the client chose;
  5. a scan answers in arrival order, not in event-time order, and ``LIMIT`` keeps the first rows
     to arrive;
  6. a book nothing has been written to answers ``OB_ERR_NOT_FOUND``.

Each of those is an assertion here, in each mode the engine has. If the engine changes, this file
fails and the fakes stop being a description of something true - which is the failure mode a fake
normally hides. How to run it is in ``_orderbook_live.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from _orderbook_live import ENABLED, MODES, REASON, fresh_book, open_engine

import sde
from sde.engines.orderbook import OrderbookEngine
from sde.errors import EngineError
from sde.testing.loader import model_from_neutral

pytestmark = pytest.mark.skipif(not ENABLED, reason=REASON)

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


@pytest.fixture(params=MODES)
def engine(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[OrderbookEngine]:
    adapter = open_engine(request.param, tmp_path)
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


@pytest.fixture()
def symbol() -> str:
    return fresh_book()


def _values(symbol: str, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "symbol": symbol,
        "exchange": EXCHANGE,
        "timestamp_ns": TS,
        "side": "bid",
        "level": 0,
        "price": 5_000_000,
        "quantity": 3,
        "order_count": 1,
        "sequence_number": None,
    }
    values.update(overrides)
    return values


def _key(symbol: str, **overrides: Any) -> dict[str, Any]:
    values = _values(symbol, **overrides)
    return {name: values[name] for name in sde.ORDERBOOK_KEY}


# ── The measurements ────────────────────────────────────────────────────────────────────────────


def test_a_write_is_invisible_until_flush(prepared: OrderbookEngine, symbol: str) -> None:
    """Measurement 1, and the reason reads flush lazily instead of trusting the caller.

    In-process only: a server also flushes on its own tick (100 ms by default), so over TCP the
    raw read is a race - and the adapter flushes before every read that follows a write in both
    modes, which the second assertion holds in each.
    """
    prepared.insert_levels(
        sde.ORDERBOOK_TABLE,
        symbol=symbol,
        exchange=EXCHANGE,
        side="bid",
        timestamp_ns=TS,
        levels=((5_000_000, 3, 1),),
    )
    if prepared.mode == "local":
        # Straight at the client library, bypassing the adapter's lazy flush, because that is the
        # behaviour being measured.
        raw = prepared._ob.query(f"SELECT * FROM '{symbol}'.'{EXCHANGE}'")
        assert raw == [], "if this ever returns the row, the lazy flush on reads is dead weight"

    assert len(prepared.levels(symbol=symbol, exchange=EXCHANGE)) == 1


def test_a_single_level_write_lands_at_level_zero(prepared: OrderbookEngine, symbol: str) -> None:
    """Measurement 2, and the reason `insert` refuses any other level.

    The engine's write API has no level parameter: a price's level is its index within one update.
    So writing a row that says level 3 would store it at 0 and the read would disagree with the
    write, which is why the adapter refuses instead.
    """
    prepared.insert(sde.ORDERBOOK_TABLE, _values(symbol, level=0))
    rows = prepared.levels(symbol=symbol, exchange=EXCHANGE)
    assert [row["level"] for row in rows] == [0]

    with pytest.raises(EngineError, match="has no level parameter"):
        prepared.insert(sde.ORDERBOOK_TABLE, _values(symbol, level=3, timestamp_ns=TS + 1))


def test_a_multi_level_update_numbers_levels_by_position(
    prepared: OrderbookEngine, symbol: str
) -> None:
    prepared.insert_levels(
        sde.ORDERBOOK_TABLE,
        symbol=symbol,
        exchange=EXCHANGE,
        side="bid",
        timestamp_ns=TS,
        levels=((5_000_000, 3, 1), (4_999_900, 7, 2), (4_999_800, 11, 4)),
    )
    rows = prepared.levels(symbol=symbol, exchange=EXCHANGE)
    assert [(row["level"], row["price"]) for row in rows] == [
        (0, 5_000_000),
        (1, 4_999_900),
        (2, 4_999_800),
    ]


def test_two_writes_with_the_same_key_both_persist(prepared: OrderbookEngine, symbol: str) -> None:
    """Measurement 3, and the reason `get` refuses rather than picking one.

    This engine is an append-only log of depth updates and does not enforce a key. ClickHouse has
    the same absence and a way out (`FINAL` over `ReplacingMergeTree`); here there is none, so a
    `get` that answered with one of these would be a read that lies about uniqueness and the client
    could not see it happen.
    """
    for price in (5_000_000, 4_999_900):
        prepared.insert_levels(
            sde.ORDERBOOK_TABLE,
            symbol=symbol,
            exchange=EXCHANGE,
            side="bid",
            timestamp_ns=TS,
            levels=((price, 1, 1),),
            sequence_number=41 if prepared.mode == "local" else None,
        )
    rows = prepared.levels(symbol=symbol, exchange=EXCHANGE)
    assert len(rows) == 2
    assert {row["level"] for row in rows} == {0}, "both at level 0: the key is violated"
    # What the control plane tells a model about this engine (sde/facts.py) says the same.
    assert sde.engine_facts("orderbook")["key"] == "not_enforced"

    with pytest.raises(EngineError, match="2 rows in orderbook share the key"):
        prepared.get(sde.ORDERBOOK_TABLE, _key(symbol))


def test_the_sequence_number_is_the_clients_in_process_and_the_servers_over_tcp(
    prepared: OrderbookEngine, symbol: str
) -> None:
    """Measurement 4, in both of its forms.

    In-process the number round-trips, so the 0-means-unknown conversion is about an *old* shared
    library - one without ``ob_result_next_seq``, added with engine issue #65 - and not about every
    row. Over TCP the server numbers every update of a book itself: the two updates here come back
    with two different numbers, and the adapter refuses to send one of its own.
    """
    if prepared.mode == "local":
        prepared.insert(sde.ORDERBOOK_TABLE, _values(symbol, sequence_number=41))
        rows = prepared.levels(symbol=symbol, exchange=EXCHANGE)
        assert rows[0]["sequence_number"] == 41, (
            "a stale liborderbook_shared.so without ob_result_next_seq reads every sequence number "
            "as 0; rebuild before concluding the engine changed"
        )
        return
    prepared.insert(sde.ORDERBOOK_TABLE, _values(symbol))
    prepared.insert(sde.ORDERBOOK_TABLE, _values(symbol, side="ask", price=5_000_100))
    numbers = [row["sequence_number"] for row in prepared.levels(symbol=symbol, exchange=EXCHANGE)]
    assert len(numbers) == 2 and all(isinstance(n, int) and n >= 1 for n in numbers)
    assert numbers[0] != numbers[1], "one number per update"
    with pytest.raises(EngineError, match="sequence number is the server's"):
        prepared.insert(sde.ORDERBOOK_TABLE, _values(symbol, timestamp_ns=TS + 5,
                                                     sequence_number=41))
    assert len(prepared.levels(symbol=symbol, exchange=EXCHANGE)) == 2, "nothing was sent"


def test_a_scan_of_the_engine_answers_in_arrival_order(
    prepared: OrderbookEngine, symbol: str
) -> None:
    """Measurement 5: the reason `select_rows` assembles pages from windows of time.

    Three updates written with event times 3000, 2000 and 1000 nanoseconds past ``TS`` come back
    in that order, and the engine's LIMIT keeps the first to arrive - so a LIMIT is not a page in
    key order, and a page built on it would skip rows.
    """
    for offset in (3_000, 2_000, 1_000):
        prepared.insert(sde.ORDERBOOK_TABLE, _values(symbol, timestamp_ns=TS + offset))
    stored = prepared.levels(symbol=symbol, exchange=EXCHANGE)
    assert [row["timestamp_ns"] - TS for row in stored] == [3_000, 2_000, 1_000]
    first = prepared.levels(symbol=symbol, exchange=EXCHANGE, limit=1)
    assert [row["timestamp_ns"] - TS for row in first] == [3_000], "LIMIT keeps the first to arrive"


def test_a_book_nobody_has_written_to_is_named_by_the_server_and_not_in_process(
    prepared: OrderbookEngine,
) -> None:
    """Measurement 6, and the two answers the two modes force.

    Over TCP the server answers OB_ERR_NOT_FOUND, and the adapter reads an empty book. In-process
    the C API keeps no reason for a failed query, so the client raises the same "Query failed" for
    an unknown book as for any failure - and the adapter will not read a failure as an empty book.
    """
    unknown = fresh_book()
    query = f"SELECT * FROM '{unknown}'.'{EXCHANGE}' WHERE timestamp BETWEEN 0 AND 1"
    if prepared.mode == "tcp":
        with pytest.raises(Exception, match=r"OB_ERR_NOT_FOUND"):
            prepared._ob.query(query)
        assert prepared.levels(symbol=unknown, exchange=EXCHANGE) == []
        assert prepared.get(sde.ORDERBOOK_TABLE, _key(unknown)) is None
        return
    with pytest.raises(Exception, match=r"Query failed"):
        prepared._ob.query(query)
    with pytest.raises(EngineError, match="cannot be told apart from a failure"):
        prepared.get(sde.ORDERBOOK_TABLE, _key(unknown))


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
    prepared: OrderbookEngine, symbol: str
) -> None:
    written = _values(symbol, sequence_number=41 if prepared.mode == "local" else None)
    prepared.insert(sde.ORDERBOOK_TABLE, written)
    row = prepared.get(sde.ORDERBOOK_TABLE, _key(symbol))
    assert row is not None
    if prepared.mode == "local":
        assert row == written
    else:
        number = row.pop("sequence_number")
        assert isinstance(number, int) and number >= 1, "the server's number, not unknown"
        assert row == {name: value for name, value in written.items() if name != "sequence_number"}


def test_a_range_read_is_addressed_by_symbol_and_bounded_by_time(
    prepared: OrderbookEngine, symbol: str
) -> None:
    for offset in range(5):
        prepared.insert(
            sde.ORDERBOOK_TABLE, _values(symbol, timestamp_ns=TS + offset, price=100 + offset)
        )

    within = prepared.levels(symbol=symbol, exchange=EXCHANGE, start_ns=TS + 1, end_ns=TS + 3)
    assert [row["timestamp_ns"] for row in within] == [TS + 1, TS + 2, TS + 3], (
        "both ends inclusive, because the engine's BETWEEN is"
    )

    capped = prepared.levels(symbol=symbol, exchange=EXCHANGE, limit=2)
    assert len(capped) == 2


def test_another_book_is_a_different_address_and_not_a_filter(
    prepared: OrderbookEngine, symbol: str
) -> None:
    """There is no scan across books here, and that is a property rather than a limitation."""
    other = fresh_book()
    prepared.insert(sde.ORDERBOOK_TABLE, _values(symbol))
    prepared.insert(sde.ORDERBOOK_TABLE, _values(other, price=300_000))

    assert len(prepared.levels(symbol=symbol, exchange=EXCHANGE)) == 1
    assert len(prepared.levels(symbol=other, exchange=EXCHANGE)) == 1


def test_transactions_are_refused_against_the_real_engine_too(prepared: OrderbookEngine) -> None:
    assert sde.engine_facts("orderbook")["transactions"] is False
    refused = pytest.raises(EngineError, match="no multi-statement transactions")
    with refused, prepared.transaction():
        pass

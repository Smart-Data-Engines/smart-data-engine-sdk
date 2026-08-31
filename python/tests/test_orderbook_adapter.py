"""The orderbook adapter, against a fake at the client-library boundary.

The split between this file and `test_orderbook_slice.py` is deliberate and worth reading before
adding to either.

The engine's Python client is not on PyPI and its shared library has to be built from C++, so a live
test cannot run everywhere. Everything the adapter *decides* - the shape check, the refusal of a
non-zero level, the refusal on a key collision, unknown-not-zero for the sequence number, the
literal check on a symbol - is decided before the client library is called, so it is tested here and
runs everywhere.

The fake is not a second implementation of the engine. It records calls and replays rows, and the
behaviour it replays was **measured against the real engine first**: a write is invisible until
`flush()`, a single-level write always lands at level 0, and two writes with the same key both
persist. `test_orderbook_slice.py` re-checks those measurements against the engine itself, which is
what keeps this file from drifting into a description of an engine that no longer behaves that way.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from typing import Any

import pytest

import sde
from sde.engines.orderbook import OrderbookEngine
from sde.errors import EngineError


@dataclass
class _Row:
    timestamp_ns: int
    price: int
    quantity: int
    order_count: int
    side: str
    level: int
    sequence_number: int


class _FakeClient:
    """Records writes, replays rows, and refuses to be queried before a flush.

    `rows` is what the next query returns. `queries` and `writes` are what the adapter asked for.
    """

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []
        self.queries: list[str] = []
        self.flushes = 0
        self.rows: list[_Row] = []
        self.closed = False

    def insert(
        self,
        symbol: str,
        exchange: str,
        side: str,
        prices: list[int],
        qtys: list[int],
        counts: list[int],
        timestamp_ns: int | None = None,
        seq: int | None = None,
    ) -> int:
        self.writes.append(
            {
                "symbol": symbol,
                "exchange": exchange,
                "side": side,
                "prices": list(prices),
                "qtys": list(qtys),
                "counts": list(counts),
                "timestamp_ns": timestamp_ns,
                "seq": seq,
            }
        )
        return seq or 0

    def flush(self) -> None:
        self.flushes += 1

    def query(self, sql: str) -> list[_Row]:
        self.queries.append(sql)
        return list(self.rows)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    """Install a fake `orderbook_engine` module, so `connect()` finds it and nothing else does."""
    client = _FakeClient()
    module = types.ModuleType("orderbook_engine")
    module.OrderbookEngine = lambda *args, **kwargs: client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "orderbook_engine", module)
    return client


@pytest.fixture
def engine(fake: _FakeClient) -> OrderbookEngine:
    adapter = OrderbookEngine("/tmp/does-not-matter-the-client-is-fake")
    adapter.connect()
    return adapter


def _layout() -> sde.PhysicalLayout:
    return sde.PhysicalLayout(
        tables={"Depth": sde.ORDERBOOK_TABLE},
        columns={"Depth": {name: name for name in sde.ORDERBOOK_SHAPE}},
        indexes=(),
    )


def _row(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "symbol": "BTCUSDT",
        "exchange": "binance",
        "timestamp_ns": 1_735_689_600_000_000_000,
        "side": "bid",
        "level": 0,
        "price": 5_000_000,
        "quantity": 3,
        "order_count": 1,
        "sequence_number": 41,
    }
    values.update(overrides)
    return values


# ── Construction: two deployments, and no default between them ──────────────────────────────────


def test_a_data_directory_or_a_host_but_not_both_and_not_neither() -> None:
    """Defaulting would pick a durability guarantee on the client's behalf."""
    with pytest.raises(EngineError, match="Not both and not neither"):
        OrderbookEngine()
    with pytest.raises(EngineError, match="Not both and not neither"):
        OrderbookEngine("/tmp/x", host="h", port=1)
    with pytest.raises(EngineError, match="a host needs a port"):
        OrderbookEngine(host="h")


def test_an_absent_client_library_says_where_to_get_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "orderbook_engine", None)
    with pytest.raises(EngineError, match="not on PyPI"):
        OrderbookEngine("/tmp/x").connect()


# ── ensure_schema: verify, because there is nothing to create ───────────────────────────────────


def test_the_matching_layout_is_accepted_and_nothing_is_created(
    engine: OrderbookEngine, fake: _FakeClient
) -> None:
    engine.ensure_schema(_layout(), keys={"Depth": sde.ORDERBOOK_KEY})
    assert fake.writes == []
    assert fake.queries == []


def test_a_map_naming_another_table_was_built_for_another_engine(engine: OrderbookEngine) -> None:
    layout = sde.PhysicalLayout(
        tables={"Depth": "depth"},
        columns={"Depth": {name: name for name in sde.ORDERBOOK_SHAPE}},
        indexes=(),
    )
    with pytest.raises(EngineError, match="this engine's storage is 'orderbook'"):
        engine.ensure_schema(layout, keys={"Depth": sde.ORDERBOOK_KEY})


def test_a_layout_missing_a_column_is_refused_before_a_single_row_is_written(
    engine: OrderbookEngine,
) -> None:
    columns = {name: name for name in sde.ORDERBOOK_SHAPE}
    del columns["price"]
    layout = sde.PhysicalLayout(
        tables={"Depth": sde.ORDERBOOK_TABLE}, columns={"Depth": columns}, indexes=()
    )
    with pytest.raises(EngineError, match=r"missing \['price'\]"):
        engine.ensure_schema(layout, keys={"Depth": sde.ORDERBOOK_KEY})


def test_the_key_order_is_checked_because_it_is_how_a_query_reaches_the_data(
    engine: OrderbookEngine,
) -> None:
    reordered = ("timestamp_ns", "symbol", "exchange", "side", "level")
    with pytest.raises(EngineError, match="addresses rows by"):
        engine.ensure_schema(_layout(), keys={"Depth": reordered})


def test_a_group_of_two_is_refused_here_as_well_as_at_map_build(engine: OrderbookEngine) -> None:
    """Checked in the client's process too, which is the only place a map built by an older version
    of the control plane gets caught."""
    layout = sde.PhysicalLayout(
        tables={"Depth": sde.ORDERBOOK_TABLE, "Trade": "trade"},
        columns={"Depth": {name: name for name in sde.ORDERBOOK_SHAPE}, "Trade": {"id": "int64_t"}},
        indexes=(),
    )
    with pytest.raises(EngineError, match="this engine stores one thing"):
        engine.ensure_schema(layout, keys={"Depth": sde.ORDERBOOK_KEY})


# ── insert: a level is an index, not a column ───────────────────────────────────────────────────


def test_a_row_at_level_zero_is_written_faithfully(
    engine: OrderbookEngine, fake: _FakeClient
) -> None:
    engine.insert(sde.ORDERBOOK_TABLE, _row())
    assert fake.writes == [
        {
            "symbol": "BTCUSDT",
            "exchange": "binance",
            "side": "bid",
            "prices": [5_000_000],
            "qtys": [3],
            "counts": [1],
            "timestamp_ns": 1_735_689_600_000_000_000,
            "seq": 41,
        }
    ]


def test_a_row_at_any_other_level_is_refused_rather_than_stored_at_zero(
    engine: OrderbookEngine, fake: _FakeClient
) -> None:
    """The one failure a storage adapter must never produce quietly.

    `level` is not a parameter of the engine's write API - it is the index of a price inside one
    update - so this row would be stored at level 0 and read back at level 0. Measured against the
    real engine, which is why this is a refusal and not a worry.
    """
    with pytest.raises(EngineError, match="has no level parameter"):
        engine.insert(sde.ORDERBOOK_TABLE, _row(level=3))
    assert fake.writes == [], "nothing may reach the engine on this path"


def test_a_missing_field_is_refused_rather_than_defaulted(engine: OrderbookEngine) -> None:
    values = _row()
    del values["quantity"]
    with pytest.raises(EngineError, match=r"missing \['quantity'\]"):
        engine.insert(sde.ORDERBOOK_TABLE, values)


def test_an_unknown_sequence_number_is_passed_through_as_unknown(
    engine: OrderbookEngine, fake: _FakeClient
) -> None:
    """`None` in, `None` on the wire: the engine assigns its own. Not 0, which is its sentinel."""
    engine.insert(sde.ORDERBOOK_TABLE, _row(sequence_number=None))
    assert fake.writes[0]["seq"] is None


def test_insert_levels_writes_one_update_with_the_levels_in_order(
    engine: OrderbookEngine, fake: _FakeClient
) -> None:
    engine.insert_levels(
        sde.ORDERBOOK_TABLE,
        symbol="BTCUSDT",
        exchange="binance",
        side="bid",
        timestamp_ns=1000,
        levels=((100, 1, 1), (99, 2, 3)),
    )
    written = fake.writes[0]
    assert written["prices"] == [100, 99], "position in this list is the level"
    assert written["qtys"] == [1, 2]
    assert written["counts"] == [1, 3]


def test_an_update_with_no_levels_is_a_write_that_would_report_success_for_nothing(
    engine: OrderbookEngine,
) -> None:
    with pytest.raises(EngineError, match="no levels"):
        engine.insert_levels(
            sde.ORDERBOOK_TABLE,
            symbol="X",
            exchange="e",
            side="bid",
            timestamp_ns=1,
            levels=(),
        )


def test_an_unknown_side_is_refused(engine: OrderbookEngine) -> None:
    with pytest.raises(EngineError, match=r"side must be one of \['ask', 'bid'\]"):
        engine.insert_levels(
            sde.ORDERBOOK_TABLE,
            symbol="X",
            exchange="e",
            side="middle",
            timestamp_ns=1,
            levels=((1, 1, 1),),
        )


def test_a_write_failure_is_surfaced_and_logged_not_swallowed(
    engine: OrderbookEngine, fake: _FakeClient
) -> None:
    def boom(*_: Any, **__: Any) -> int:
        raise RuntimeError("disk full")

    fake.insert = boom  # type: ignore[method-assign]
    with pytest.raises(EngineError, match="insert into orderbook failed"):
        engine.insert(sde.ORDERBOOK_TABLE, _row())


# ── Symbols are literals, and a quote cannot be expressed ───────────────────────────────────────


@pytest.mark.parametrize("bad", ["BTC'USDT", "BTC\\USDT", "BTC\nUSDT"])
def test_a_symbol_that_cannot_be_expressed_is_refused_not_escaped(
    engine: OrderbookEngine, fake: _FakeClient, bad: str
) -> None:
    """The engine's tokeniser has no escape sequence inside a string literal.

    So there is nothing to escape *to*, and the escaping a reader reaches for first - doubling the
    quote - would address a different symbol without saying so.
    """
    with pytest.raises(EngineError, match="no escape sequence"):
        engine.insert_levels(
            sde.ORDERBOOK_TABLE,
            symbol=bad,
            exchange="binance",
            side="bid",
            timestamp_ns=1,
            levels=((1, 1, 1),),
        )
    assert fake.writes == []


# ── Reads: flush lazily, and refuse to answer a violated key ────────────────────────────────────


def test_a_read_flushes_only_when_there_is_something_unflushed(
    engine: OrderbookEngine, fake: _FakeClient
) -> None:
    """A write is invisible to a query until flush(), measured. So correctness cannot depend on the
    client remembering - and the cost cannot be paid on reads that would see nothing new."""
    engine.levels(symbol="X", exchange="e")
    assert fake.flushes == 0

    engine.insert(sde.ORDERBOOK_TABLE, _row())
    engine.levels(symbol="X", exchange="e")
    assert fake.flushes == 1

    engine.levels(symbol="X", exchange="e")
    assert fake.flushes == 1, "nothing was written since; there is nothing to make visible"


def test_the_symbol_and_the_exchange_reach_the_query_as_literals(
    engine: OrderbookEngine, fake: _FakeClient
) -> None:
    engine.levels(symbol="BTCUSDT", exchange="binance")
    assert fake.queries == ["SELECT * FROM 'BTCUSDT'.'binance'"]


def test_a_range_is_inclusive_at_both_ends_because_the_engines_between_is(
    engine: OrderbookEngine, fake: _FakeClient
) -> None:
    engine.levels(symbol="X", exchange="e", start_ns=10, end_ns=20, limit=5)
    assert fake.queries == [
        "SELECT * FROM 'X'.'e' WHERE timestamp BETWEEN 10 AND 20 LIMIT 5"
    ]


def test_an_open_upper_bound_becomes_the_largest_uint64_rather_than_a_round_number(
    engine: OrderbookEngine, fake: _FakeClient
) -> None:
    engine.levels(symbol="X", exchange="e", start_ns=10)
    assert f"BETWEEN 10 AND {(1 << 64) - 1}" in fake.queries[0]


def test_the_engines_zero_sequence_number_comes_back_as_unknown(
    engine: OrderbookEngine, fake: _FakeClient
) -> None:
    """Unknown is not zero, which is the rule the rest of this library follows.

    Zero is a safe sentinel inside the engine - its own numbering starts at 1 - and not safe here,
    because a client comparing sequence numbers cannot tell a sentinel from a datum.
    """
    fake.rows = [_Row(1000, 100, 1, 1, "bid", 0, 0), _Row(2000, 99, 2, 1, "bid", 0, 42)]
    rows = engine.levels(symbol="X", exchange="e")
    assert rows[0]["sequence_number"] is None
    assert rows[1]["sequence_number"] == 42


def test_get_returns_the_one_row_with_that_key(engine: OrderbookEngine, fake: _FakeClient) -> None:
    fake.rows = [_Row(1000, 100, 1, 1, "bid", 0, 41), _Row(1000, 99, 2, 1, "ask", 0, 41)]
    row = engine.get(
        sde.ORDERBOOK_TABLE,
        {"symbol": "X", "exchange": "e", "timestamp_ns": 1000, "side": "bid", "level": 0},
    )
    assert row is not None
    assert row["price"] == 100


def test_get_returns_none_when_there_is_no_such_row(
    engine: OrderbookEngine, fake: _FakeClient
) -> None:
    fake.rows = []
    assert (
        engine.get(
            sde.ORDERBOOK_TABLE,
            {"symbol": "X", "exchange": "e", "timestamp_ns": 1000, "side": "bid", "level": 0},
        )
        is None
    )


def test_get_refuses_when_two_rows_share_a_key_rather_than_picking_one(
    engine: OrderbookEngine, fake: _FakeClient
) -> None:
    """The interesting half, and the reason this adapter has a `get` at all rather than just reads.

    This engine does not enforce the key - two writes with the same one both persist, measured, with
    the same sequence number too. Returning either would be a read that lies about uniqueness, and
    the client could not see that it happened.
    """
    fake.rows = [_Row(1000, 100, 1, 1, "bid", 0, 41), _Row(1000, 99, 2, 1, "bid", 0, 41)]
    with pytest.raises(EngineError, match="2 rows in orderbook share the key"):
        engine.get(
            sde.ORDERBOOK_TABLE,
            {"symbol": "X", "exchange": "e", "timestamp_ns": 1000, "side": "bid", "level": 0},
        )


def test_a_partial_key_is_refused_because_there_is_no_scan_to_fall_back_on(
    engine: OrderbookEngine,
) -> None:
    with pytest.raises(EngineError, match=r"missing \['exchange', 'symbol'\]"):
        engine.get(sde.ORDERBOOK_TABLE, {"timestamp_ns": 1000, "side": "bid", "level": 0})


# ── Transactions ────────────────────────────────────────────────────────────────────────────────


def test_transaction_refuses_out_loud(engine: OrderbookEngine) -> None:
    """A context manager that silently did nothing would turn a declared atomicity requirement into
    a comment."""
    with pytest.raises(EngineError, match="no multi-statement transactions"), engine.transaction():
        pass


def test_not_connected_says_so_rather_than_failing_somewhere_else() -> None:
    with pytest.raises(EngineError, match="not connected"):
        OrderbookEngine("/tmp/x").levels(symbol="X", exchange="e")

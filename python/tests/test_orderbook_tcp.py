"""The orderbook adapter's TCP decisions, batches and key-order reads, against a fake.

Everything here is decided before the engine's client is called, so it runs everywhere. The fake
book answers the way the engine was **measured** to answer on 2 October 2026 (engine ``971dda2``),
and ``test_orderbook_slice.py`` and ``test_orderbook_session_slice.py`` re-check each of those
behaviours against the engine itself:

- a scan returns rows in **arrival** order, whatever their event times, and ``LIMIT`` keeps the
  first rows to arrive;
- a book nothing has been written to answers ``OB_ERR_NOT_FOUND`` rather than an empty result;
- the server numbers every update per book over TCP, and refuses a number the client chose.
"""

from __future__ import annotations

import re
import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest

import sde
import sde.engines.orderbook as orderbook
from sde.engines.orderbook import OrderbookEngine
from sde.errors import EngineError
from sde.query import (
    QueryRefused,
    ReadColumn,
    ReadPlan,
    count_engine,
    plan_read,
    query_engine,
    summary_engine,
)
from sde.testing.loader import model_from_neutral

TABLE = sde.ORDERBOOK_TABLE


@dataclass
class _Row:
    symbol: str
    exchange: str
    timestamp_ns: int
    side: str
    level: int
    price: int
    quantity: int
    order_count: int
    sequence_number: int


@dataclass
class _BookUpdate:
    symbol: str
    exchange: str
    side: str
    prices: list[int]
    qtys: list[int]
    counts: list[int] | None = None
    timestamp_ns: int | None = None


@dataclass
class _Outcome:
    index: int
    ok: bool
    message: str = ""


_QUERY = re.compile(
    r"SELECT \* FROM '(?P<symbol>[^']+)'\.'(?P<exchange>[^']+)'"
    r"(?: WHERE timestamp BETWEEN (?P<low>\d+) AND (?P<high>\d+)"
    r"(?: AND price BETWEEN (?P<plow>-?\d+) AND (?P<phigh>-?\d+))?)?"
    r" LIMIT (?P<limit>\d+)$"
)


class _NotFound(Exception):
    status = -1


@dataclass
class _Server:
    """A book store that answers like the engine: arrival order, LIMIT on arrival, NOT_FOUND."""

    rows: list[_Row] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    batches: list[list[_BookUpdate]] = field(default_factory=list)
    inserts: list[dict[str, Any]] = field(default_factory=list)
    refuse: set[int] = field(default_factory=set)
    fail_batch: int | None = None
    fail_close: bool = False
    capabilities: set[str] = field(
        default_factory=lambda: {"insert_event_time", "strict_args", "backup"}
    )
    constructed: list[dict[str, Any]] = field(default_factory=list)
    closed: int = 0
    sequence: int = 0

    def store(self, symbol: str, exchange: str, side: str, prices: list[int], qtys: list[int],
              counts: list[int], timestamp_ns: int, seq: int | None) -> int:
        self.sequence += 1
        number = self.sequence if seq is None else seq
        for level, (price, qty, count) in enumerate(zip(prices, qtys, counts, strict=True)):
            self.rows.append(
                _Row(symbol, exchange, timestamp_ns, side, level, price, qty, count, number)
            )
        return number


class _Client:
    def __init__(self, server: _Server, **kwargs: Any) -> None:
        self.server = server
        server.constructed.append(kwargs)

    def server_capabilities(self) -> set[str]:
        return set(self.server.capabilities)

    def insert(self, symbol: str, exchange: str, side: str, prices: list[int], qtys: list[int],
               counts: list[int], timestamp_ns: int | None = None, seq: int | None = None) -> int:
        self.server.inserts.append({"symbol": symbol, "side": side, "prices": list(prices),
                                    "timestamp_ns": timestamp_ns, "seq": seq})
        assert timestamp_ns is not None
        return self.server.store(symbol, exchange, side, prices, qtys, counts, timestamp_ns, seq)

    def insert_batch(self, updates: list[_BookUpdate]) -> list[_Outcome]:
        self.server.batches.append(list(updates))
        failing = self.server.fail_batch
        if failing is not None and len(self.server.batches) == failing:
            raise OSError("connection reset by peer")
        out = []
        for index, update in enumerate(updates):
            number = sum(len(batch) for batch in self.server.batches[:-1]) + index
            if number in self.server.refuse:
                out.append(_Outcome(index, False, "ERR something the server said"))
                continue
            assert update.timestamp_ns is not None and update.counts is not None
            self.server.store(update.symbol, update.exchange, update.side, update.prices,
                              update.qtys, update.counts, update.timestamp_ns, None)
            out.append(_Outcome(index, True))
        return out

    def flush(self) -> None:
        return None

    def query(self, sql: str) -> list[_Row]:
        self.server.queries.append(sql)
        match = _QUERY.fullmatch(sql)
        assert match, f"the adapter sent a query the engine's grammar would not take: {sql}"
        book = [r for r in self.server.rows
                if (r.symbol, r.exchange) == (match["symbol"], match["exchange"])]
        if not book:
            raise _NotFound(
                f"query error: OB_ERR_NOT_FOUND: symbol '{match['symbol']}' exchange "
                f"'{match['exchange']}' not found"
            )
        if match["low"] is not None:
            low, high = int(match["low"]), int(match["high"])
            book = [r for r in book if low <= r.timestamp_ns <= high]
        if match["plow"] is not None:
            plow, phigh = int(match["plow"]), int(match["phigh"])
            book = [r for r in book if plow <= r.price <= phigh]
        return book[: int(match["limit"])]

    def close(self) -> None:
        self.server.closed += 1
        if self.server.fail_close:
            raise OSError("bad file descriptor")


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> _Server:
    state = _Server()
    module = types.ModuleType("orderbook_engine")
    module.OrderbookEngine = lambda *args, **kwargs: _Client(  # type: ignore[attr-defined]
        state, args=args, **kwargs
    )
    module.BookUpdate = _BookUpdate  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "orderbook_engine", module)
    return state


@pytest.fixture
def tcp(server: _Server) -> OrderbookEngine:
    adapter = OrderbookEngine(host="127.0.0.1", port=9090)
    adapter.connect()
    return adapter


@pytest.fixture
def local(server: _Server) -> OrderbookEngine:
    adapter = OrderbookEngine("/tmp/the-client-is-fake")
    adapter.connect()
    return adapter


def _row(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "symbol": "BTCUSDT",
        "exchange": "binance",
        "timestamp_ns": 1_000,
        "side": "bid",
        "level": 0,
        "price": 5_000_000,
        "quantity": 3,
        "order_count": 1,
        "sequence_number": None,
    }
    values.update(overrides)
    return values


def _model() -> sde.LogicalModel:
    sde.clear_registry()
    return model_from_neutral(
        {
            "name": "market",
            "entities": [
                {
                    "name": "DepthLevel",
                    "fields": [
                        {"name": name, "type": kind, "nullable": name == "sequence_number"}
                        for name, kind in sde.ORDERBOOK_SHAPE.items()
                    ],
                    "key": list(sde.ORDERBOOK_KEY),
                }
            ],
            "relations": [],
            "atomic": [],
        }
    )


def _plan(**kwargs: Any) -> ReadPlan:
    model = _model()
    entity = model.entity("DepthLevel")
    columns = [ReadColumn(f.name, f.type) for f in entity.fields]
    return plan_read(columns, entity.key, **kwargs)


# ── Construction and the DSN ─────────────────────────────────────────────────────────────────────


def test_credentials_and_tls_belong_to_a_connection_and_local_mode_has_none() -> None:
    settings: list[dict[str, Any]] = [
        {"auth": ("a", "s")}, {"tls": True}, {"tls_ca_file": "/x"}, {"tls_verify": False}
    ]
    for kwargs in settings:
        with pytest.raises(EngineError, match="local mode opens none"):
            OrderbookEngine("/tmp/x", **kwargs)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"tls_ca_file": "/ca.pem"}, "verifies nothing"),
        ({"tls_verify": False}, "no certificate to decline"),
        ({"auth": ("identity", "")}, "two non-empty strings"),
        ({"auth": ("two words", "secret")}, "two non-empty strings"),
        ({"auth": ["identity", "secret"]}, "two non-empty strings"),
        ({"timeout": 0}, "positive number"),
        ({"timeout": True}, "positive number"),
    ],
)
def test_a_connection_setting_that_would_do_nothing_is_refused(
    kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(EngineError, match=message):
        OrderbookEngine(host="h", port=1, **kwargs)


def test_connect_hands_every_setting_to_the_engines_client(server: _Server) -> None:
    adapter = OrderbookEngine(
        host="db1", port=9091, auth=("app", "s3cret"), tls=True, tls_ca_file="/ca.pem", timeout=3
    )
    adapter.connect()
    assert server.constructed[-1] == {
        "args": (),
        "host": "db1",
        "port": 9091,
        "timeout": 3.0,
        "auth": ("app", "s3cret"),
        "tls": True,
        "tls_ca_file": "/ca.pem",
        "tls_verify": True,
    }
    assert adapter.mode == "tcp"


def test_a_server_that_cannot_store_an_event_time_is_refused_before_any_write(
    server: _Server,
) -> None:
    server.capabilities = {"strict_args"}
    adapter = OrderbookEngine(host="h", port=1)
    with pytest.raises(EngineError, match="cannot store a write's event time"):
        adapter.connect()
    assert server.closed == 1, "the connection it opened to ask is closed again"
    with pytest.raises(EngineError, match="not connected"):
        adapter.insert(TABLE, _row())


def test_the_dsn_carries_host_port_credentials_and_tls() -> None:
    adapter = OrderbookEngine.from_dsn(
        "orderbook://app%40desk:p%3Ass%20w@db.internal:9443?tls=on&ca=/etc/ca.pem&timeout=2.5"
    )
    assert adapter.mode == "tcp"
    assert (adapter._host, adapter._port) == ("db.internal", 9443)
    assert adapter._auth == ("app@desk", "p:ss w")
    assert (adapter._tls, adapter._tls_ca_file, adapter._tls_verify) == (True, "/etc/ca.pem", True)
    assert adapter._timeout == 2.5
    plain = OrderbookEngine.from_dsn("orderbook://127.0.0.1:9090")
    assert (plain._auth, plain._tls) == (None, False)


@pytest.mark.parametrize(
    ("dsn", "message"),
    [
        ("postgresql://a:b@h:1", "starts with orderbook://"),
        ("orderbook://h", "names a host and a port"),
        ("orderbook://h:notaport", "port is not a number"),
        ("orderbook://h:1/var/data", "no path"),
        ("orderbook://app@h:1", "identity needs its secret"),
        ("orderbook://h:1?sslmode=require", "'sslmode' is unknown"),
        ("orderbook://h:1?tls=on&tls=off", "gives 'tls' twice"),
        ("orderbook://h:1?tls=yes", "on or off"),
        ("orderbook://h:1?verify=off", "no certificate to decline"),
        ("orderbook://h:1?timeout=soon", "number of seconds"),
    ],
)
def test_a_malformed_dsn_is_refused_by_its_part(dsn: str, message: str) -> None:
    with pytest.raises(EngineError, match=message):
        OrderbookEngine.from_dsn(dsn)


def test_the_secret_appears_in_no_refusal_and_no_repr() -> None:
    with pytest.raises(EngineError) as refused:
        OrderbookEngine.from_dsn("orderbook://app:TOPSECRET@h:1?mode=x")
    assert "TOPSECRET" not in str(refused.value)
    adapter = OrderbookEngine.from_dsn("orderbook://app:TOPSECRET@h:1?tls=on")
    assert "TOPSECRET" not in repr(adapter)
    assert repr(adapter) == "OrderbookEngine(tcp h:1, identity='app', tls=on)"


# ── What the engine can store ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"timestamp_ns": -1}, "timestamp_ns -1 is outside"),
        ({"timestamp_ns": 1 << 63}, "timestamp_ns .* is outside"),
        ({"quantity": -5}, "quantity -5 is outside"),
        ({"order_count": -1}, "order_count -1 is outside"),
        ({"order_count": 1 << 31}, "order_count .* is outside"),
        ({"price": 1 << 63}, "price .* is outside"),
        ({"quantity": True}, "quantity must be an integer"),
        ({"price": 1.5}, "price must be an integer"),
        ({"symbol": "BTC USDT"}, "cannot be used as a symbol"),
        ({"symbol": ""}, "non-empty string"),
        ({"exchange": "bin\tance"}, "cannot be used as a exchange"),
        ({"symbol": "BTC'USDT"}, "no escape sequence"),
    ],
)
def test_a_value_the_engine_cannot_store_is_refused_before_sending(
    tcp: OrderbookEngine, server: _Server, override: dict[str, Any], message: str
) -> None:
    with pytest.raises(EngineError, match=message):
        tcp.insert(TABLE, _row(**override))
    assert server.inserts == [] and server.batches == []


def test_a_level_past_the_engines_depth_is_refused(tcp: OrderbookEngine) -> None:
    with pytest.raises(EngineError, match="level 1000 is outside"):
        tcp.insert_many(TABLE, [_row(level=level) for level in range(1001)])


def test_a_negative_price_is_stored_because_the_engine_stores_one(
    tcp: OrderbookEngine, server: _Server
) -> None:
    tcp.insert(TABLE, _row(price=-37_630_000))
    assert server.rows[0].price == -37_630_000


def test_a_write_generation_has_nowhere_to_go_and_is_refused(tcp: OrderbookEngine) -> None:
    with pytest.raises(EngineError, match="carries no write generation"):
        tcp.insert(TABLE, {**_row(), sde.WRITE_EPOCH_COLUMN: 1})
    with pytest.raises(EngineError, match="carries no write generation"):
        tcp.insert_many(TABLE, [{**_row(), sde.WRITE_EPOCH_COLUMN: 1}])


# ── The sequence number belongs to the server over TCP ─────────────────────────────────────────


def test_over_tcp_a_chosen_sequence_number_is_refused_before_sending(
    tcp: OrderbookEngine, server: _Server
) -> None:
    with pytest.raises(EngineError, match="sequence number is the server's"):
        tcp.insert(TABLE, _row(sequence_number=41))
    with pytest.raises(EngineError, match="sequence number is the server's"):
        tcp.insert_many(TABLE, [_row(sequence_number=41)])
    assert server.inserts == [] and server.batches == []


def test_over_tcp_no_sequence_number_goes_out_and_the_servers_comes_back(
    tcp: OrderbookEngine, server: _Server
) -> None:
    tcp.insert(TABLE, _row())
    assert server.inserts[0]["seq"] is None
    key = {name: _row()[name] for name in sde.ORDERBOOK_KEY}
    assert tcp.get(TABLE, key)["sequence_number"] == 1  # type: ignore[index]


def test_in_local_mode_a_chosen_sequence_number_is_written_and_zero_is_not_one(
    local: OrderbookEngine, server: _Server
) -> None:
    local.insert(TABLE, _row(sequence_number=41))
    assert server.inserts[0]["seq"] == 41
    with pytest.raises(EngineError, match="sequence_number 0 is outside"):
        local.insert(TABLE, _row(sequence_number=0))


def test_an_unconfirmed_tcp_write_says_it_may_have_been_stored(
    tcp: OrderbookEngine, server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: Any, **__: Any) -> int:
        raise OSError("timed out")

    monkeypatch.setattr(_Client, "insert", boom)
    with pytest.raises(EngineError, match="may still have been stored"):
        tcp.insert(TABLE, _row())


# ── Batches are the engine's updates ────────────────────────────────────────────────────────────


def test_a_batch_becomes_updates_in_the_order_they_first_appear(
    tcp: OrderbookEngine, server: _Server
) -> None:
    rows = [
        _row(side="ask", level=1, price=101),
        _row(side="bid", level=0, price=99),
        _row(side="ask", level=0, price=100),
        _row(side="bid", level=1, price=98),
        _row(timestamp_ns=2_000, side="bid", level=0, price=97),
    ]
    tcp.insert_many(TABLE, rows)
    assert [(u.side, u.timestamp_ns, u.prices) for u in server.batches[0]] == [
        ("ask", 1_000, [100, 101]),
        ("bid", 1_000, [99, 98]),
        ("bid", 2_000, [97]),
    ]
    assert len(server.batches) == 1, "one round trip for three updates"


def test_updates_go_in_round_trips_of_sixty_four(tcp: OrderbookEngine, server: _Server) -> None:
    tcp.insert_many(TABLE, [_row(timestamp_ns=t) for t in range(130)])
    assert [len(batch) for batch in server.batches] == [64, 64, 2]


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([_row(level=0), _row(level=2)], r"has levels \[0, 2\]"),
        ([_row(level=1)], r"has levels \[1\]"),
        ([_row(level=0, price=1), _row(level=0, price=2)], "are both level 0 of one update"),
    ],
)
def test_an_update_with_a_gap_or_a_repeated_level_is_refused_whole(
    tcp: OrderbookEngine, server: _Server, rows: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(EngineError, match=message):
        tcp.insert_many(TABLE, rows)
    assert server.batches == [], "every refusal that can be decided before sending is"


def test_in_local_mode_one_update_carries_one_sequence_number(local: OrderbookEngine) -> None:
    with pytest.raises(EngineError, match="one update carries one sequence number"):
        local.insert_many(
            TABLE, [_row(level=0, sequence_number=5), _row(level=1, sequence_number=6)]
        )


def test_in_local_mode_a_batch_writes_each_update_with_its_sequence_number(
    local: OrderbookEngine, server: _Server
) -> None:
    local.insert_many(
        TABLE,
        [_row(level=0, sequence_number=7), _row(level=1, sequence_number=7),
         _row(side="ask", sequence_number=8)],
    )
    assert [(i["side"], i["prices"], i["seq"]) for i in server.inserts] == [
        ("bid", [5_000_000, 5_000_000], 7),
        ("ask", [5_000_000], 8),
    ]


def test_a_refusal_by_the_server_names_the_update_and_what_was_stored(
    tcp: OrderbookEngine, server: _Server
) -> None:
    server.refuse = {65}
    with pytest.raises(EngineError) as refused:
        tcp.insert_many(TABLE, [_row(timestamp_ns=t) for t in range(130)])
    message = str(refused.value)
    assert "refused 1 of 130 updates" in message
    assert "update 65 (rows [65])" in message
    assert "127 updates were stored" in message
    assert "not a transaction" in message
    assert len(server.batches) == 2, "nothing after the refused part was sent"


def test_a_failed_connection_mid_batch_is_an_unknown_outcome_and_closes_the_adapter(
    tcp: OrderbookEngine, server: _Server
) -> None:
    server.fail_batch = 2
    with pytest.raises(EngineError) as unknown:
        tcp.insert_many(TABLE, [_row(timestamp_ns=t) for t in range(130)])
    message = str(unknown.value)
    assert "outcome of this batch is unknown" in message
    assert "64 of its 130 updates were confirmed" in message
    assert "in full, in part or not at all" in message
    assert server.closed == 1
    with pytest.raises(EngineError, match="not connected"):
        tcp.insert(TABLE, _row())


def test_a_connection_that_fails_to_close_does_not_hide_the_unknown_outcome(
    tcp: OrderbookEngine, server: _Server
) -> None:
    server.fail_batch, server.fail_close = 1, True
    sde.reset_internal_failures()
    with pytest.raises(EngineError, match="outcome of this batch is unknown"):
        tcp.insert_many(TABLE, [_row()])
    assert server.closed == 1
    assert sde.internal_failures() == {"orderbook.close": 1}
    with pytest.raises(EngineError, match="not connected"):
        tcp.insert(TABLE, _row())


# ── Reads in key order, from an engine that answers in arrival order ───────────────────────────


def _book(tcp: OrderbookEngine, server: _Server) -> None:
    """Six updates of one book written out of event-time order, as a feed with corrections does."""
    for stamp in (3_000, 1_000, 2_000):
        for side, base in (("ask", 101), ("bid", 99)):
            server.store("BTCUSDT", "binance", side, [base + stamp, base + stamp + 1],
                         [1, 2], [1, 1], stamp, None)


def test_a_scan_answers_in_key_order_whatever_the_arrival_order(
    tcp: OrderbookEngine, server: _Server
) -> None:
    _book(tcp, server)
    rows = query_engine(tcp).select_rows(
        TABLE, _plan(where={"symbol": "BTCUSDT", "exchange": "binance"}, limit=100)
    )
    assert [(r["timestamp_ns"], r["side"], r["level"]) for r in rows] == [
        (t, side, level) for t in (1_000, 2_000, 3_000) for side in ("ask", "bid")
        for level in (0, 1)
    ]


def test_a_descending_scan_is_the_same_rows_reversed(tcp: OrderbookEngine, server: _Server) -> None:
    _book(tcp, server)
    rows = tcp.select_rows(
        TABLE,
        _plan(where={"symbol": "BTCUSDT", "exchange": "binance"}, descending=True, limit=100),
    )
    keys = [(r["timestamp_ns"], r["side"], r["level"]) for r in rows]
    assert keys == sorted(keys, reverse=True) and len(keys) == 12


def test_a_page_returns_limit_plus_one_and_the_next_page_starts_after_it(
    tcp: OrderbookEngine, server: _Server
) -> None:
    _book(tcp, server)
    where = {"symbol": "BTCUSDT", "exchange": "binance"}
    first = tcp.select_rows(TABLE, _plan(where=where, limit=5))
    assert len(first) == 6, "one more than the page, so Session.scan knows there is another"
    last = first[4]
    after = {name: last[name] for name in sde.ORDERBOOK_KEY}
    second = tcp.select_rows(TABLE, _plan(where=where, after=after, limit=100))
    assert [(r["timestamp_ns"], r["side"], r["level"]) for r in first[:5] + second] == [
        (t, side, level) for t in (1_000, 2_000, 3_000) for side in ("ask", "bid")
        for level in (0, 1)
    ]


def test_filters_on_side_level_price_and_time_reach_the_engine_where_they_can(
    tcp: OrderbookEngine, server: _Server
) -> None:
    _book(tcp, server)
    rows = tcp.select_rows(
        TABLE,
        _plan(
            where={"symbol": "BTCUSDT", "exchange": "binance", "side": "ask", "level": 1},
            bounds=sde.Range("timestamp_ns", 1_500, 3_000),
            limit=10,
        ),
    )
    assert [(r["timestamp_ns"], r["price"]) for r in rows] == [(2_000, 2_102)]
    assert all("BETWEEN 1500 AND 2999" in query for query in server.queries)
    priced = tcp.select_rows(
        TABLE,
        _plan(where={"symbol": "BTCUSDT", "exchange": "binance", "price": 1_099}, limit=10),
    )
    assert [(r["timestamp_ns"], r["side"], r["level"]) for r in priced] == [(1_000, "bid", 0)]
    assert "AND price BETWEEN 1099 AND 1099" in server.queries[-1]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"where": {"symbol": "BTCUSDT"}}, "fix both symbol and exchange"),
        ({"where": {"symbol": "BTCUSDT", "exchange": "binance", "quantity": 1}},
         r"cannot filter on \['quantity'\]"),
        ({"where": {"symbol": "BTCUSDT", "exchange": "binance"},
          "bounds": sde.Range("level", 0, 3)}, r"no range over \['level'\]"),
        ({"where": {"symbol": "BTCUSDT", "exchange": "binance"}, "order_by": "price"},
         "answers one book in time order"),
    ],
)
def test_a_read_the_engine_cannot_answer_is_refused_before_any_query(
    tcp: OrderbookEngine, server: _Server, kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(QueryRefused, match=message):
        tcp.select_rows(TABLE, _plan(limit=10, **kwargs))
    assert server.queries == []


def test_a_read_that_matches_nothing_asks_nothing(tcp: OrderbookEngine, server: _Server) -> None:
    where = {"symbol": "BTCUSDT", "exchange": "binance"}
    assert tcp.select_rows(TABLE, _plan(where=where, bounds=sde.Range("timestamp_ns", 5, 5))) == []
    assert tcp.select_rows(TABLE, _plan(where={**where, "side": "mid"})) == []
    assert server.queries == []


def test_a_book_nobody_has_written_to_is_empty_not_an_error(
    tcp: OrderbookEngine, server: _Server
) -> None:
    rows = tcp.select_rows(TABLE, _plan(where={"symbol": "NEW", "exchange": "X"}, limit=10))
    assert rows == []
    assert tcp.get(TABLE, {"symbol": "NEW", "exchange": "X", "timestamp_ns": 1,
                           "side": "bid", "level": 0}) is None


def test_two_rows_with_one_key_in_a_page_are_refused_as_in_get(
    tcp: OrderbookEngine, server: _Server
) -> None:
    server.store("BTCUSDT", "binance", "bid", [1], [1], [1], 1_000, None)
    server.store("BTCUSDT", "binance", "bid", [2], [1], [1], 1_000, None)
    with pytest.raises(EngineError, match="does not enforce a key"):
        tcp.select_rows(TABLE, _plan(where={"symbol": "BTCUSDT", "exchange": "binance"}))


def test_a_window_too_large_is_split_and_the_page_stays_in_key_order(
    tcp: OrderbookEngine, server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orderbook, "SCAN_CHUNK_ROWS", 4)
    monkeypatch.setattr(orderbook, "SCAN_FIRST_WINDOW_NS", 10_000)
    for stamp in (900, 500, 100, 700, 300):
        server.store("BTCUSDT", "binance", "bid", [stamp, stamp - 1], [1, 1], [1, 1], stamp, None)
    rows = tcp.select_rows(
        TABLE, _plan(where={"symbol": "BTCUSDT", "exchange": "binance"}, limit=100)
    )
    assert [(r["timestamp_ns"], r["level"]) for r in rows] == [
        (t, level) for t in (100, 300, 500, 700, 900) for level in (0, 1)
    ]
    assert all(q.endswith("LIMIT 5") for q in server.queries), "never more than the cap in memory"


def test_one_instant_holding_more_than_the_cap_is_refused(
    tcp: OrderbookEngine, server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orderbook, "SCAN_CHUNK_ROWS", 4)
    server.store("BTCUSDT", "binance", "bid", [5, 4, 3, 2, 1], [1] * 5, [1] * 5, 100, None)
    with pytest.raises(EngineError, match="at the instant 100"):
        tcp.select_rows(TABLE, _plan(where={"symbol": "BTCUSDT", "exchange": "binance"}))


def test_a_sparse_book_grows_the_window_instead_of_asking_once_per_second(
    tcp: OrderbookEngine, server: _Server
) -> None:
    """The number of reads is logarithmic in the span of time, not linear.

    Bounded at 2000 seconds, a page of two rows takes about eleven doublings of a one-second window.
    Unbounded, the scan cannot know the second row was the last one, so it keeps doubling to the end
    of the uint64 range - at most about 64 reads, each of them empty and cheap.
    """
    server.store("BTCUSDT", "binance", "bid", [1], [1], [1], 10 * 1_000_000_000, None)
    server.store("BTCUSDT", "binance", "bid", [2], [1], [1], 1_000 * 1_000_000_000, None)
    where = {"symbol": "BTCUSDT", "exchange": "binance"}
    bounded = tcp.select_rows(
        TABLE, _plan(where=where, bounds=sde.Range("timestamp_ns", 0, 2_000 * 1_000_000_000))
    )
    assert len(bounded) == 2
    assert len(server.queries) <= 12, "two thousand seconds is eleven doublings of one second"
    server.queries.clear()
    unbounded = tcp.select_rows(TABLE, _plan(where=where))
    assert len(unbounded) == 2
    assert len(server.queries) <= 65, "the whole uint64 range is at most 64 doublings"


# ── What it does not count ──────────────────────────────────────────────────────────────────────


def test_a_count_and_a_summary_are_refused_by_name_before_anything_is_timed(
    tcp: OrderbookEngine,
) -> None:
    with pytest.raises(QueryRefused, match="counts nothing over its history"):
        count_engine(tcp)
    with pytest.raises(QueryRefused, match="summarizes nothing over its history"):
        summary_engine(tcp)
    assert query_engine(tcp) is tcp, "a page of rows is offered"


def test_the_shape_check_is_also_a_schema_validation_with_no_findings(
    tcp: OrderbookEngine,
) -> None:
    layout = sde.PhysicalLayout(
        tables={"DepthLevel": TABLE},
        columns={"DepthLevel": {name: name for name in sde.ORDERBOOK_SHAPE}},
        indexes=(),
    )
    assert tcp.validate_schema(layout, keys={"DepthLevel": sde.ORDERBOOK_KEY}) == ()
    with pytest.raises(EngineError, match="addresses rows by"):
        tcp.validate_schema(layout, keys={"DepthLevel": ("symbol",)})


# ── The facts the control plane gives a model about this engine (sde/facts.py) ─────────────────


def test_the_facts_name_what_a_scan_needs(tcp: OrderbookEngine, server: _Server) -> None:
    needs = sde.engine_facts("orderbook")["scan_requires"]
    book = {"symbol": "BTCUSDT", "exchange": "binance"}
    assert sorted(needs["equal"]) == sorted(book)
    for column in needs["equal"]:
        partial = {name: value for name, value in book.items() if name != column}
        with pytest.raises(QueryRefused):
            tcp.select_rows(TABLE, _plan(where=partial, limit=1))
    for column in needs["ranges"]:
        tcp.select_rows(TABLE, _plan(where=book, bounds=sde.Range(column, 0, 10), limit=1))
    unlisted = sorted(set(sde.ORDERBOOK_SHAPE) - set(needs["ranges"]) - set(needs["equal"]))
    for column in unlisted:
        with pytest.raises(QueryRefused):
            tcp.select_rows(TABLE, _plan(where=book, bounds=sde.Range(column, 0, 3), limit=1))
    assert needs["order"] == [name for name in sde.ORDERBOOK_KEY if name not in book]
    with pytest.raises(QueryRefused, match="time order"):
        tcp.select_rows(TABLE, _plan(where=book, order_by="price", limit=1))


def test_the_facts_name_the_write_unit(tcp: OrderbookEngine, server: _Server) -> None:
    assert sde.engine_facts("orderbook")["write_unit"] == "level_update"
    with pytest.raises(EngineError, match="declares level 1"):
        tcp.insert(TABLE, _row(level=1))
    with pytest.raises(EngineError):
        tcp.insert_many(TABLE, [_row(level=0), _row(level=2)])
    assert server.batches == [] and server.inserts == []

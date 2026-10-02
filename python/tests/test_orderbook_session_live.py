"""A Session over the orderbook engine, live, in both of its modes - and over TLS with credentials.

What a client actually calls is ``Session``: ``save_many`` writes whole updates, ``scan`` pages a
book in key order although the engine answers in arrival order, ``count`` and ``summarize`` are
refused by name, and the telemetry window sees every call. ``test_orderbook_tcp.py`` holds the
adapter's decisions against a fake; this holds them against the engine. How to run it is in
``_orderbook_live.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
from _orderbook_live import ENABLED, MODES, REASON, SECURE_DSN, TCP, fresh_book, open_engine

import sde
from sde.engines.orderbook import OrderbookEngine
from sde.errors import EngineError
from sde.query import QueryRefused
from sde.testing.loader import model_from_neutral

pytestmark = pytest.mark.skipif(not ENABLED, reason=REASON)

EXCHANGE = "binance"
T0 = 1_790_000_000_000_000_000


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


def _session(
    engine: OrderbookEngine, recorder: sde.Recorder | None = None
) -> tuple[sde.Session, sde.LogicalModel]:
    model = _model()
    group = sde.colocation_groups(model)[0]
    layout = sde.default_layout(model, group, dialect="orderbook")
    raw = {
        "tables": dict(layout.tables),
        "columns": {k: dict(v) for k, v in layout.columns.items()},
    }
    placement = sde.load_map(
        {
            "contract": 3,
            "model_version": model.version,
            "map_version": 1,
            "groups": {"DepthLevel": {"source": {"id": "source", "engine": "ob", "layout": raw}}},
        },
        model=model,
    )
    engine.ensure_schema(layout, keys={"DepthLevel": sde.ORDERBOOK_KEY})
    return sde.Session(model, placement, {"ob": engine}, recorder=recorder), model


@pytest.fixture(params=MODES)
def engine(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[OrderbookEngine]:
    adapter = open_engine(request.param, tmp_path)
    adapter.connect()
    try:
        yield adapter
    finally:
        adapter.close()


def _snapshot(symbol: str, stamp: int, depth: int = 3) -> list[dict[str, Any]]:
    """Both sides of one book at one instant, as a feed's snapshot: two updates of ``depth``."""
    rows = []
    for side, top, step in (("bid", 6_500_000, -100), ("ask", 6_500_100, 100)):
        for level in range(depth):
            rows.append(
                {
                    "symbol": symbol,
                    "exchange": EXCHANGE,
                    "timestamp_ns": stamp,
                    "side": side,
                    "level": level,
                    "price": top + level * step,
                    "quantity": 1_000 + level,
                    "order_count": 1 + level,
                }
            )
    return rows


def _positions(rows: Any) -> list[tuple[int, str, int]]:
    return [(row["timestamp_ns"], row["side"], row["level"]) for row in rows]


def _without_sequence(row: dict[str, Any]) -> dict[str, Any]:
    return {name: value for name, value in row.items() if name != "sequence_number"}


def test_save_many_writes_whole_updates_and_every_row_reads_back(engine: OrderbookEngine) -> None:
    session, _ = _session(engine)
    symbol = fresh_book()
    written = _snapshot(symbol, T0, depth=5)
    session.save_many("DepthLevel", written)
    page = session.scan("DepthLevel", where={"symbol": symbol, "exchange": EXCHANGE}, limit=100)
    assert [_without_sequence(row) for row in page.rows] == sorted(
        written, key=lambda row: (row["timestamp_ns"], row["side"], row["level"])
    )
    numbers = {row["side"]: row["sequence_number"] for row in page.rows}
    if engine.mode == "tcp":
        assert len(set(numbers.values())) == 2, "the server numbers each of the two updates"
    key = {name: written[0][name] for name in sde.ORDERBOOK_KEY}
    assert _without_sequence(session.get("DepthLevel", key)) == written[0]


def test_a_scan_pages_in_key_order_although_the_engine_answers_in_arrival_order(
    engine: OrderbookEngine,
) -> None:
    """Updates arrive out of event-time order, as a feed with corrections does; pages do not."""
    session, _ = _session(engine)
    symbol = fresh_book()
    for offset in (5, 1, 4, 2, 3):
        session.save_many("DepthLevel", _snapshot(symbol, T0 + offset * 1_000))
    expected = [
        (T0 + offset * 1_000, side, level)
        for offset in (1, 2, 3, 4, 5)
        for side in ("ask", "bid")
        for level in range(3)
    ]
    where = {"symbol": symbol, "exchange": EXCHANGE}
    seen: list[tuple[int, str, int]] = []
    after = None
    while True:
        page = session.scan("DepthLevel", where=where, after=after, limit=7)
        seen.extend(_positions(page.rows))
        if page.next_after is None:
            break
        after = dict(page.next_after)
    assert seen == expected

    descending = session.scan("DepthLevel", where=where, descending=True, limit=100)
    assert _positions(descending.rows) == list(reversed(expected))


def test_a_scan_narrowed_by_side_level_time_and_price(engine: OrderbookEngine) -> None:
    session, _ = _session(engine)
    symbol = fresh_book()
    for offset in (1, 2, 3):
        session.save_many("DepthLevel", _snapshot(symbol, T0 + offset * 1_000))
    page = session.scan(
        "DepthLevel",
        where={"symbol": symbol, "exchange": EXCHANGE, "side": "ask", "level": 1},
        bounds=sde.Range("timestamp_ns", T0 + 1_500, T0 + 3_000),
        limit=10,
    )
    assert _positions(page.rows) == [(T0 + 2_000, "ask", 1)]
    priced = session.scan(
        "DepthLevel",
        where={"symbol": symbol, "exchange": EXCHANGE},
        bounds=sde.Range("price", 6_499_900, 6_500_101),
        limit=100,
    )
    assert {row["price"] for row in priced.rows} == {6_499_900, 6_500_000, 6_500_100}


def test_a_duplicated_key_is_refused_by_a_scan_as_by_a_get(engine: OrderbookEngine) -> None:
    session, _ = _session(engine)
    symbol = fresh_book()
    one = _snapshot(symbol, T0, depth=1)[:1]
    session.save_many("DepthLevel", one)
    session.save_many("DepthLevel", [{**one[0], "price": one[0]["price"] - 1}])
    with pytest.raises(EngineError, match="does not enforce a key"):
        session.scan("DepthLevel", where={"symbol": symbol, "exchange": EXCHANGE})
    with pytest.raises(EngineError, match="share the key"):
        session.get("DepthLevel", {name: one[0][name] for name in sde.ORDERBOOK_KEY})


def test_count_and_summarize_are_refused_by_name_and_are_not_recorded_as_errors(
    engine: OrderbookEngine,
) -> None:
    recorder = sde.Recorder(_model().version)
    session, model = _session(engine, recorder)
    symbol = fresh_book()
    session.save_many("DepthLevel", _snapshot(symbol, T0))
    where = {"symbol": symbol, "exchange": EXCHANGE}
    with pytest.raises(QueryRefused, match="counts nothing over its history"):
        session.count("DepthLevel", where=where)
    with pytest.raises(QueryRefused, match="summarizes nothing over its history"):
        session.summarize("DepthLevel", "quantity", where=where)
    window = recorder.roll()
    assert window is not None
    group = window.as_record(model)["groups"]["DepthLevel"]
    assert group["error_share"] == 0.0, "a read the engine cannot answer is not an engine error"
    assert {shape["kind"] for shape in group["shapes"]} == {"bulk_write"}


def test_the_window_sees_every_call_and_names_the_size_it_could_not_measure(
    engine: OrderbookEngine,
) -> None:
    recorder = sde.Recorder(_model().version)
    session, model = _session(engine, recorder)
    symbol = fresh_book()
    session.save_many("DepthLevel", _snapshot(symbol, T0))
    session.scan(
        "DepthLevel",
        where={"symbol": symbol, "exchange": EXCHANGE},
        bounds=sde.Range("timestamp_ns", T0, T0 + 1),
    )
    measured = session.measure_storage()
    assert measured.sizes == () and measured.unavailable == {"DepthLevel": "unsupported"}
    window = recorder.roll()
    assert window is not None
    group = window.as_record(model)["groups"]["DepthLevel"]
    kinds = {shape["kind"]: shape for shape in group["shapes"]}
    assert set(kinds) == {"bulk_write", "range_read"}
    assert kinds["bulk_write"]["rows"] == 6
    assert kinds["range_read"]["fields"] == ["timestamp_ns"]
    assert kinds["range_read"]["filtered_on"] == [
        {"equal": ["exchange", "symbol"], "range": "timestamp_ns", "calls": 1}
    ]
    assert "total_bytes" in group["missing"], "unknown, and named as unknown"


@pytest.mark.skipif(not TCP, reason="set SDE_ORDERBOOK_TCP=host:port to run against a server")
def test_over_tcp_a_chosen_sequence_number_never_leaves_the_session(tmp_path: Path) -> None:
    """In-process the client chooses the number, as the slice holds; over TCP the server does."""
    engine = open_engine("tcp", tmp_path)
    engine.connect()
    session, _ = _session(engine)
    symbol = fresh_book()
    row = {**_snapshot(symbol, T0, depth=1)[0], "sequence_number": 41}
    with pytest.raises(EngineError, match="sequence number is the server's"):
        session.save("DepthLevel", row)
    with pytest.raises(EngineError, match="sequence number is the server's"):
        session.save_many("DepthLevel", [row])
    page = session.scan("DepthLevel", where={"symbol": symbol, "exchange": EXCHANGE})
    assert page.rows == (), "nothing was sent"
    engine.close()


# ── Client authentication and TLS ───────────────────────────────────────────────────────────────

secure = pytest.mark.skipif(
    not SECURE_DSN,
    reason="set SDE_ORDERBOOK_SECURE_DSN to an ob_tcp_server with --auth-secret-file and "
    "--tls-client, as orderbook://identity:secret@host:port?tls=on&ca=PATH",
)


def _variant(dsn: str, *, secret: str | None = None, query: str | None = None) -> str:
    parts = urlsplit(dsn)
    netloc = parts.netloc
    if secret is not None:
        credentials, _, address = netloc.rpartition("@")
        identity = credentials.split(":", 1)[0]
        netloc = f"{identity}:{secret}@{address}"
    return urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query if query is None else query, "")
    )


@secure
def test_with_credentials_and_tls_a_session_writes_and_reads() -> None:
    assert SECURE_DSN is not None
    engine = OrderbookEngine.from_dsn(SECURE_DSN)
    engine.connect()
    try:
        session, _ = _session(engine)
        symbol = fresh_book()
        session.save_many("DepthLevel", _snapshot(symbol, T0))
        page = session.scan("DepthLevel", where={"symbol": symbol, "exchange": EXCHANGE})
        assert len(page.rows) == 6
    finally:
        engine.close()


@secure
def test_a_wrong_secret_is_refused_at_connect() -> None:
    assert SECURE_DSN is not None
    engine = OrderbookEngine.from_dsn(_variant(SECURE_DSN, secret="not-the-secret"))
    with pytest.raises(EngineError) as refused:
        engine.connect()
    assert "not-the-secret" not in str(refused.value)


@secure
def test_plain_text_against_the_tls_port_does_not_connect() -> None:
    assert SECURE_DSN is not None
    parts = urlsplit(SECURE_DSN)
    plain_query = "&".join(
        item for item in parts.query.split("&") if not item.startswith(("tls=", "ca=", "verify="))
    )
    engine = OrderbookEngine.from_dsn(_variant(SECURE_DSN, query=plain_query + "&timeout=2"))
    with pytest.raises(EngineError):
        engine.connect()

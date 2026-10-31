"""A book stream against the orderbook engine, live: the book the engine composes from what a
stream sent is the book the application gave it - in both stream modes and both engine modes.

The application here is a seeded random walk of whole sides and of changes, and the oracle is the
walk's own book, not the stream's, which is what is under test. After every step the engine is
asked for the book as of that step's time (``Session.book_at``), which it composes by price from
history; over TCP also for its live book, which it builds in arrival order. A full side moved by a
tick checks the one thing the two can differ on: an update that adds a price to a full side and
removes another is applied removals first. How to run it is in ``_orderbook_live.py``.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from pathlib import Path

import pytest
from _orderbook_live import ENABLED, MODES, REASON, fresh_book, open_engine

import sde
from sde.engines.orderbook import OrderbookEngine
from sde.testing.loader import model_from_neutral

pytestmark = pytest.mark.skipif(not ENABLED, reason=REASON)

EXCHANGE = "binance"
T0 = 1_790_000_000_000_000_000
SIDES = ("bid", "ask")
# Each side walks inside its own band of prices, so a side holds at most this many.
BANDS = {"bid": range(940, 1000), "ask": range(1000, 1060)}

Book = dict[int, tuple[int, int]]
Levels = list[tuple[int, int, int]]


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


def _session(engine: OrderbookEngine) -> sde.Session:
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
    return sde.Session(model, placement, {"ob": engine})


@pytest.fixture(params=MODES)
def engine(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[OrderbookEngine]:
    adapter = open_engine(request.param, tmp_path)
    adapter.connect()
    try:
        yield adapter
    finally:
        adapter.close()


def _levels(side: str, book: Book) -> Levels:
    return [(p, book[p][0], book[p][1]) for p in sorted(book, reverse=(side == "bid"))]


def _whole(rng: random.Random, side: str, book: Book) -> Book:
    """The side after a step that gives it whole: some prices gone, some new, some moved."""
    new = {p: v for p, v in book.items() if rng.random() > 0.2}
    for _ in range(rng.randint(1, 6)):
        new[rng.choice(BANDS[side])] = (rng.randint(1, 50), rng.randint(1, 5))
    return new


def _changes(rng: random.Random, side: str, book: Book) -> Levels:
    """Changes as an exchange sends them: removals, a removal of a price not held, new and moved
    prices - each price once."""
    out: Levels = []
    for price in rng.sample(BANDS[side], rng.randint(1, 8)):
        if (price in book and rng.random() < 0.5) or (price not in book and rng.random() < 0.2):
            out.append((price, 0, 0))
        else:
            out.append((price, rng.randint(1, 50), rng.randint(1, 5)))
    return out


def _live(engine: OrderbookEngine, symbol: str) -> dict[str, Levels]:
    """The engine's live book, over TCP: the structure it updates in arrival order."""
    rows = engine._ob.book(symbol, EXCHANGE)
    book: dict[str, Levels] = {side: [] for side in SIDES}
    for row in sorted(rows, key=lambda r: (r.side, r.level)):
        book[row.side].append((row.price, row.quantity, row.order_count))
    return book


@pytest.mark.parametrize("mode", ["full", "incremental"])
def test_the_engine_composes_the_book_the_application_gave(
    engine: OrderbookEngine, mode: str
) -> None:
    session = _session(engine)
    symbol = fresh_book()
    stream = session.book_stream(
        "DepthLevel", symbol=symbol, exchange=EXCHANGE, mode=mode,
        resync=sde.Resync(updates=9, seconds=0),
    )
    rng = random.Random(f"{mode}-233")
    truth: dict[str, Book] = {side: {} for side in SIDES}
    at = T0
    for step in range(160):
        at += 1_000
        # Both sides given whole first: a book is created by its first write, and in local mode
        # the engine cannot tell a book nobody wrote from a failed read.
        side = SIDES[step] if step < 2 else rng.choice(SIDES)
        if mode == "incremental" and step >= 2 and rng.random() < 0.5:
            changes = _changes(rng, side, truth[side])
            for price, quantity, count in changes:
                if quantity == 0:
                    truth[side].pop(price, None)
                else:
                    truth[side][price] = (quantity, count)
            stream.apply(side, changes, timestamp_ns=at)
        else:
            truth[side] = _whole(rng, side, truth[side])
            stream.update(side, _levels(side, truth[side]), timestamp_ns=at)
        expected = {s: _levels(s, truth[s]) for s in SIDES}
        got = session.book_at("DepthLevel", symbol=symbol, exchange=EXCHANGE, at_ns=at)
        assert got == expected, f"step {step}: the book as of {at}"
        if engine.mode == "tcp":
            assert _live(engine, symbol) == expected, f"step {step}: the live book"
    if mode == "incremental":
        assert stream.stats.changes > 0 and stream.stats.whole > 0


def test_a_full_side_moved_by_a_tick_keeps_every_price(engine: OrderbookEngine) -> None:
    """Each step removes the best bid and adds a new worst one, to a side of the engine's 1000.
    Applied in the other order, the new price would find the side full and be dropped."""
    session = _session(engine)
    symbol = fresh_book()
    stream = session.book_stream(
        "DepthLevel", symbol=symbol, exchange=EXCHANGE, resync=sde.Resync(updates=0, seconds=0)
    )
    at = T0
    for tick in range(5):
        at += 1_000
        side = [(p, 1 + p % 7, 1) for p in range(10_000 - tick, 9_000 - tick, -1)]
        stream.update("bid", side, timestamp_ns=at)
        assert session.book_at(
            "DepthLevel", symbol=symbol, exchange=EXCHANGE, at_ns=at
        ) == {"bid": side, "ask": []}
        if engine.mode == "tcp":
            assert _live(engine, symbol)["bid"] == side
    assert (stream.stats.whole, stream.stats.changes, stream.stats.levels_changes) == (1, 4, 8)


def test_an_earlier_book_is_read_as_it_was(engine: OrderbookEngine) -> None:
    """The changes a stream stores answer every instant, not only the last one."""
    session = _session(engine)
    symbol = fresh_book()
    stream = session.book_stream("DepthLevel", symbol=symbol, exchange=EXCHANGE)
    stream.update("ask", [(1000, 5, 1), (1001, 3, 1), (1002, 2, 1)], timestamp_ns=T0 + 10)
    stream.apply("ask", [(1001, 0, 0), (1003, 7, 2)], timestamp_ns=T0 + 20)
    stream.update("ask", [(1000, 5, 1), (1003, 8, 2)], timestamp_ns=T0 + 30)
    read = {
        at: session.book_at("DepthLevel", symbol=symbol, exchange=EXCHANGE, at_ns=at)["ask"]
        for at in (T0 + 10, T0 + 25, T0 + 30)
    }
    assert read == {
        T0 + 10: [(1000, 5, 1), (1001, 3, 1), (1002, 2, 1)],
        T0 + 25: [(1000, 5, 1), (1002, 2, 1), (1003, 7, 2)],
        T0 + 30: [(1000, 5, 1), (1003, 8, 2)],
    }
    # The whole side, the changes, then the third update's difference: 1002 removed, 1003 moved.
    assert (stream.stats.whole, stream.stats.changes) == (1, 2)

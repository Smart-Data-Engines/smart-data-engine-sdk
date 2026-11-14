"""The engine's released Python client, the floor of the extra ``orderbook``, on a newer server.

``pip install 'smart-data-engine-sdk[orderbook]'`` resolves to the engine's client on PyPI, and the
oldest one the extra admits predates the server this repository pins: it has no ``replace_side``,
which came with the engine's updates by price. Measured on 14 November 2026, with that client and
the pinned server: every write and read of the adapter worked over TCP, with credentials and TLS
too, and a book stream's first update was an ``AttributeError``.

This file runs where that client is the one installed - the ``orderbook`` CI job, in an environment
of its own with nothing of the engine's checkout on its path - beside the TCP cases of the two
slices, which are the first half of the measurement. It holds the second: a stream is refused by
name before anything is sent, and what such a client can do stays open to it.

``SDE_ORDERBOOK_RELEASED_CLIENT=1`` and ``SDE_ORDERBOOK_TCP=host:port`` of a server that announces
``replace_side`` turn it on. The first test is the control: on any other client it fails rather
than letting the rest pass for the wrong reason.
"""

from __future__ import annotations

import importlib.metadata
import os
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
from _orderbook_live import TCP, fresh_book, open_engine
from test_orderbook_books import EXCHANGE, T0, _session

import sde
from sde.engines.orderbook import OrderbookEngine
from sde.errors import EngineError

pytestmark = pytest.mark.skipif(
    os.environ.get("SDE_ORDERBOOK_RELEASED_CLIENT") != "1" or not TCP,
    reason=(
        "set SDE_ORDERBOOK_RELEASED_CLIENT=1 and SDE_ORDERBOOK_TCP=host:port where the installed "
        "orderbook client is the oldest the extra `orderbook` admits"
    ),
)

TABLE = sde.ORDERBOOK_TABLE
PROJECT = tomllib.loads(
    (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
)["project"]


def _floor() -> tuple[str, str]:
    """The distribution and the oldest version the extra admits, from the packaging metadata."""
    (requirement,) = PROJECT["optional-dependencies"]["orderbook"]
    distribution, _, version = requirement.partition(">=")
    assert version, requirement
    return distribution, version


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[OrderbookEngine]:
    adapter = open_engine("tcp", tmp_path)
    adapter.connect()
    try:
        yield adapter
    finally:
        adapter.close()


def test_the_client_installed_is_the_floor_of_the_extra_from_the_index() -> None:
    import orderbook_engine

    distribution, version = _floor()
    assert importlib.metadata.version(distribution) == version
    owned = {
        str(path.locate().resolve())
        for path in importlib.metadata.files(distribution) or ()
        if path.name == "__init__.py"
    }
    assert str(Path(orderbook_engine.__file__).resolve()) in owned, (
        f"orderbook_engine was imported from {orderbook_engine.__file__}, which {distribution} "
        f"{version} did not install: something else is on the path before it"
    )
    assert not hasattr(orderbook_engine.OrderbookEngine, "replace_side"), (
        f"{distribution} {version} replaces a side: the floor moved, and this file describes the "
        f"one before it"
    )


def test_a_book_stream_is_refused_by_name_and_nothing_is_sent(engine: OrderbookEngine) -> None:
    assert engine.supports_replace_side(), "the server is no newer than the client: nothing to hold"
    session = _session(engine)
    symbol = fresh_book()
    with pytest.raises(EngineError, match="Python client installed here cannot replace a side"):
        session.book_stream("DepthLevel", symbol=symbol, exchange=EXCHANGE)
    book = {"symbol": symbol, "exchange": EXCHANGE}
    with pytest.raises(EngineError, match="Install the client of the engine you run"):
        engine.replace_levels(TABLE, **book, side="bid", timestamp_ns=T0, levels=[(100, 1, 1)])
    assert session.book_at("DepthLevel", symbol=symbol, exchange=EXCHANGE, at_ns=T0 + 10) == {
        "bid": [],
        "ask": [],
    }


def test_an_update_by_price_and_a_book_as_of_a_moment_work(engine: OrderbookEngine) -> None:
    """An update by price is the engine's ordinary write, which the released client has: a level
    sets its price's quantity, a quantity of 0 removes the price, and a price it does not name
    stays."""
    session = _session(engine)
    symbol = fresh_book()
    book = {"symbol": symbol, "exchange": EXCHANGE}
    first = [(100, 5, 1), (99, 3, 1)]
    engine.apply_levels(TABLE, **book, side="bid", timestamp_ns=T0, levels=first)
    engine.apply_levels(
        TABLE, **book, side="bid", timestamp_ns=T0 + 10, levels=[(100, 0, 0), (101, 2, 1)]
    )
    assert session.book_at("DepthLevel", **book, at_ns=T0) == {"bid": first, "ask": []}
    assert session.book_at("DepthLevel", **book, at_ns=T0 + 10) == {
        "bid": [(101, 2, 1), (99, 3, 1)],
        "ask": [],
    }

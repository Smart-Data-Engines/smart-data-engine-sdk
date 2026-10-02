"""What the live orderbook tests share: switches, the engine in both of its modes, and fresh books.

The engine's Python client is not on PyPI and its shared library is built from C++, so these tests
run only where an engine is: the SDK's ``orderbook`` CI job builds one at the commit
``.github/orderbook-engine.txt`` pins, starts two servers and fails if anything here was
skipped. Locally::

    cmake -S . -B build -DOB_BUILD_TESTS=OFF
    cmake --build build --target ob_tcp_server orderbook_shared
    ./build/ob_tcp_server --port 59090 --data-dir /tmp/ob-sde &
    OB_LIB_PATH=$PWD/build/liborderbook_shared.so PYTHONPATH=$PWD/python \\
    SDE_ORDERBOOK=1 SDE_ORDERBOOK_TCP=127.0.0.1:59090 pytest tests/test_orderbook_*.py

``SDE_ORDERBOOK_SECURE_DSN`` adds the server with client authentication and TLS, as an
``orderbook://identity:secret@host:port?tls=on&ca=PATH`` DSN.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from sde.engines.orderbook import OrderbookEngine

ENABLED = os.environ.get("SDE_ORDERBOOK") == "1"
TCP = os.environ.get("SDE_ORDERBOOK_TCP")
SECURE_DSN = os.environ.get("SDE_ORDERBOOK_SECURE_DSN")

REASON = (
    "set SDE_ORDERBOOK=1, OB_LIB_PATH and PYTHONPATH to run the orderbook slice. The engine's "
    "Python client is not on PyPI and its shared library is built from C++, so this cannot run "
    "everywhere - which is why the adapter's own decisions are tested against a fake and only the "
    "engine's measured behaviour is tested here"
)

MODES = [
    pytest.param("local", id="local"),
    pytest.param(
        "tcp",
        id="tcp",
        marks=pytest.mark.skipif(
            not TCP, reason="set SDE_ORDERBOOK_TCP=host:port to run against an ob_tcp_server"
        ),
    ),
]


def open_engine(mode: str, directory: Path) -> OrderbookEngine:
    """An unconnected adapter: in-process on a fresh data directory, or the configured server."""
    if mode == "local":
        return OrderbookEngine(str(directory / "ob"))
    assert TCP is not None
    host, port = TCP.rsplit(":", 1)
    return OrderbookEngine(host=host, port=int(port))


def fresh_book() -> str:
    """A symbol no other test has written to, because over TCP every test shares one server."""
    return "T" + uuid.uuid4().hex[:12].upper()

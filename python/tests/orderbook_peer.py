"""The Python half of the TypeScript orderbook slice: write a book, or read one back, over TCP.

``python orderbook_peer.py write|read <host:port> <symbol>`` writes three updates of a fixed book
through ``Session.save_many`` - out of event-time order, as a feed with corrections does - or prints
every row of the book in key order as JSON, integers as strings, so the TypeScript test can compare
what one library wrote with what the other reads. Needs the engine's Python client on PYTHONPATH.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import sde
from sde.engines.orderbook import OrderbookEngine
from sde.testing.loader import model_from_neutral

EXCHANGE = "binance"


def model() -> sde.LogicalModel:
    return model_from_neutral(
        {
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


def session(engine: OrderbookEngine) -> sde.Session:
    logical = model()
    group = sde.colocation_groups(logical)[0]
    layout = sde.default_layout(logical, group, dialect="orderbook")
    placement = sde.load_map(
        {
            "contract": 3,
            "model_version": logical.version,
            "map_version": 1,
            "groups": {
                "DepthLevel": {
                    "source": {
                        "id": "source",
                        "engine": "ob",
                        "layout": {
                            "tables": dict(layout.tables),
                            "columns": {k: dict(v) for k, v in layout.columns.items()},
                        },
                    }
                }
            },
        },
        model=logical,
    )
    return sde.Session(logical, placement, {"ob": engine})


def rows(symbol: str) -> list[dict[str, Any]]:
    out = []
    for stamp in (3_000, 1_000, 2_000):
        for side, base in (("ask", 101), ("bid", 99)):
            for level in range(2):
                out.append(
                    {
                        "symbol": symbol,
                        "exchange": EXCHANGE,
                        "timestamp_ns": 1_790_000_000_000_000_000 + stamp,
                        "side": side,
                        "level": level,
                        "price": base * 100 + stamp + level,
                        "quantity": 5 + level,
                        "order_count": 1 + level,
                        "sequence_number": None,
                    }
                )
    return out


def main() -> None:
    action, address, symbol = sys.argv[1:4]
    host, port = address.rsplit(":", 1)
    engine = OrderbookEngine(host=host, port=int(port))
    engine.connect()
    try:
        current = session(engine)
        if action == "write":
            current.save_many("DepthLevel", rows(symbol))
            # Visible to another connection at the server's next flush tick, or now with a FLUSH:
            # the reader is another process, and its own adapter has nothing unflushed to flush.
            engine.flush()
            print(json.dumps({"written": len(rows(symbol))}))
            return
        page = current.scan(
            "DepthLevel", where={"symbol": symbol, "exchange": EXCHANGE}, limit=1000
        )
        as_text = [
            {key: str(value) if isinstance(value, int) else value for key, value in row.items()}
            for row in page.rows
        ]
        print(json.dumps(as_text))
    finally:
        engine.close()


if __name__ == "__main__":
    main()

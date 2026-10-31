#!/usr/bin/env python3
"""One order book sent to the orderbook engine whole and as its changes, and read back by price.

    BOOK_STREAM_OB_DSN=orderbook://host:port python book_stream.py [--steps 600] [--depth 20]

An exchange's feed of one instrument - a bid and an ask side of up to ``--depth`` prices around a
walking mid price, given now as a snapshot of a side and now as the changes of one - is sent twice,
to two books of the engine:
- ``mode="full"``: every update a side whole, which the engine stores as a replacement;
- ``mode="incremental"``: the changes, as the feed gave them or as the difference from the side last
  sent, and a side whole at the first send and at every resync.

Then the book the engine composes is read back with ``Session.book_at`` at every tenth step, for
both, and compared with the feed's own book at that step: the program exits 1 on any difference. It
prints what each stream sent, as JSON.

The placement map is written here, unsigned. The library reads a map without an account - a
documented mode - and a map the control plane signs places ``DepthLevel`` the same way.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import sde
from sde.engines.orderbook import OrderbookEngine

HERE = Path(__file__).resolve().parent
EXCHANGE = "SDE"
T0 = 1_790_000_000_000_000_000
STEP_NS = 1_000_000
READ_EVERY = 10

Side = dict[int, tuple[int, int]]   # price: (quantity, order count)
Levels = list[tuple[int, int, int]]


def model() -> sde.LogicalModel:
    from sde.testing.loader import model_from_neutral

    return model_from_neutral(json.loads((HERE / "model.json").read_text()))


def placement(logical: sde.LogicalModel) -> sde.PlacementMap:
    group = sde.colocation_groups(logical)[0]
    layout = sde.default_layout(logical, group, dialect="orderbook")
    columns = {k: dict(v) for k, v in layout.columns.items()}
    raw = {"tables": dict(layout.tables), "columns": columns}
    return sde.load_map(
        {
            "contract": 3,
            "model_version": logical.version,
            "map_version": 1,
            "groups": {"DepthLevel": {"source": {"id": "source", "engine": "ob", "layout": raw}}},
        },
        model=logical,
    )


def best_first(side: str, book: Side) -> Levels:
    return [(p, *book[p]) for p in sorted(book, reverse=side == "bid")]


class Feed:
    """The exchange: a walk of both sides, the same for every run and in both languages."""

    def __init__(self, depth: int) -> None:
        self.depth = depth
        self.state = 2_830_000_000   # the mulberry32 seed the TypeScript half starts from too
        self.mid = 100_000
        self.book: dict[str, Side] = {"bid": {}, "ask": {}}

    def random(self) -> float:
        """mulberry32: the same numbers as the TypeScript half."""
        self.state = (self.state + 0x6D2B79F5) & 0xFFFFFFFF
        t = self.state
        t = ((t ^ (t >> 15)) * (t | 1)) & 0xFFFFFFFF
        t ^= (t + (((t ^ (t >> 7)) * (t | 61)) & 0xFFFFFFFF)) & 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296

    def between(self, low: int, high: int) -> int:
        return low + int(self.random() * (high - low))

    def step(self, number: int) -> tuple[str, str, Levels]:
        """One step: the side it moves, how the exchange gives it - "snapshot" or "changes" - and
        the levels: the whole side, or the changes, a quantity of 0 removing a price."""
        side = "bid" if number % 2 == 0 else "ask"
        self.mid += self.between(-2, 3)
        sign = -1 if side == "bid" else 1
        near = self.mid + sign   # the best price this side may hold
        if number < 2 or self.random() < 0.2:
            book: Side = {}
            for k in range(self.between(self.depth // 2, self.depth + 1)):
                book[near + sign * k] = (self.between(1, 50), self.between(1, 6))
            self.book[side] = book
            return side, "snapshot", best_first(side, book)
        # Prices the walk left on the wrong side of the mid go first, as an exchange's matching
        # takes them; then a few prices on this side are set or removed, each price once.
        changes: Levels = []
        for price in sorted(self.book[side]):
            if price > self.mid if side == "bid" else price < self.mid:
                changes.append((price, 0, 0))
                del self.book[side][price]
        for _ in range(self.between(1, 6)):
            price = near + sign * self.between(0, self.depth)
            if price in [c[0] for c in changes]:
                continue
            if price in self.book[side] and self.random() < 0.4:
                changes.append((price, 0, 0))
                del self.book[side][price]
            else:
                value = (self.between(1, 50), self.between(1, 6))
                changes.append((price, *value))
                self.book[side][price] = value
        return side, "changes", changes


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--steps", type=int, default=600)
    parser.add_argument("--depth", type=int, default=20)
    args = parser.parse_args()
    dsn = os.environ.get("BOOK_STREAM_OB_DSN")
    if not dsn:
        print("set BOOK_STREAM_OB_DSN to orderbook://host:port", file=sys.stderr)
        return 2

    logical = model()
    engine = OrderbookEngine.from_dsn(dsn)
    engine.connect()
    engines: dict[str, Any] = {"ob": engine}   # as the trading example passes its adapters
    session = sde.Session(logical, placement(logical), engines)
    run = uuid.uuid4().hex[:8].upper()
    books = {"full": f"BSF{run}", "incremental": f"BSI{run}"}
    streams = {
        mode: session.book_stream("DepthLevel", symbol=symbol, exchange=EXCHANGE, mode=mode)
        for mode, symbol in books.items()
    }
    feed = Feed(args.depth)
    truth: dict[int, dict[str, Levels]] = {}
    try:
        for number in range(args.steps):
            at = T0 + (number + 1) * STEP_NS
            side, given, levels = feed.step(number)
            # The full stream is given the side whole; the incremental one what the exchange gave.
            streams["full"].update(side, best_first(side, feed.book[side]), timestamp_ns=at)
            if given == "snapshot":
                streams["incremental"].update(side, levels, timestamp_ns=at)
            else:
                streams["incremental"].apply(side, levels, timestamp_ns=at)
            if number % READ_EVERY == READ_EVERY - 1 or number == args.steps - 1:
                truth[at] = {s: best_first(s, feed.book[s]) for s in ("bid", "ask")}
        mismatches = []
        for at, expected in truth.items():
            for mode, symbol in books.items():
                got = session.book_at("DepthLevel", symbol=symbol, exchange=EXCHANGE, at_ns=at)
                if got != expected:
                    mismatches.append(f"{mode} at {at}")
    finally:
        session.close()
        engine.close()
    stats = {
        mode: {
            "updates": stream.stats.updates,
            "sides_whole": stream.stats.whole,
            "updates_of_changes": stream.stats.changes,
            "unchanged": stream.stats.unchanged,
            "levels_sent": stream.stats.levels_whole + stream.stats.levels_changes,
        }
        for mode, stream in streams.items()
    }
    print(json.dumps({"steps": args.steps, "books_read": len(truth) * len(books),
                      "mismatches": mismatches, "streams": stats}, indent=2))
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())

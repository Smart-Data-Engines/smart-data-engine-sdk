#!/usr/bin/env python3
"""A trading firm's application on Smart Data Engine: book depth, market trades, orders and fills.

The application names entities and fields - ``DepthLevel``, ``MarketTrade``, ``Order``, ``Fill`` -
and never an engine or a table. Where each group lives, and how it is laid out, is in the signed
placement map the control plane issued from a model's decision; this program reads that map from a
file and the engines' credentials from its own environment, and connects to each engine itself.

    python trading.py provision --map MAP --keys KEYS --project ID --engines ENGINES
    python trading.py run       --map MAP --keys KEYS --project ID --engines ENGINES \\
                                --run RUN [--books 4] [--updates 200] [--window WINDOW] \\
                                [--workload feed|accounts]
    python trading.py verify    --map MAP --keys KEYS --project ID --engines ENGINES \\
                                --run RUN [--books 4] [--updates 200]

``KEYS`` maps the control plane's key ids to base64 public keys. ``ENGINES`` maps each engine the
map names to the environment variables holding its DSN, one for the runtime login and one for the
provisioning login (``engines.example.json``); a DSN is ``postgresql://...``, ``clickhouse://...``
or ``orderbook://...``. ``run`` writes deterministic traffic for ``RUN`` - a 32-digit hex id - and
``verify`` reads every row of it back, every field except the sequence number the orderbook server
assigns, and exits 1 on any difference. ``--workload accounts`` adds what a risk desk does over the
same data: it pages through each account's orders, so the window shows reads of ``Order`` filtered
on ``account`` - the traffic an index on that field is for.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import sde

HERE = Path(__file__).resolve().parent
EXCHANGE = "sde"
T0 = 1_790_000_000_000_000_000
LEVELS = 5
ORDER_EVERY = 10
READ_EVERY = 20


def model() -> sde.LogicalModel:
    from sde.testing.loader import model_from_neutral

    return model_from_neutral(json.loads((HERE / "model.json").read_text()))


def placement(path: str, keys: str, logical: sde.LogicalModel) -> sde.PlacementMap:
    encoded = json.loads(Path(keys).read_text())
    public = {name: base64.b64decode(value) for name, value in encoded.items()}
    return sde.load_map(
        json.loads(Path(path).read_text()), model=logical, public_key=public, require_signature=True
    )


def adapter(dsn: str) -> Any:
    """One engine from its DSN. Credentials stay in this process; the control plane has none."""
    if dsn.startswith(("postgresql://", "postgres://")):
        from sde.engines.postgres import PostgresEngine

        return PostgresEngine(dsn)
    if dsn.startswith(("clickhouse://", "clickhouses://")):
        from sde.engines.clickhouse import ClickHouseEngine

        return ClickHouseEngine(dsn)
    if dsn.startswith("orderbook://"):
        from sde.engines.orderbook import OrderbookEngine

        return OrderbookEngine.from_dsn(dsn)
    raise SystemExit("a DSN starts with postgresql://, clickhouse:// or orderbook://")


def engines(config: str, placed: sde.PlacementMap, role: str) -> dict[str, Any]:
    entries = json.loads(Path(config).read_text())
    needed = sorted({m.engine for spot in placed.groups.values() for m in spot.all()})
    out = {}
    for name in needed:
        variable = entries[name][f"{role}_env"]
        dsn = os.environ.get(variable)
        if not dsn:
            raise SystemExit(f"{name}: set {variable} to its {role} DSN")
        engine = adapter(dsn)
        engine.connect()
        out[name] = engine
    return out


class Traffic:
    """Every row a run writes, derived from its id: ``verify`` regenerates what ``run`` wrote."""

    def __init__(self, run: str, books: int, updates: int) -> None:
        if len(run) != 32 or any(c not in "0123456789abcdef" for c in run):
            raise SystemExit("--run is 32 lowercase hex digits")
        self.run, self.books, self.updates = run, books, updates
        self.symbols = [f"S{run[:10].upper()}{book}" for book in range(books)]
        self.namespace = uuid.UUID(run)

    def depth(self, update: int, book: int) -> list[dict[str, Any]]:
        side = "bid" if update % 2 == 0 else "ask"
        top = 1_000_000 + (update % 50) * 10
        return [
            {
                "symbol": self.symbols[book],
                "exchange": EXCHANGE,
                "timestamp_ns": T0 + update * 1_000_000 + book,
                "side": side,
                "level": level,
                "price": top - level if side == "bid" else top + 1 + level,
                "quantity": 1 + (update * 7 + level) % 20,
                "order_count": 1 + level % 3,
                "sequence_number": None,
            }
            for level in range(LEVELS)
        ]

    def trade(self, update: int, book: int) -> dict[str, Any]:
        return {
            "symbol": self.symbols[book],
            "exchange": EXCHANGE,
            "trade_id": update,
            "price": 1_000_000 + (update % 50) * 10,
            "quantity": 1 + update % 9,
            "at_ns": T0 + update * 1_000_000 + book + 500,
        }

    def order(self, update: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        order_id = uuid.uuid5(self.namespace, f"order-{update}")
        at = datetime(2026, 10, 2, tzinfo=UTC) + timedelta(microseconds=update)
        order = {
            "id": order_id,
            "account": f"acct-{update % 3}",
            "symbol": self.symbols[update % self.books],
            "side": "buy" if update % 2 == 0 else "sell",
            "qty": Decimal(f"{1 + update % 5}.50000000"),
            "price": Decimal(f"{100 + update % 50}.12345678"),
            "placed_at": at,
        }
        fills = [
            {
                "id": uuid.uuid5(self.namespace, f"fill-{update}-{part}"),
                "order_id": order_id,
                "qty": Decimal("0.75000000"),
                "price": order["price"],
                "at": at + timedelta(microseconds=part + 1),
            }
            for part in range(2)
        ]
        return order, fills

    def orders(self) -> Iterator[int]:
        return iter(range(0, self.updates, ORDER_EVERY))


def run(arguments: argparse.Namespace) -> int:
    logical = model()
    placed = placement(arguments.map, arguments.keys, logical)
    connected = engines(arguments.engines, placed, "runtime")
    recorder = sde.Recorder(logical.version)
    session = sde.Session(
        logical, placed, connected, project_id=arguments.project, recorder=recorder
    )
    traffic = Traffic(arguments.run, arguments.books, arguments.updates)
    try:
        for update in range(traffic.updates):
            session.save_many(
                "DepthLevel",
                [row for book in range(traffic.books) for row in traffic.depth(update, book)],
            )
            session.save_many(
                "MarketTrade", [traffic.trade(update, book) for book in range(traffic.books)]
            )
            if update % ORDER_EVERY == 0:
                order, fills = traffic.order(update)
                with session.transaction("Order", "Fill"):
                    session.save("Order", order)
                    for fill in fills:
                        session.save("Fill", fill)
            if arguments.workload == "accounts" and update % ORDER_EVERY == 0:
                for account in range(3):
                    session.scan("Order", where={"account": f"acct-{account}"}, limit=100)
            if update % READ_EVERY == 0:
                book = update % traffic.books
                where = {"symbol": traffic.symbols[book], "exchange": EXCHANGE}
                session.scan(
                    "DepthLevel",
                    where=where,
                    bounds=sde.Range("timestamp_ns", T0, T0 + (update + 1) * 1_000_000),
                    limit=50,
                )
                session.scan("MarketTrade", where=where, limit=50)
                session.get("Order", {"id": traffic.order(update - update % ORDER_EVERY)[0]["id"]})
        session.measure_storage()
        window = recorder.roll()
        if arguments.window and window is not None:
            record = window.as_record(logical)
            Path(arguments.window).write_text(json.dumps(record, indent=2) + "\n")
    finally:
        session.close()
        for engine in connected.values():
            engine.close()
    print(json.dumps({"run": traffic.run, "updates": traffic.updates, "books": traffic.books}))
    return 0


def _pages(session: sde.Session, entity: str, where: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    after = None
    while True:
        page = session.scan(entity, where=dict(where), after=after, limit=1000)
        rows.extend(dict(row) for row in page.rows)
        if page.next_after is None:
            return rows
        after = page.next_after


def verify(arguments: argparse.Namespace) -> int:
    logical = model()
    placed = placement(arguments.map, arguments.keys, logical)
    connected = engines(arguments.engines, placed, "runtime")
    session = sde.Session(logical, placed, connected, project_id=arguments.project)
    traffic = Traffic(arguments.run, arguments.books, arguments.updates)
    depth_dialect = connected[placed.groups["DepthLevel"].source.engine].dialect
    mismatches: list[str] = []
    counts = {"depth": 0, "trades": 0, "orders": 0, "fills": 0}
    try:
        for book in range(traffic.books):
            where = {"symbol": traffic.symbols[book], "exchange": EXCHANGE}
            expected = sorted(
                (row for update in range(traffic.updates) for row in traffic.depth(update, book)),
                key=lambda row: (row["timestamp_ns"], row["side"], row["level"]),
            )
            stored = _pages(session, "DepthLevel", where)
            # On the orderbook engine the server numbers every update; a row without its number
            # was not read back from that engine.
            unnumbered = any(row["sequence_number"] is None for row in stored)
            if depth_dialect == "orderbook" and unnumbered:
                mismatches.append(f"{where['symbol']}: a row without the server's sequence number")
            seen = [{k: v for k, v in row.items() if k != "sequence_number"} for row in stored]
            wanted = [{k: v for k, v in row.items() if k != "sequence_number"} for row in expected]
            if seen != wanted:
                mismatches.append(
                    f"{where['symbol']}: depth differs ({len(seen)} of {len(wanted)} rows)"
                )
            counts["depth"] += len(seen)
            trades = _pages(session, "MarketTrade", where)
            if trades != [traffic.trade(update, book) for update in range(traffic.updates)]:
                mismatches.append(f"{where['symbol']}: trades differ ({len(trades)})")
            counts["trades"] += len(trades)
        for update in traffic.orders():
            order, fills = traffic.order(update)
            if session.get("Order", {"id": order["id"]}) != order:
                mismatches.append(f"order {update} differs")
            counts["orders"] += 1
            for fill in fills:
                if session.get("Fill", {"id": fill["id"]}) != fill:
                    mismatches.append(f"fill {fill['id']} differs")
                counts["fills"] += 1
    finally:
        session.close()
        for engine in connected.values():
            engine.close()
    print(json.dumps({"run": traffic.run, "verified": counts, "mismatches": mismatches}))
    return 1 if mismatches else 0


def provision(arguments: argparse.Namespace) -> int:
    logical = model()
    placed = placement(arguments.map, arguments.keys, logical)
    connected = engines(arguments.engines, placed, "provision")
    try:
        sde.prepare_schema(logical, placed, connected, project_id=arguments.project)
    finally:
        for engine in connected.values():
            engine.close()
    print(json.dumps({"provisioned": sorted(connected)}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("provision", "run", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--map", required=True)
        command.add_argument("--keys", required=True)
        command.add_argument("--project", required=True)
        command.add_argument("--engines", required=True)
        if name != "provision":
            command.add_argument("--run", required=True)
            command.add_argument("--books", type=int, default=4)
            command.add_argument("--updates", type=int, default=200)
        if name == "run":
            command.add_argument("--window")
            command.add_argument("--workload", choices=("feed", "accounts"), default="feed")
    arguments = parser.parse_args(argv)
    return {"provision": provision, "run": run, "verify": verify}[arguments.command](arguments)


if __name__ == "__main__":
    sys.exit(main())

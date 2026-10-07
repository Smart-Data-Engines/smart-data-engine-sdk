# A trading firm's application on three engines

A desk's market data and orders, written and read through the logical API in Python, TypeScript
and C++. [`model.json`](model.json) declares four entities:
- `DepthLevel` is L2 book depth, in exactly the shape our orderbook engine stores;
- `MarketTrade` is the trades of each book;
- `Order` and `Fill` must commit together, so they are one colocation group.

Where each group lives is decided by the control plane, from a model's proposal, and arrives as a
signed placement map. With PostgreSQL, ClickHouse and the orderbook engine in the registry, a good
answer puts depth on the orderbook, the orders on PostgreSQL (the one engine with transactions) and
the trades on ClickHouse. The map is then contract 6: the depth group carries no write generation,
because the orderbook engine cannot fence writes. That group stays where it was placed, while the
others can still move. This program reads the map from a file and connects to each engine itself.
The control plane has no credentials to any of them.

```bash
export TRADING_PG_DSN=... TRADING_PG_ADMIN_DSN=...     # postgresql://
export TRADING_CH_DSN=... TRADING_CH_ADMIN_DSN=...     # clickhouse://
export TRADING_OB_DSN=orderbook://desk:SECRET@host:port?tls=on&ca=/path/ca.pem
common="--map map.json --keys keys.json --project $PROJECT_ID --engines engines.example.json"
python trading.py provision $common
python trading.py run    $common --run $RUN --books 4 --updates 200 --window window.json
python trading.py verify $common --run $RUN --books 4 --updates 200
node trading.mjs run    $common --run $RUN2
node trading.mjs verify $common --run $RUN2 --depth-run $RUN
```

- `provision` prepares each engine's schema with the provisioning logins. There is nothing to create
  on the orderbook engine, so there it checks that the depth group is its shape.
- `run` writes deterministic traffic for one run id:
  - one depth update of five levels per book per step;
  - one trade per book per step;
  - an order with two fills in one transaction every tenth step;
  - reads of depth, trades and orders.

  It writes the SDK's telemetry window to `--window`, for the control plane's `observe`.
- `verify` reads every row back and compares it with what `run` wrote, and exits 1 on any difference.
  It compares every field except the sequence number, which the orderbook server assigns. A depth
  row without one fails verification.
- `trading.mjs` is the TypeScript half: orders, fills and trades, plus a read of the depth the Python
  half wrote. It imports `@smart-data-engines/sde`.
- [`cpp/examples/trading/trading.cpp`](../../cpp/examples/trading/trading.cpp) is the whole program
  in C++: `provision`, `run` and `verify` with these arguments and this traffic, so `trading.py
  verify` reads back a run it wrote, and it reads back a run `trading.py` wrote. It reads
  `model.json` from its working directory, and exits 1 when `verify` found a difference and 2 on
  anything else.

Two properties of the orderbook engine shape the program:
- A write is visible to another connection at the server's next flush tick (100 ms by default), or
  at once after `flush()`. `run` reads only what it wrote itself, and its adapter flushes before it
  reads.
- The engine does not enforce its key, so two updates at one nanosecond of one side of one book make
  later reads refuse. The generator gives every update its own instant.

The SDK runs this example on all three engines in CI, from all three libraries
(`python/tests/test_orderbook_trading_example.py`).

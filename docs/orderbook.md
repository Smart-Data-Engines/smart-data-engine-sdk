# The orderbook engine adapter

`sde.engines.orderbook.OrderbookEngine` connects a session to our own
[L2 orderbook engine](https://github.com/Smart-Data-Engines/low-cost-and-low-latency-orderbook-dbengine).
It is the third adapter and the first whose physical schema is not ours: the engine stores depth in
one shape fixed in its C++ source, so a group either *is* that shape or it cannot be placed there.

Every behaviour on this page that concerns the engine was measured against the commit
`.github/orderbook-engine.txt` pins. The CI job `orderbook` builds that commit and re-measures each
behaviour on every change.

## The shape

`sde.ORDERBOOK_SHAPE` has nine fields:
- `symbol` and `exchange`, both `string`: the book's address;
- `timestamp_ns` (`int64`);
- `side` (`string`, `ask` or `bid`);
- `level` (`int32`);
- `price` and `quantity` (`int64`);
- `order_count` (`int32`);
- `sequence_number` (`int64`, nullable).

`sde.ORDERBOOK_KEY` is `(symbol, exchange, timestamp_ns, side, level)`, in that positional order. An
entity with any other fields or types cannot be placed on this engine, and `default_layout` refuses it
with the whole expected shape in the message. `price` and `quantity` are integers in the engine's
sub-unit; a model that declares `decimal(12,2)` is a different model.

**Which fields may be null is part of the shape.** A model declares `sequence_number` nullable and no
other field (`sde.ORDERBOOK_NULLABLE`). Over TCP the server assigns the number, so a model that made it
required could not write at all. The engine stores no null anywhere else, so a model that allowed one
in `quantity` could save a row the engine refuses. Both passed the shape check until 2 October 2026,
on an engine a group can never be moved off. `default_layout` and `sde.fixed_schema_mismatch` refuse
both now: the second takes `nullable`, which `sde.group_nullable` derives from the model, and the
engine's facts list it as `fixed_shape.nullable` and `fixed_shape.assigned_by_server`.

## Connecting

The engine's Python client is not on PyPI. Install it from the engine's repository: its `python/`
directory, and for local mode its shared library, `liborderbook_shared.so`, through `OB_LIB_PATH`.

| Mode | Construction | What it is |
|---|---|---|
| local | `OrderbookEngine("/path/to/data")` | the engine runs in this process, on that data directory |
| TCP | `OrderbookEngine(host=..., port=...)` | a running `ob_tcp_server` |

TCP takes the server's client credentials and TLS:

```python
engine = OrderbookEngine(
    host="ob1.internal", port=9443,
    auth=("trading-app", secret),            # an identity from the server's --auth-secret-file
    tls=True, tls_ca_file="/etc/ssl/ca.pem",  # the server runs --tls-client
    timeout=10.0,
)
```

The same as a DSN, for a deployment that keeps connections in environment variables beside its
PostgreSQL and ClickHouse DSNs:

```
orderbook://trading-app:SECRET@ob1.internal:9443?tls=on&ca=/etc/ssl/ca.pem&timeout=10
```

Rules for the DSN:
- the identity and the secret are percent-decoded;
- `verify=off` is accepted only together with `tls=on` and only when written out;
- an unknown parameter is refused.

Every refusal names the part of the DSN that is wrong and never repeats the DSN, which carries the
secret; `repr()` shows the identity, never the secret.

Settings that would do nothing are refused rather than ignored:
- credentials or TLS in local mode;
- a CA file without TLS;
- `tls_verify=False` without TLS.

`connect()` asks a TCP server for its capabilities and refuses a server that cannot store a write's
event time, which would stamp every update with its arrival instead. After an exchange that does not
finish, the connection is not used again. TypeScript drops it, and its next `connect()` opens a new
one. In Python the engine's client closes it, and `connect()` pings a connection it already has and
replaces a closed one. A write with no connection is refused before anything is sent: `not
connected; call connect() first`.

## Writing

**One update is one side of one book at one instant.** The engine's write API has no level
parameter: a price's level is its position in an update. So:
- `Session.save` writes one row, and only at level 0. A row at level 3 would be stored at level 0, so
  it is refused.
- `Session.save_many` writes whole updates. Its rows are grouped by
  `(symbol, exchange, side, timestamp_ns)` in the order they first appear. Each group must be levels
  0 to n-1, each once; a gap or a repeat refuses the whole batch before anything is sent.

**Over TCP the sequence number is the server's.** The server numbers every update per book. Write
`sequence_number` as `None` and read the server's number back. A number the client chose is refused
before it is sent. Measured on 2 October 2026: the current server refuses it, and before that release
it accepted and discarded it, so the row read back carried a different number from the one written.
In local mode you choose the number, from 1; 0 is the engine's "unknown".

**Values the engine cannot store are refused before sending**, with the field named:
- a negative time, quantity or order count;
- a level past 999 (the engine stores 1000 levels per side);
- whitespace, a quote, a backslash or a control character in a symbol or an exchange.

Measured: the server refuses `INSERT BTC USD ...` as "unexpected token" and a negative quantity as
"unknown command", which tells a client nothing.

**Use an engine at or after `c1f14c0`** (the engine's #198). Every quantity from 0 to 2^63 - 1 is
admitted, and an engine before that commit wrote exactly 2^60 - 1 as its codec's fallback marker:
that quantity read back as 2^64 - 1, and every later quantity in the segment as 0. Measured against
`971dda2` from both libraries. The slices write the edges of the range, 2^60 - 1 among them, and read
them back, so the pin cannot move below that commit unnoticed.

**A batch is not a transaction.** Over TCP the updates go in pipelined round trips of 64. If the
server refuses some of them, the error names the first refused update and how many were stored. If
the connection fails, the error names the outcome as unknown - the rest may have been stored in full,
in part or not at all - and closes the adapter, so a reply still on its way is not read as the answer
to the next write. Read the book before writing again.

## Reading

**`Session.get`** reads one row by its full key. Two rows with one key - the engine does not enforce
the key - refuse rather than answer with either.

**`Session.scan`** reads one book, in key order:
- `where` must fix `symbol` and `exchange`, because the engine's query language takes them in its
  FROM clause and has no scan across books;
- `where` may also fix `side`, `level` and `price`;
- `bounds` may range over `timestamp_ns` or `price`;
- it can run ascending or descending, and pages with `after` as for any engine.

**The engine answers in arrival order, not in event-time order** (measured), and its `LIMIT` keeps the
first rows to arrive, so a page cannot be the engine's `LIMIT`. The adapter assembles each page from
complete windows of time, sorted in this process:
- the first window is one second;
- it doubles while windows are sparse and halves when one holds more than 100 000 rows;
- an instant holding more than that is refused.

The cost of a page is the number of rows its windows hold, which shows in the window's `range_read`
latency. An unbounded scan of a sparse book takes about 64 empty reads to reach the end of the time
range. Bound it by time.

**`Session.count` and `Session.summarize` are refused by name.** The engine's aggregates read the live
book, not the stored rows, and a count here would be a full scan. The refusal comes before the call
is timed, so a window does not record it as an engine error.

**A row outside the model's types is refused, not returned.** The engine stores unsigned 64-bit
times, quantities and sequence numbers, so it can hand back a value the model's `int64` cannot hold.
No write of this library stores one. An engine before `c1f14c0` read a stored quantity of 2^60 - 1
back as 2^64 - 1, and the adapter returned it. Now a time, quantity or sequence number above
2^63 - 1, an order count above 2^31 - 1 or a level above 999 refuses the read, naming the field and
the range but not the value, which is read data.

**A book nothing has been written to** answers `OB_ERR_NOT_FOUND` over TCP, and reads as empty. In
local mode the C API keeps no reason for a failed query, so an unknown book and a failure read alike.
There a failed read stays a failure and says why.

## Telemetry

The window sees every call:
- `bulk_write` with its rows;
- `point_read`;
- `range_read` over `timestamp_ns` or `price`, with `filtered_on`.

A group's size stays unknown (`total_bytes` in `missing`, `unsupported` in
`Session.measure_storage().unavailable`), because the engine reports no size per book.

## What this engine does not do

It has no transactions, so a group that declared atomicity cannot be placed here. It has no write
fences, so a group here carries no write generation (map contract 6, see
[generation-maps.md](generation-maps.md)) and cannot take part in a staging, a cutover or an index
build. The other groups of the same map move as usual, and the local operator needs no binding for
this engine. It has no query planner, so `explain_plan` refuses. Each refusal says so in its message.

`sde.engine_facts("orderbook")` states all of this as data, for the control plane to give a model that
decides placement; the tests hold each fact against the adapter and the engine.

## From TypeScript

`OrderbookEngine` from `@smart-data-engines/sde/engines/orderbook` is the same adapter for Node,
over the engine's TCP protocol only: the in-process mode loads the engine's shared library, which
this runtime has no binding for. It speaks the protocol itself, with no client library, and makes
the same decisions with the same refusals:

```typescript
import { OrderbookEngine } from '@smart-data-engines/sde/engines/orderbook'

const engine = OrderbookEngine.fromDsn(process.env.SDE_ORDERBOOK_DSN!)
// or: new OrderbookEngine({ host, port, auth: { identity, secret }, tls: true, tlsCaFile })
await engine.connect()
```

`timestamp_ns`, `price`, `quantity` and `sequence_number` are `bigint` when read, as every `int64`
in this library is, and a write takes a `bigint` or a safe `number`. A nanosecond timestamp is past
2^53, so in practice it is a `bigint`. `orderbook.slice.test.ts` writes a book from each library and
reads it from the other, in both directions, against a live server.

## Who sees a write, and when

A process reads its own writes: the adapter flushes before a read when it has written anything since
the last flush. **Another connection sees a write at the server's next flush tick** -
`--flush-interval-ms`, 100 ms by default - or at once after a `FLUSH`. So with a writer and a reader in
two processes, a read can miss the last tick's writes. Measured on 2 October 2026: a reader in another
process found none of a book written a moment earlier, until the writer flushed. Call `flush()` on
the writer where a reader elsewhere must see the data at once; it is not done after every write,
because a flush in local mode costs 3.4 ms (measured).

## Running the slices

```bash
git clone https://github.com/Smart-Data-Engines/low-cost-and-low-latency-orderbook-dbengine ../ob
git -C ../ob checkout "$(cat .github/orderbook-engine.txt)"
cmake -S ../ob -B ../ob/build -DCMAKE_BUILD_TYPE=Release -DOB_BUILD_TESTS=OFF
cmake --build ../ob/build --target ob_tcp_server orderbook_shared
../ob/build/ob_tcp_server --port 59090 --data-dir /tmp/ob-sde &
OB_LIB_PATH=$PWD/../ob/build/liborderbook_shared.so PYTHONPATH=$PWD/../ob/python \
  SDE_ORDERBOOK=1 SDE_ORDERBOOK_TCP=127.0.0.1:59090 \
  python/.venv/bin/python -m pytest python/tests/test_orderbook_*.py
```

`SDE_ORDERBOOK_SECURE_DSN` adds a server with `--auth-secret-file` and `--tls-client`. The CI job
generates a CA, a certificate and a secret for one. The TypeScript slices run with the same two
variables (`npx vitest run tests/orderbook.slice.test.ts` in `typescript/`), and `SDE_PYTHON` names
an interpreter with this SDK and the engine's client for the half written in Python. `test_orderbook_three_engines.py` needs
`SDE_POSTGRES_DSN` and `SDE_CLICKHOUSE_DSN` as well, as every live slice does.

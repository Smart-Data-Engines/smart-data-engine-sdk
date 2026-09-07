# What happens when something fails

*In English because it is for you, not for us.*

You are about to put a library in your request path and hand somebody else your physical schema.
"What happens when X breaks?" is the right question to ask before that rather than after, so this
page exists before the first sale rather than after the first incident. Every row below is either
measured against a real engine or pinned by a test named in the last column, and
`python/tests/test_failure_semantics.py` fails if any of them stops being true.

**It applies to both supported libraries.** Every bound and every message below holds in Python and
in TypeScript, and each has its own tests: `typescript/tests/failure.test.ts` for the bounds, which
needs no server because every socket it waits on is one it starts itself, and
`typescript/tests/cut-connection.live.test.ts` for what the second call says after a connection is
cut. Two of the rows below were **defects** when this page was first written, and one of them was a
defect again in the second language for a different reason - which is the argument for a page like
this existing at all rather than a description of one.

## Three rules, and everything else follows from them

1. **We are not in your data path.** The library connects to your engines directly. Our control
   plane holds no credentials to them and the library makes no network call to us at all, so an
   outage of ours is not an outage of yours. Measured, not asserted: an interpreter audit hook in a
   subprocess records every connection the library attempts, and every host and port in that list
   is one of your engines.
2. **A write your engine did not accept comes back to your code as an error.** It is never retried
   into a different engine and never dropped quietly.
3. **A retry happens only where the operation is known to be idempotent, and only against the same
   engine.** In practice that means the migration's own backfill, which is idempotent by
   construction. Your `save()` is not retried by us.

## The failures, one row each

| What failed | What your call does | What is retried | What can be lost | Pinned by |
|---|---|---|---|---|
| The engine is not listening | `connect()` raises `EngineError` carrying the driver's own reason. Measured: 0.16 s to fail against a closed port | nothing | nothing: no operation was attempted | `test_failure_semantics.py` |
| The engine accepts the socket and never answers | `connect()` raises after **10 s** on PostgreSQL and **15 s** on ClickHouse (measured; before these bounds existed the call did not return within 45 seconds). Both are defaults this library supplies and a value in your DSN wins | nothing | nothing | `test_failure_semantics.py`, `failure.test.ts` |
| The connection dies under an operation | the failing call raises `EngineError` carrying what the server said. Every call after it raises "the connection is closed", plus the sentence that matters: this library does not reopen a connection it was handed | nothing | the operation either reached the engine or did not, and the error is your signal. Nothing was written twice | `test_failure_semantics.py` |
| A query is simply slow | not a failure, and not bounded by us. Set `statement_timeout` in your PostgreSQL DSN, or `max_execution_time` on your ClickHouse server. Measured on the Python side, where a bound does exist past the handshake: with `send_receive_timeout` at 15 s, a 23-second and a 24-second query both returned normally | nothing | nothing | see "what we do not do" |
| A read asked for freshness | goes to the source, before any routing table is consulted. A derived copy is behind by design, so it cannot answer this | nothing | nothing | `routing/` conformance vectors |
| A write during a migration | goes to the source, and additionally to every fan-out target the map names. The additional write is **never authoritative**: if the copy refuses it, your call still succeeds and the failure is counted | nothing | nothing you were told was written. The copy's gap is what the migration's verification exists to find, and it refuses to switch reads until the copy matches value by value | `test_dual_write.py`, and the control plane's gate |
| The map is for a different model version | refused when the map loads, naming both versions. Never reconciled: the difference between two models cannot be guessed from either side | nothing | nothing | `errors/007` |
| A map older than the one already applied | refused, against a watermark in **your own engine** (`sde_map_state`, append-only, the watermark is `max()`). Only signed maps are checked, so the no-account mode costs nothing here | nothing | nothing. This is what stops a stale file rerouting your writes mid-migration | `test_rollback_protection*.py` |
| We are unreachable, or gone | nothing happens. The library never calls us; there is no heartbeat, no licence check and no phone-home | nothing | nothing | `test_no_account*.py` |
| Your subscription lapses | nothing happens. There is no expiry in the library and no date anywhere in a placement map, so your application keeps running on the last map it was given. What stops is us issuing new ones | nothing | nothing | `test_no_expiry.py` |
| The schema does not match the map | a **missing** column is refused at `ensure_schema`, with the difference printed, and so is a column whose **type** differs from the one the map declares - both are printed. An **extra** column is logged and accepted, because you may have added it outside this library and the map says nothing about it | nothing | nothing: the refusal happens at startup rather than at the first write that needs the column. Types were not compared until 7 September 2026, and until then a column that was `text` where the map said `timestamptz` was reported as a good schema | `test_schema_statements.py`, `test_postgres_slice.py`, `test_clickhouse_slice.py` |
| The engine has no transactions | `transaction()` refuses on ClickHouse rather than pretending. One colocation group lives in one engine precisely so that "what commits together" is answerable | nothing | nothing | `test_clickhouse_slice.py` |
| The engine imposes its own schema | our order book engine stores a shape fixed in its own source, so rendering DDL for it yields **no statements** - which means "nothing to run", not "no tables". A `get()` that finds two rows under one key **refuses** rather than choosing one | nothing | nothing | `test_orderbook_adapter.py` |
| Telemetry cannot be recorded | dropped and counted. Never at the cost of your operation, and the counter is readable with `sde.internal_failures()` | nothing | telemetry only, and you can see how much | `test_telemetry.py`, `test_internal_failures.py` |
| An event name this library does not know reaches its own logger | counted, not raised. A diagnostic that takes down a request is worse than a missing diagnostic | nothing | one log line | `test_no_account.py` |

## What we deliberately do not do

- **No automatic reconnect.** The library uses the connection you handed it. A library that
  reconnected silently would also be retrying silently, and rule 3 above says when a retry is
  allowed. Reconnecting is `close()` then `connect()`, and the error message says so.
- **No connection pooling.** That is Tier 3 in the format contract and it is your pool's job. Hand
  the session a connection from it.
- **No statement timeout.** An analytical query legitimately takes minutes, and a library that cut
  it off would be deciding something about your workload that it cannot know. The one exchange this
  library does know the shape of is the connection handshake, which is why that is the only thing
  it bounds. The two libraries reach that position differently and it is worth knowing which you
  have: in Python the driver's read bound applies to every request and a slow query survives it
  anyway (measured, above); in TypeScript only the handshake request carries a timeout at all, which
  is one fewer thing to be right about.
- **No fallback to another engine.** A read that cannot be served where the map sends it fails.
  Answering it from somewhere else would mean answering from a copy you did not ask for.
- **No retry of a write.** See rule 2.

## Two things a driver does that we cannot promise about

Our own libraries print nothing: no logging is configured, no writes to standard output or standard
error, and a test over the source pins the absence of every such channel. The **drivers** underneath
are not ours. `clickhouse-connect` writes "Unexpected Http Driver Exception" to standard error when a
connection fails, and `psycopg` can be configured to log. If silence in your logs matters, that is a
driver configuration and this page is telling you where to look rather than claiming it is handled.

The second is timing. Every duration on this page was measured on one machine, on loopback, against
containers. They are the shape of the behaviour, not a service level: your network decides the
numbers and your DSN decides the bounds.

## How to check any of this yourself

```bash
cd python && make engines-up
SDE_POSTGRES_DSN=... SDE_CLICKHOUSE_DSN=... .venv/bin/python -m pytest tests/test_failure_semantics.py -v
```

The live half of that file kills a connection under a running session, points the adapter at a
closed port and at a socket that accepts and stays silent, and asserts what comes back. If it passes
against your engines, the table above is true for your deployment and not only for ours.

## What the second language cost, and why it is on this page

Requirement 17.5 says a Tier 2 implementation brings its own drivers, and that four languages times
two engines is eight integrations. Writing two of the eight found two things worth telling you,
because both are about *your* process rather than ours.

**A `connect_timeout` in a PostgreSQL DSN does nothing in Node.** `pg` derives its own connect bound
from a code-level option and *overwrites* whatever the connection string said, so the value you
wrote is parsed and discarded. The first version of the TypeScript adapter did what the Python one
does - leave your value alone if you set one - and the result was the worst of both: your bound
ignored by the driver and ours suppressed by yours, so a DSN asking for two seconds got **no bound
at all**. It translates the parameter now, so your value wins and a bound always exists.

**An unhandled driver error can kill your process.** `pg.Client` is an `EventEmitter` and emits
`error` when the server terminates a connection between queries - a restart, a failover, an
administrator. Node's rule for an `error` event with no listener is to throw it, with no call of
yours on the stack. The adapter listens, records it, and reports it as part of the next call's
message. Neither of these has an equivalent in Python, which is the general point: the contract is
identical across languages and the *failure modes of the runtimes are not*, so each library measures
its own.
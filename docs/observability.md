# What this library says, and what it never says

This library runs inside somebody else's application. Two promises follow from that, and both are
things you can check yourself rather than take from us.

**It does not call us.** Not to check a licence, not to report telemetry, not on import. There is
no address in the code and no field in the placement map format where one could be written, and the
set of modules the library imports — outside the engine adapters, which connect to *your* engines —
is an allowlist with nothing network-capable on it.

**It does not talk about your account.** Running on a hand-written, unsigned map is a supported
mode (see the no-account mode in the README), and the library emits nothing in that mode that it
does not emit with a map we signed. There is no line telling you what you would get if you paid.

Both are checked mechanically, in this repository, by tests you can run:

| Claim | Where it is checked |
|---|---|
| No connection is attempted to anything but your own engines | `python/tests/test_no_account_live.py` — an interpreter audit hook, with a connection the test makes itself as the control; `typescript/tests/no-account.live.test.ts` — `net.Socket.prototype.connect` wrapped before the library is imported, with the same control |
| The import surface has nothing that could open one | `python/tests/test_no_account.py` |
| The no-account mode emits no event the account mode does not | `python/tests/test_no_account_live.py`; `cpp/tests/unit/test_invariants.cpp`, over the same work on a map signed and unsigned |
| The library has no channel louder than one INFO event | `python/tests/test_no_account.py` |
| The TypeScript library has no output channel at all | `typescript/tests/silence.test.ts` |
| Importing the TypeScript library resolves no driver and reaches no socket module | `typescript/tests/boundary.test.ts` — over the import closure of `src/index.ts`, because ES modules offer no registry to read at runtime |
| Nothing in the library knows when a map was issued | `python/tests/test_no_expiry.py`, `typescript/tests/no-expiry.test.ts` and `no-expiry.live.test.ts` |
| The wall clock is read only to stamp what happened | `python/tests/test_no_expiry.py` and `typescript/tests/no-expiry.test.ts` name every read and find none compared with anything; `cpp/tests/unit/test_invariants.cpp` names every read |

The TypeScript row above is the weaker of the two mechanisms and says so. Python runs a subprocess
and reads `sys.modules`, which is a fact about what was loaded; ES modules have no equivalent, so
what is checked there is the **import closure of the entry point**, computed from the source. It
answers "can this reach a socket" rather than "did it", and the two engine adapters are behind their
own subpath exports precisely so that the answer is no. They do reach one, deliberately, and the
same test asserts that - a check that passes by finding nothing has to be shown a case it finds.

The audit-hook test is worth one note, because it is the one that could be written to look
convincing and prove nothing. An audit hook sees the Python socket layer and nothing below it:
measured, `psycopg` connects through `libpq` in C and raises no audit event at all, while
`clickhouse-connect` goes over `http.client` and raises fourteen. So the test does not assert that
the list of attempts is empty — an empty list is what a blind instrument produces. It asserts that
the list is **not** empty and that every host and port in it is your engine.

## The event vocabulary

Logging goes through the standard `logging` module under the `sde` logger, at INFO, and only if
your application has configured a handler. Event names are a closed set so that an alert built on
one keeps working; the values are in `record.sde_fields` rather than interpolated into the name.

The C++ and TypeScript libraries have no logger: an event goes to a sink you pass in, a function
of the event's name and an object of its fields (`SessionOptions::log` and the adapters' `log` in
C++; `events` in TypeScript's `loadMap`, `Session.open`, `backfill` and the adapters'
constructors). Without one they say nothing. Their vocabulary is a subset of the list below,
held to it by a test: a routing decision, a model built and a telemetry window are events only
here, and TypeScript, whose PostgreSQL adapter does not check index collations, has no
`sde.schema.text_collation`. A sink that throws changes nothing about the operation it reports
on; TypeScript counts it under `events.sink` in `internalFailures()`.

<!-- events:begin -->
- `sde.explain.no_query_tree`
- `sde.internal.error`
- `sde.map.forward_only`
- `sde.map.loaded`
- `sde.map.rejected`
- `sde.map.rollback_unprotected`
- `sde.migration.backfill_progress`
- `sde.migration.divergence`
- `sde.model.built`
- `sde.orderbook.flushed`
- `sde.route.fallback`
- `sde.route.resolved`
- `sde.schema.applied`
- `sde.schema.extra_columns`
- `sde.schema.physical_mismatch`
- `sde.schema.text_collation`
- `sde.telemetry.dropped`
- `sde.telemetry.storage_rejected`
- `sde.telemetry.storage_unavailable`
- `sde.telemetry.window_closed`
- `sde.write.failed`
<!-- events:end -->

This list is generated from `sde.logging.EVENTS` and a test fails if the two drift apart, because a
document listing a vocabulary is otherwise a second copy of it to keep true.

`sde.telemetry.window_closed` is worth a paragraph, since it used to be called
`sde.telemetry.window_sent`. Nothing is sent. A telemetry window is aggregated in your process and
buffered; `Recorder.pending()` hands you the aggregates and `Recorder.acknowledge()` releases them
once you have taken them. Whether they ever reach us is a decision made by your code, not by ours.
The old name was renamed the day somebody read this vocabulary to find out whether the library
phones home and found a line saying it had.

`sde.schema.physical_mismatch` is a report, never a refusal. A running session emits it when an
existing table's sort key, primary-key order, partition or index is not the physical design the
placement map declares, and lists the tables in its fields; `Session.physical` holds the details.
The rows are the same rows whatever the design, so an application keeps serving them: a difference
in performance is not an outage this library is allowed to cause. `prepare_schema` refuses the same
differences, because that is where a person can act on them. On ClickHouse a runtime login without
`SELECT` on `system.data_skipping_indices` reports declared indexes as *unverified* rather than
failing to start — see [runtime roles](runtime-roles.md).

`sde.schema.text_collation` is a report too, and it names an index rather than a difference. A
PostgreSQL table created before 2 October 2026 has text columns in the default collation. Its
primary key, and any declared index created the same way, cannot serve a scan, count or summary of
this library, all of which compare text in `COLLATE "C"`. The event is emitted once per such index,
with the table, the index, the text columns and the remedy, a staging into a fresh copy or an
administrator's `ALTER COLUMN`. Why it is not a mismatch, and what each remedy costs, is in
[the format contract](format-contract.md) §7a.

## The operation hook

A session given a hook calls it once for every operation it completes, after the operation and in
the thread or task that ran it, with one record:

| Field | What it holds |
|---|---|
| `operation` | the method: `save`, `save_many`, `get`, `scan`, `count`, `summarize`, `search`, `book_stream`, `book_at`, or `transaction` for a whole transaction from its opening to its end |
| `entity`, `group` | in your own names, also when the model's identifiers are hashed |
| `shape` | the operation shape's id, as telemetry names it; empty for a transaction |
| `engine`, `dialect` | the engine's name in the placement map and its dialect - never its address |
| `materialization` | the copy that served the operation |
| `duration_ns` (`durationNs` in TypeScript) | how long it took, on the monotonic clock |
| `rows` | rows written or returned; 1 for a count or a summary |
| `outcome` | `ok`, the class of an error of this library's hierarchy, or `unexpected` |

Nothing an operation carried reaches it: no key, no row, no filter, no statement, no connection
string and no error message, since a driver's message can quote a key. Each library's tests put one
value in every row, key and filter of every operation and find it nowhere in what the hook was
given - and, as the control, in the rows the operations return (`python/tests/test_operation_hook.py`,
`typescript/tests/operation-hook.test.ts`, `cpp/tests/unit/test_operation_hook.cpp`). There is no
start time: the library reads the wall clock only to stamp what happened, and a span's start is the
time the hook runs minus `duration_ns`.

A hook that raises changes nothing about the operation's result; it is counted under
`session.hook` in `internal_failures()`. A slow hook is a slow application: it runs before the
operation returns. Pass it as `Session(..., hook=...)` in Python, `Session.open(..., { hook })` in
TypeScript and `SessionOptions::hook` in C++.

### OpenTelemetry

This library depends on no tracing or metrics package and has no exporter. A span and a histogram
value for each operation are a few lines of your own code. In Python, with the `opentelemetry-api`
package:

<!-- bridge:python -->
```python
import time
from typing import Any

from opentelemetry.trace import Status, StatusCode

import sde

def opentelemetry_hook(tracer: Any, histogram: Any) -> sde.OperationHook:
    """A span and a histogram value for each operation the session completes."""

    def hook(record: sde.OperationRecord) -> None:
        end = time.time_ns()
        span = tracer.start_span(
            f"sde.{record.operation}",
            start_time=end - record.duration_ns,
            attributes={
                "db.system": record.dialect,
                "sde.entity": record.entity,
                "sde.group": record.group,
                "sde.shape": record.shape,
                "sde.engine": record.engine,
                "sde.materialization": record.materialization,
                "sde.rows": record.rows,
            },
        )
        if record.outcome != "ok":
            span.set_status(Status(StatusCode.ERROR, record.outcome))
        span.end(end_time=end)
        histogram.record(
            record.duration_ns / 1e9,
            attributes={
                "sde.operation": record.operation,
                "db.system": record.dialect,
                "sde.outcome": record.outcome,
            },
        )

    return hook
```

with `tracer = trace.get_tracer("sde")` and `histogram = metrics.get_meter("sde").create_histogram(
"sde.operation.duration", unit="s")`. In TypeScript, with `@opentelemetry/api`:

<!-- bridge:typescript -->
```typescript
import { SpanStatusCode, type Histogram, type Tracer } from '@opentelemetry/api'
import type { OperationHook } from '@smart-data-engines/sde'

/** A span and a histogram value for each operation the session completes. */
function opentelemetryHook(tracer: Tracer, histogram: Histogram): OperationHook {
  return (record) => {
    const end = performance.timeOrigin + performance.now()
    const span = tracer.startSpan(`sde.${record.operation}`, {
      startTime: end - record.durationNs / 1e6,
      attributes: {
        'db.system': record.dialect,
        'sde.entity': record.entity,
        'sde.group': record.group,
        'sde.shape': record.shape,
        'sde.engine': record.engine,
        'sde.materialization': record.materialization,
        'sde.rows': record.rows,
      },
    })
    if (record.outcome !== 'ok') span.setStatus({ code: SpanStatusCode.ERROR, message: record.outcome })
    span.end(end)
    histogram.record(record.durationNs / 1e9, {
      'sde.operation': record.operation,
      'db.system': record.dialect,
      'sde.outcome': record.outcome,
    })
  }
}
```

And in C++, with opentelemetry-cpp, where a span ends on the steady clock, so it starts on both:

<!-- bridge:cpp -->
```cpp
#include <opentelemetry/metrics/sync_instruments.h>
#include <opentelemetry/trace/tracer.h>

#include "sde/hook.hpp"

/// A span and a histogram value for each operation the session completes.
sde::OperationHook opentelemetry_hook(
    opentelemetry::nostd::shared_ptr<opentelemetry::trace::Tracer> tracer,
    opentelemetry::metrics::Histogram<double>* histogram) {
  return [tracer, histogram](const sde::OperationRecord& record) {
    const auto took = std::chrono::nanoseconds(record.duration_ns);
    const auto end = std::chrono::steady_clock::now();
    opentelemetry::trace::StartSpanOptions options;
    options.start_system_time =
        opentelemetry::common::SystemTimestamp(std::chrono::system_clock::now() - took);
    options.start_steady_time = opentelemetry::common::SteadyTimestamp(end - took);
    auto span = tracer->StartSpan("sde." + record.operation,
                                  {{"db.system", record.dialect},
                                   {"sde.entity", record.entity},
                                   {"sde.group", record.group},
                                   {"sde.shape", record.shape},
                                   {"sde.engine", record.engine},
                                   {"sde.materialization", record.materialization},
                                   {"sde.rows", static_cast<std::int64_t>(record.rows)}},
                                  options);
    if (record.outcome != "ok") {
      span->SetStatus(opentelemetry::trace::StatusCode::kError, record.outcome);
    }
    opentelemetry::trace::EndSpanOptions ending;
    ending.end_steady_time = opentelemetry::common::SteadyTimestamp(end);
    span->End(ending);
    histogram->Record(static_cast<double>(record.duration_ns) / 1e9,
                      {{"sde.operation", record.operation},
                       {"db.system", record.dialect},
                       {"sde.outcome", record.outcome}},
                      opentelemetry::context::Context{});
  };
}
```

Each block is the code its language's test runs (`python/tests/test_otel_bridge.py`,
`typescript/tests/otel-bridge.test.ts`, `cpp/tests/unit/test_otel_bridge.cpp`) against stand-ins
with OpenTelemetry's shape, held equal to this page by the same test. Which events and which
numbers deserve an alert is in [alerts](alerts.md).

## What is not here

No log line carries one of your rows. Telemetry is keyed by the *shape* of an operation — assembled
from the structure of a call, never from its arguments — and the backfill progress marker is a row
count and never a key value, for the same reason.

No log line is a warning, in the sense the standard library gives that word: the library never
emits above INFO. If you want it silent, do not configure a handler for the `sde` logger; that is
already the default.

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
| No connection is attempted to anything but your own engines | `python/tests/test_no_account_live.py` — an interpreter audit hook, with a connection the test makes itself as the control |
| The import surface has nothing that could open one | `python/tests/test_no_account.py` |
| The no-account mode emits no event the account mode does not | `python/tests/test_no_account_live.py` |
| The library has no channel louder than one INFO event | `python/tests/test_no_account.py` |
| The TypeScript library has no output channel at all | `typescript/tests/silence.test.ts` |
| Nothing in the library knows when a map was issued | `python/tests/test_no_expiry.py` |

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
- `sde.telemetry.dropped`
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

## What is not here

No log line carries one of your rows. Telemetry is keyed by the *shape* of an operation — assembled
from the structure of a call, never from its arguments — and the backfill progress marker is a row
count and never a key value, for the same reason.

No log line is a warning, in the sense the standard library gives that word: the library never
emits above INFO. If you want it silent, do not configure a handler for the `sde` logger; that is
already the default.

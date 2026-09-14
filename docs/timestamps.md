# Exact timestamps in TypeScript

Both TypeScript adapters return `Timestamp` for `timestamp` and `timestamptz`. It is an immutable
value with a `bigint` count of microseconds since the Unix epoch. Python continues to use `datetime`.
Calendar `date` columns remain `YYYY-MM-DD` strings in TypeScript.

```typescript
import { Timestamp } from '@smart-data-engines/sde'

const at = Timestamp.from('2026-09-12T11:30:15.123456+02:00')
at.toISOString() // '2026-09-12T09:30:15.123456Z'
const next = Timestamp.fromEpochMicroseconds(at.epochMicroseconds + 1n)
at.epochMicroseconds < next.epochMicroseconds // true
JSON.stringify({ at }) // '{"at":"2026-09-12T09:30:15.123456Z"}'
```

Pass `Timestamp` directly to `Session.save`, adapter writes, and key predicates. A `Date` remains
accepted on writes, with the precision it actually carries; the adapters serialize its UTC value
explicitly, including for a timezone-free `timestamp` column. Reading either input returns
`Timestamp`. Code that previously expected `Date` on a read must use `toISOString()` for text,
`epochMicroseconds` for exact comparisons, or `toDate()` when the instant is exactly representable.

`toDate()` throws if microseconds would be lost. It never rounds. Calling `Number(timestamp)` also
throws: arithmetic and ordering must name `epochMicroseconds`. JSON uses the same six-digit UTC
string as `toISOString()`; recover the value with `Timestamp.from()`.

`Timestamp.from()` accepts ISO date-time text with a `T` or space separator, optional fractional
seconds (one through six digits), and `Z` or a signed numeric UTC offset. An absent offset means
UTC, including the wall-clock value of a timezone-free SQL timestamp; it never means the process
timezone. It rejects invalid calendar dates, leap seconds, infinities and fractions finer than a
microsecond. UTC years 0001 through 9999 are representable; an engine can impose a narrower range
and reports its own write error. No database, network dependency or calendar-clock read is needed
to construct a value.

## Why this changed

The SQL types already held six fractional digits. The old TypeScript adapters converted their
responses to JavaScript `Date`, which holds only milliseconds. On 12 September 2026 a live
PostgreSQL-to-ClickHouse backfill changed `09:30:15.123456` into `09:30:15.123000`, then `verify`
reported `matched: true`: both readers discarded the same digits before comparing. Matching two
lossy readings did not establish that the stored rows agreed.

PostgreSQL now retains timestamp text with parsers on the adapter's own client, before `pg` can
convert it to `Date`. The application's other `pg` clients keep their parsers. ClickHouse parses
the complete server text. Writes, point keys, keyset bounds, resume keys and verification retain
the exact value. Migration's temporary key index also handles native `bigint` identifiers instead
of passing them to an unsupported JSON serialization.

No IR, map, DDL or shared-vector bytes change. This repairs an adapter that lost information the
existing neutral type requires. It cannot reconstruct digits discarded by a previous copy;
rerun verification against the intact source before accepting an existing migration.

## Executable evidence

[`timestamps.live.test.ts`](../typescript/tests/timestamps.live.test.ts) launches a separate
[`Python SDK process`](../python/tests/timestamp_peer.py) to write and read the rows independently.
It covers both migration directions, interrupted backfills, composite timestamp/int64 keys,
bidirectional writes and a deliberate one-microsecond mismatch. The two timestamp keys fall within
one millisecond, and the int64 is above JavaScript's safe integer range. The ordinary engine slice
also round-trips microseconds across every mapped type.

These tests run in each Node CI job, with both engines and the Python peer installed. The main
TypeScript suite runs in a non-UTC process timezone, and the integration guard requires the slice
to pass without skips. Locally, after installing both libraries' development dependencies:

```sh
sudo make engines-up
export SDE_POSTGRES_DSN=postgresql://postgres:sde@127.0.0.1:55432/sde
export SDE_CLICKHOUSE_DSN=clickhouse://default:sde@127.0.0.1:58123/sde
TZ=America/New_York make check
```


## Python ClickHouse temporal query parameters

Python point reads, half-open ranges and migration keyset bounds bind datetime values as
microsecond-precision UTC strings, with an explicit `toDateTime64(..., 6, 'UTC')` conversion in the
comparison. Naive inputs keep the SDK's UTC interpretation; aware inputs retain their instant.
Values remain driver-bound parameters. This also avoids depending on the connection's timezone
when the driver's ordinary datetime formatter renders a value.

Before this correction, ClickHouse stored all six fractional digits but the Python driver's plain
datetime query formatting emitted seconds. A point get could return no row; an exclusive keyset
cursor could repeat its previous page; an inclusive upper bound could exclude its own row.
`test_clickhouse_datetime_queries.py` covers both timestamp kinds, offsets and pre-epoch values,
half-open ranges, composite keyset bounds, and resumed backfill/verification in both directions.
The six get/range/keyset regressions reproduced the defect before the fix. The map and stored-row
formats remain unchanged. These checks precede sustained traffic qualification.

The driver's [parameter documentation](https://clickhouse.com/docs/integrations/language-clients/python/driver-api#parameters-argument)
distinguishes ordinary datetime formatting from DateTime64 precision and timezone handling.
The SDK uses explicit UTC conversion over a bound string, so it does not require a new driver
wrapper API. A separate TypeScript writer and Python query peer also exercise adjacent microsecond
keys through the native engine in CI.

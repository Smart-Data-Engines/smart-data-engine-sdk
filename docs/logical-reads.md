# Logical reads and exact summaries

Python and TypeScript sessions expose bounded `scan`, exact `count` and numeric `summarize`
operations on PostgreSQL and ClickHouse. Applications name entities and fields. The SDK compiles
native queries, using the selected materialization from the loaded map; no request visits the
control plane.

```python
page = session.scan(
    "Reading", where={"station": "WAW"},
    bounds=sde.Range("at", low=start, high=end),
    order_by="at", descending=True, limit=100,
)
for row in page.rows:
    consume(row)
if page.next_after is not None:
    following = session.scan(
        "Reading", where={"station": "WAW"},
        bounds=sde.Range("at", low=start, high=end),
        order_by="at", descending=True, after=page.next_after, limit=100,
    )
```

```typescript
const options = {
  where: { station: 'WAW' },
  bounds: { field: 'at', low: start, high: end },
  orderBy: 'at', descending: true, limit: 100,
}
const page = await session.scan('Reading', options)
for (const row of page.rows) consume(row)
if (page.nextAfter !== null) {
  const following = await session.scan('Reading', { ...options, after: page.nextAfter })
}
```

These examples assume a declared Reading entity with a key, station and timestamp. Session setup
and ownership follow [session lifecycle](session-lifecycle.md); [timestamps](timestamps.md)
documents the native microsecond representations.

## Filters and bounded pages

Equality conditions in where are combined with AND. A null value means IS NULL. One Range may
add a lower inclusive and/or an upper exclusive bound on an ordered declared field. At least
one bound must be present. There is no raw SQL, cross-engine join, implicit OR or client-side
table filtering in this API.

The page limit defaults to 100 and must be an integer from 1 to the exported MAX_PAGE_ROWS = 1000.
The engine requests at most one additional row to detect whether another page exists. The returned
position is the key of the last **returned** row; the additional row belongs to the next page.

order_by / orderBy selects one field, followed by all missing primary-key fields in declared
order. The default is the whole key. The direction applies to every value, with NULL explicitly
last in either direction. Up to 32 fields may participate in this ordering. Integer, Boolean,
Decimal, UUID, text and date/time keys are supported, as are binary keys in PostgreSQL.
JSON and floating-point ordering are refused in this API's current scope. Finite float bounds
can be used in filters; a range excludes NaN as unordered.

Text uses UTF-8 byte order, with explicit PostgreSQL COLLATE "C". UUID uses the same canonical
order on both engines, including ClickHouse's explicit toString(UUID) expression.
Existing migration key_range / nth_key ordering and row-count checkpoints are unchanged.

after must contain exactly the complete ordering key. It is an explicit typed position in data,
not a security token or a persisted migration checkpoint. A new Session can use it. Applications
own its external serialization; timestamps, UUIDs, bytes and TypeScript bigint are typed SDK
values, not a promise of implicit JSON conversion.

These pages are not a snapshot across requests. An inserted key behind the position may require
restarting the scan; a key ahead can appear on a later page. A changed filter or direction is a new
question with an explicit position. Use appropriate engine isolation when a snapshot is required;
a default multi-statement transaction alone does not promise it.

## Exact counts and numeric summaries

```python
count = session.count("Reading", where={"station": "WAW"})
summary = session.summarize("Reading", "celsius", mean_scale=3)
# summary.count, non_null_count, minimum, maximum, total, mean
```

```typescript
const count = await session.count('Reading', { where: { station: 'WAW' } }) // bigint
const summary = await session.summarize('Reading', 'celsius', { meanScale: 3 })
// count, nonNullCount, minimum, maximum, total, mean
```

Both accept the same where, bounds and fresh options. Count returns an exact Python int or
TypeScript bigint, including values beyond JavaScript's safe-number range.

Summaries support int32/int64 and Decimal fields with precision up to 56. The native sum widens
to 76 decimal digits **before** accumulating, and results are transferred as text. This matters:
ordinary ClickHouse sum(Int64) can wrap; a measured sum of two INT64_MAX values returned -2 and
its native average returned -1. The SDK summary returns the exact total 18446744073709551614.

count includes every matching row; non_null_count / nonNullCount excludes NULL values.
With no non-null values, minimum, maximum, total and mean are null, regardless of the backend's
empty-aggregate defaults. Integer extrema retain their field representation; integer totals are
int/bigint. Decimal extrema and totals retain the declared field scale, as Python Decimal or
TypeScript decimal text.

Mean is computed from the exact sum and non-null count, with half-even rounding. Its scale defaults
to six decimal places and may be chosen from 0 through 38. Python's global Decimal context does
not change the result. Mean is Decimal in Python and decimal text in TypeScript. Floating
summaries, arbitrary GROUP BY and time bucketing are outside this API's current scope.

## Placement, lifecycle and errors

fresh=True / fresh: true reads the source. A read inside a write transaction also uses the
source. Otherwise the map's route remains authoritative. If a routed derived copy lacks a field
needed by a projection or filter, the SDK raises QueryRefused before I/O; it does not silently
reroute the operation. The whole-entity scan projects declared fields and derived foreign-key
fields, excluding internal generation metadata and unrelated physical columns.

Hashing applies to filter, order and position field names as well as returned rows. Session and
adapter ownership, closed-session and expired-scope refusals apply to all three operations.
QueryRefused is a local planning refusal; native execution failures retain EngineError.
Adapters implement the optional Queryable and Summarizable capabilities; unsupported
operations do not fall back to scans or loops.

Scan records the existing full_scan or range_read shape. Count and summarize record aggregate.
An aggregate returns one result row: its numeric result is not the telemetry row count.
Filters, cursor keys and aggregate result values never enter telemetry. The current
time_filtered_share metric remains explicitly unmeasured; these APIs do not invent that value.

The ClickHouse layout generator now preserves nullable non-key fields as Nullable(nativeType).
Previously it dropped that model property and a valid null value was refused by the generated
schema. Existing explicit maps keep their types, and existing tables are never silently altered;
a changed physical layout needs the ordinary preparation/migration workflow. Legacy auto
maps still derive PostgreSQL layouts with the same interpretation.

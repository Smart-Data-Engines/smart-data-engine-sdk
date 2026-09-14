# Logical batch writes

Use `Session.save_many(entity, rows)` in Python or `Session.saveMany(entity, rows)` in TypeScript
to insert one batch through a logical entity. The application does not name a physical table or
engine. PostgreSQL and ClickHouse support this API. An adapter for another engine must provide
`BulkWritable.insert_many` / `BulkWritable.insertMany`; the SDK refuses a missing capability
before writing to the source or any required copy.

```python
session.save_many("Reading", [
    {"id": 1, "celsius": 20},
    {"id": 2, "celsius": 21},
])
```

```typescript
await session.saveMany('Reading', [
  { id: 1n, celsius: 20 },
  { id: 2n, celsius: 21 },
])
```

These examples assume a declared `Reading` entity with an int64 key and an integer `celsius`
field. Session setup and connection ownership are described in [session lifecycle](session-lifecycle.md).

## Input contract

Both SDKs export `MAX_BATCH_ROWS = 1000` and `MAX_BATCH_VALUES = 60000`. A batch must satisfy both:

- At most 1000 rows, supplied as a Python sequence or a JavaScript array. Generators and streams
  are refused; applications choose their batch boundaries explicitly.
- `row count × (supplied fields + generation column, when present) ≤ 60000`. For example,
  60 application fields with a generation-bearing map allow 983 rows in one batch.
- Every row has the same nonempty set of fields. The fields belong to the declared entity,
  including its derived foreign-key fields such as `parent_id`. Include the complete key and
  every non-nullable declared field. Omitted nullable fields retain the backend's default behavior.

The value limit is below PostgreSQL's [65535 query-parameter limit](https://www.postgresql.org/docs/15/limits.html).
A local PostgreSQL 15 probe accepted 60000 and 65535 parameters and refused 65536. These are
operation bounds, not a promise about payload bytes, throughput or a backend's own limits.
The SDK never splits an oversized batch automatically.

`BulkWriteRefused` is a `ModelPlanningError`: local batch validation or a required capability
failed before any batch I/O. An empty batch checks the entity, session lifetime and transaction
group, then returns without I/O, capability requirements or telemetry. Engine failures retain
`EngineError`. The existing single-row `save` contract is unchanged.

The SDK snapshots the batch and mutable values before its first I/O. Changing the caller's
array, mappings, nested JSON, mutable dates or byte buffers afterward does not change a deferred
copy. Timestamp/Decimal/UUID and other supported scalar values retain their precision. Custom
objects, cyclic containers and undefined JavaScript values are refused. Values still undergo the
native driver's type adaptation and column checks; the field preflight is not a universal type
coercion layer. Python PostgreSQL binds dict/list documents as JSONB for this API without changing
any global driver adapters. ClickHouse's existing exclusion of neutral JSON and binary fields
remains in force.

## Native execution and failures

PostgreSQL receives one parameterized multi-row `INSERT`. A duplicate key fails the statement;
there is no `ON CONFLICT DO NOTHING`. The migration adapter's `copy_in` has different semantics
and is not the application's bulk API.

ClickHouse receives one native/HTTP batch and retains its native table semantics. The SDK does
not promise transactionality across blocks, parts or partitions, nor universal upsert semantics.
A failed response may leave a partial result. A lost response can follow an accepted write in
either backend. A successful return inside a transaction remains provisional until the outer
transaction commits.

There is no automatic source replay. On an uncertain result, inspect/reconcile the data using
application identities and an explicit recovery policy before deciding whether to retry. This
also applies to ClickHouse transport errors: the Python adapter disables urllib3 replay and
prevents clickhouse-connect's implicit retry after a remote close. A controlled test lost the
response after a real accepted INSERT; the unguarded driver sent it twice and returned success.
The guarded path reports one uncertain failure and leaves one physical row. The guard is local
to that adapter and preserves the existing pool's TLS/proxy configuration.

## Generations, transactions and copies

Each row carries the immutable generation of its Session. Opening another Session does not
upgrade an old one. Native [write fences](write-fences.md) refuse stale batches after cutover.
[Session ownership](session-lifecycle.md) and expired-scope refusals apply to the entire batch.

The source must succeed before fan-out begins. Within a PostgreSQL transaction, the SDK queues
one batch per maintained copy and sends it after the outer commit. Nested savepoints retain or
discard their own queued batches. A rollback sends none of its rows to a copy. Replay preserves
the batch boundary; it does not issue one remote operation per row.

A copy failure is recorded as divergence and does not undo an accepted source operation. The
[local cutover](local-cutover.md) recovery and verification gate must reconcile that divergence
before activating a different source. The SDK does not retry the source to repair a failed copy.

## Telemetry and evidence

Each nonempty executed batch records one existing `bulk_write` shape with empty shape fields,
elapsed time, submitted row count and source-operation errors. No values enter telemetry.
The row count on an error is the attempted count, not a claim about accepted rows. In a
transaction the observation precedes final commit, just as it does for `save`.

Fan-out `writes` and `failures` count attempts to deliver a **batch**, not individual rows.
The interval includes queue time until the copy attempt finishes. Existing map, model and shape
bytes have not changed. Shared `migration/122`–`132` vectors pin call order, transaction outcomes,
failures and exact value-free metric bytes. Native tests cover both engines and languages,
1000-row values with adjacent microseconds, generation changes, source/copy execution, PostgreSQL
statement count, conflicts and rollback, and the controlled lost-response regression.

The declared clickhouse-connect floor is exercised as well as the current driver. The floor
requires native UUID objects when writing the fence-drain identity; the adapter binds that exact
identity as UUID instead of relying on a newer driver's acceptance of hyphenated text.

Table identifiers are quoted by the SDK before they reach the native INSERT client. This also
keeps the driver's DESCRIBE and INSERT statements valid on the 0.7.0 floor for table names with
spaces, backticks or backslashes. Ordinary inserts and migration copies share this boundary.

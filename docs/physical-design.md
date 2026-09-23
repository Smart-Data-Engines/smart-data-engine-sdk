# Physical design in a placement map

From placement map contract 5 a layout can say how its tables are stored, not only what they are
called and which types their columns have: the physical order of an entity's key, a time partition,
and indexes of a named method. The control plane chooses these - a model proposes the design with
evidence from your measured workload, and deterministic code validates, scores and records it - and
this library renders and verifies what the map says. A hand-written map may carry the same keys; the
no-account mode is a supported mode.

Every element is a **closed vocabulary rendered by this library**. Nothing in a map is pasted into
DDL, so a map can choose between designs this library knows how to build and cannot ask for anything
else.

The rules are normative in [format-contract.md §7i](format-contract.md); this page is the
explanation, and the tests it cites are the evidence.

## The three keys

```jsonc
"layout": {
  "tables":  {"Reading": "reading"},
  "columns": {"Reading": {"at": "DateTime64(6, 'UTC')", "station": "String", "temperature": "Float64"}},
  "key_order":    {"Reading": ["at", "station"]},
  "partition_by": {"Reading": {"field": "at", "granularity": "month"}},
  "indexes": [
    {"entity": "Reading", "name": "reading_temperature", "columns": ["temperature"],
     "method": "minmax", "granularity": 4}
  ]
}
```

renders, for ClickHouse, as one statement:

```sql
CREATE TABLE IF NOT EXISTS `reading` (`at` DateTime64(6, 'UTC'), `station` String,
  `temperature` Float64, INDEX `reading_temperature` `temperature` TYPE minmax GRANULARITY 4)
  ENGINE = ReplacingMergeTree PARTITION BY toYYYYMM(`at`) ORDER BY (`at`, `station`)
```

### `key_order`

The entity's key columns in physical order. It is a **permutation of the key the model declares**
and nothing else: the logical key stays the row's identity for `get`, for backfill and for
verification, so reordering it keeps the same uniqueness in PostgreSQL and the same deduplication in
ClickHouse (measured in both) while choosing which prefix an index can seek or prune by. It becomes
`PRIMARY KEY (...)` in PostgreSQL and `ORDER BY (...)` in ClickHouse. Absent means the declared
order, which is exactly what every earlier map produced.

### `partition_by` (ClickHouse only)

Exactly `{"field": ..., "granularity": ...}`, granularity `day`, `month` or `year`, rendered as
`toDate`, `toYYYYMM` or `toYear` of the field. Two rules, and both are about your data:

- **The field must be in the key.** `ReplacingMergeTree` collapses rows of one key only inside one
  partition. Partitioned on a column outside the key, two writes of one key can land in two
  partitions and stay two rows for ever: measured on ClickHouse 24.8, `FINAL` hid the duplicate only
  because `do_not_merge_across_partitions_select_final` was 0, and after `OPTIMIZE ... FINAL` both
  rows remained. Both test suites repeat that measurement on every run against the server they use.
- **The field must be `date` or `timestamptz`.** A `timestamp` without a zone is stored as
  `DateTime64(6)`, whose partition follows the server's configured timezone; a server reconfigured
  later would put one key into two partitions - the same duplicate by another road. A session's
  `session_timezone` does not move it (measured); the server's zone is documented to.

There is no week, because ClickHouse's `toStartOfWeek` depends on a mode and a closed vocabulary has
no modes. PostgreSQL partitioning is refused by name: it needs every partition created before a row
can arrive, which is a lifecycle this product does not manage, and an unpartitioned table under a
map that says otherwise would be a silent drop.

### Index methods

| Dialect | Method | Parameters | Renders as |
|---|---|---|---|
| PostgreSQL | `btree` (or absent) | - | `CREATE INDEX ... ON t (cols)`, the bytes every earlier map produced |
| PostgreSQL | `brin` | - | `CREATE INDEX ... ON t USING brin (cols)` |
| ClickHouse | `minmax` | `granularity` 1-1024 | `INDEX name col TYPE minmax GRANULARITY g` inside `CREATE TABLE` |
| ClickHouse | `set` | `granularity` 1-1024, `max_rows` 1-65536 | `... TYPE set(N) GRANULARITY g` |
| ClickHouse | `bloom_filter` | `granularity` 1-1024 | `... TYPE bloom_filter GRANULARITY g` |

A ClickHouse data-skipping index summarises exactly one column. `set(0)` means unlimited there,
which is an unbounded memory commitment per entry, so zero is outside the range rather than a
default. ClickHouse indexes are part of `CREATE TABLE` because `CREATE TABLE IF NOT EXISTS` never
adds one to an existing table and a separate `ALTER` is a mutation over every part. Indexes render in
code point order of their name, whatever order the document gives.

A method belongs to one dialect, and a map carries no dialect - so a BRIN index in a layout for a
ClickHouse engine loads cleanly and is refused by the renderer, which is the first place that knows
the dialect. `schema/016`-`018` pin those refusals.

## Applying a design, and finding out what a table really is

`CREATE ... IF NOT EXISTS` keeps whatever table or index already has the name - including one with
another sort key, another partition, or a B-tree where the map says BRIN (all measured). So after
applying a layout the library reads the design back from the engine's own catalogue and compares:

| Engine | Read from | Compared |
|---|---|---|
| PostgreSQL | `pg_index`, `pg_attribute`, `pg_am` | primary-key column order; each declared index's method and columns, and that it has no predicate, expression or `INCLUDE` columns |
| ClickHouse | `system.tables`, `system.data_skipping_indices` | sort key, partition key, each declared index's type, column and granularity |

ClickHouse formats those expressions, so they are **parsed**, not compared with a string this
library predicts: 24.8 leaves `select` and `order` bare and quotes `null` and `ząb`, and a formatting
rule that changed between releases would turn a correct table into a refusal.

What happens with a difference depends on who asked:

- **`prepare_schema` / `prepareSchema` refuses it**, naming the table, the aspect and both values.
  That is where a person applying a map can act on it. A new layout belongs in fresh tables - the
  staging protocol creates them under new names - not in the old ones under a new declaration.
  PostgreSQL indexes are built only after the table's key is confirmed, so a refused provisioning
  does not first build an index on the old table: `CREATE INDEX` without `CONCURRENTLY` blocks that
  table's writes while it builds.
- **A running session only reports it**, in `Session.physical` / `session.physical` and, in Python,
  as the `sde.schema.physical_mismatch` log event. The rows are the same rows whatever the design, and
  a difference in performance is not an outage this library is allowed to cause. Missing columns and
  wrong types still refuse, as they always have.

On ClickHouse, reading `system.data_skipping_indices` needs an explicit grant that the documented
runtime role does not have. The library reads it only when a table declares an index, and a runtime
login without the grant sees declared indexes reported as *unverified* - it does not fail to start.
[Runtime roles](runtime-roles.md) has the optional grant.

## Compatibility

A contract-5 library reads contracts 1 to 4 with their meaning unchanged. The new keys in a document
declaring 4 or less are refused rather than ignored, because an earlier library would ignore them and
build a different table from the same document. The structural rules for `indexes` - an entity with a
table, a name, distinct existing columns, no unknown keys - apply to every contract; they refuse
documents that could never have been applied correctly, which is a tightening and needs no bump.

A staging packet may raise the prepared map's contract from 4 to 5, because the fresh copy is where
a physical design first appears, and may not lower it. `migration/133`-`137` pin the packet rules.

## What this does not do

- It does not change an existing table. A different design means a new table, created by staging and
  switched to by cutover; this library never alters or drops a physical object in place.
- It does not partition PostgreSQL, partition by week, partition on a non-key column or on a zoneless
  timestamp, or accept an expression anywhere.
- It does not verify indexes a map does not declare. An extra index added outside SDE is left alone.
- It does not decide anything. Which design a table should have is the control plane's decision, from
  your measured workload; this library renders and checks the decision, and refuses one it cannot
  apply.

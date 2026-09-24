# In-place index builds: evidence

What [in-place index builds](../../in-place-index.md) rest on, measured before the design and kept
with the scripts that measured it. Everything here ran on the SDK's own test engines in containers
on one laptop (i3-7100U, 2 cores / 4 threads, ~16 GB), PostgreSQL 15.19 and ClickHouse 24.8.14.39,
on 24 September 2026. The machine was shared with other work (load average 2.5-5.7), so the numbers
are indicative; the direction they show is not in doubt.

## Premise: the copy path pauses writes in proportion to the table

`premise/measure_premise.py` runs, for each engine and table size, the path a model's
"add an index" decision took before this change - a fresh copy with the index (staging protocol 2)
and the cutover that follows - and the in-place build the engines offer, with a writer inserting a
row every 5 ms throughout. Raw results: `premise/smoke.json` (10 000 rows), `premise/premise.json`
and `premise/premise-run.log` (100 000 and 300 000 rows).

| engine | rows | write pause through the copy | in-place build | longest gap between writes during the build (before it) |
|---|---|---|---|---|
| PostgreSQL | 10 000 | 1 361 ms | 28 ms | 12.4 ms (10.5) |
| PostgreSQL | 100 000 | 9 504 ms | 157 ms | 21.0 ms (11.0) |
| PostgreSQL | 300 000 | 22 523 ms | 355 ms | 16.2 ms (8.4) |
| ClickHouse | 10 000 | 3 109 ms | 45 ms | 29.5 ms (42.0) |
| ClickHouse | 100 000 | 5 664 ms | 59 ms | 18.7 ms (18.0) |
| ClickHouse | 300 000 | 24 685 ms | 125 ms | 44.8 ms (27.9) |

The pause through the copy is linear in the table - about 75 µs a row on PostgreSQL - because the
cutover copies and compares every row while the source's writes are frozen. At about 400 000 rows
it passes the cutover's 30 s budget, and the cutover rolls back: an "add an index" decision could
not be carried out for a table of that size at all. The in-place build moves no row; the longest
gap between two writes during it is of the order of the gaps without it. No write failed in any run.

The script's usage lines were rewritten when it moved into this directory; its code is the code
that produced these files. `premise/SHA256SUMS` covers every file here.

## Engine behaviours the rules are written against

`premise/probe_engines.py` -> `premise/probe_engines.out.json`:

- **PostgreSQL:** a failed `CREATE INDEX CONCURRENTLY` leaves its index in the catalogue with
  `indisvalid` and `indisready` false, and `CREATE INDEX CONCURRENTLY IF NOT EXISTS` over it
  succeeds with only the notice `relation "leftover" already exists, skipping` - leaving it not
  valid, and in this probe unique, a different shape from the one asked for.
- **ClickHouse:** `ADD INDEX` lists the index in `system.data_skipping_indices` at once;
  `MATERIALIZE INDEX` is a mutation recorded with the command `MATERIALIZE INDEX <name>`, the name
  unquoted; `ADD INDEX IF NOT EXISTS` with another type keeps the index that is there.

`premise/probe_merge_pool.py` -> `premise/probe_merge_pool.out.json`, both ClickHouse:

- `SYSTEM STOP MERGES` holds a `MATERIALIZE INDEX` mutation (not done after 3 s) while inserts go
  on, and it completes once merges start; `KILL MUTATION` removes a pending mutation from
  `system.mutations` altogether. The live tests hold builds this way.
- `ALTER TABLE ... DROP INDEX` is a mutation too. With the default settings the ALTER waits for it
  on the merge pool - no answer within 8 s while merges were stopped - and with `alter_sync = 0` it
  returns in 0.02 s, the index gone from the catalogue at once and the file removal left pending.
  Abandoning a build uses the latter, because a build that has to be abandoned is likely to be one
  whose merge pool is not getting to it.

The PostgreSQL half of the deterministic holding - a `CREATE INDEX CONCURRENTLY` waits in its last
phase, `waiting for old snapshots`, for any transaction with an older snapshot - is exercised by
every held PostgreSQL build in `python/tests/test_index_operator_live.py` rather than probed here.

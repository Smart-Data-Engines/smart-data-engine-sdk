# Removing an index from a live table: evidence

What [removing and replacing indexes in place](../../in-place-index.md#signed-packet-protocol-2-removing-and-replacing-indexes)
rests on, measured before the design and kept with the script that measured it. It ran on the SDK's
own test engines in containers on one laptop (i3-7100U, 2 cores / 4 threads, ~16 GB), PostgreSQL
15.19 and ClickHouse 24.8.14.39, on 24 September 2026. The machine was shared with other work, so
the timings are indicative.

`probe_drop.py` -> `probe_drop.out.json`, with a writer inserting a row every 5 ms throughout:

| engine | what | result |
|---|---|---|
| PostgreSQL | `DROP INDEX CONCURRENTLY` on 100 000 rows | 6.3 ms; the longest gap between two writes 7.9 ms (10.2 ms before) |
| PostgreSQL | the same drop beside a `REPEATABLE READ` transaction that has taken its snapshot but not touched the table | finished in 6.5 ms; the index is gone |
| PostgreSQL | the same drop beside a `REPEATABLE READ` transaction that has read the table and stays open | still running after 3 s, waiting on `Lock` / `virtualxid`; meanwhile the index is `valid = false`, `ready = true` - no query uses it, writes still maintain it; the longest write gap 14.4 ms |
| PostgreSQL | that drop terminated (`pg_terminate_backend`) | the dropping session ends with `AdminShutdown`; the index is left `valid = false`, `ready = true` |
| PostgreSQL | a second `DROP INDEX CONCURRENTLY` | 10.7 ms; the index is gone |
| ClickHouse | `ALTER TABLE ... DROP INDEX ... SETTINGS alter_sync = 0` on 100 000 rows | 13.2 ms; no longer listed in `system.data_skipping_indices`; a read the index served still answers; the longest write gap 12.1 ms (19.8 ms before) |

No write failed: 634 inserts on PostgreSQL, 174 on ClickHouse.

A concurrent drop waits for the transactions that hold a lock on the table, not for older
snapshots as a concurrent build does - PostgreSQL's `index_drop` waits for the table's lockers.
The first run of this probe, without the second row, called the third one "held by an old
snapshot"; its transaction had read the table, and the second row shows that the reading, not the
snapshot, is what holds the drop. The live tests hold removals with a transaction that has read the
table for that reason. All numbers here are from the run with the second row.

What the operator's rules take from it:

- A removal waits for no write and holds none, so it needs no barrier - but it cannot be undone.
  The operator therefore removes only after its decision and the publication of the next map, one
  resumable step per index; a process still on the map in force loses nothing but the index's help.
- An open transaction that has read the table holds a concurrent drop. The signed build budget
  bounds the wait; after it the drop may run on in the server until that transaction ends, or stop
  and leave the index invalid and maintained, and `resume` finishes the removal.
- A stopped drop leaves an index of the declared shape that is not valid, and a second drop removes
  it. Recovery therefore drops an index of the declared shape whether it is valid or not, with
  `IF EXISTS` for an earlier drop that finishes meanwhile, and treats an absent one - or another
  object under the name, which it leaves alone - as already removed.
- ClickHouse drops with `alter_sync = 0`, as abandonment already does: without it the ALTER waits
  for the merge pool ([in-place-index](../in-place-index/README.md), `probe_merge_pool.py`).

A process on a map that declares an index the table no longer holds reports it as a physical
finding - `absent` on PostgreSQL, `unverified` where a restricted login cannot read the index
catalogue - and keeps serving rows: `python/tests/test_physical_live.py`.

`SHA256SUMS` covers the script and its output.

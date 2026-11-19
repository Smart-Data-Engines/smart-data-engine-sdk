# A fence's epoch change and release, one change of each table

The [settle record](../cutover-settled/README.md) measured an advance's pause of 3.9 to 4.2 s with
a ClickHouse copy, 3.0 to 3.3 s of it raising the copy's generation: three of that step's four
`ALTER TABLE ... ADD CONSTRAINT` and `DROP CONSTRAINT` statements took 0.6 to 1.3 s each, statements
of milliseconds otherwise, and none of them waited for a lock. This record finds why, and measures
the change it led to: an epoch change, and a barrier's release, are now one statement each.

Measured on 19 November 2026 on a Lenovo ThinkPad E570 (Intel Core i3-7100U, 2 cores and 4
threads, 16 GB, an ADATA SX7000NP NVMe SSD), ClickHouse 24.8.14 in Docker on the same disk.

## Why an ALTER of the constraints is slow

**Each one writes the table's metadata and syncs it once, and the sync is most of its time.**
`query_log` gives every such `ALTER` `FileSync = 1`. In a repeat of the settle record's move of four
million rows from PostgreSQL into ClickHouse (`cause/settled-4m.json`), the 27 `ALTER`s of the
fences took 2 to 64 ms, and their sync 1.4 to 27.4 ms of that; the slower ones also waited for the
table's parts lock, up to 36 ms (`cause/settled-4m-alters.json`, from `cause/query_log_alters.py`).
That run did not reproduce the slow step - the copy's generation rose in 287 ms - so the disk's state
at the moment of the step decides it.

**What the sync costs depends on the disk.** `cause/probe_alter_fsync.py` times the advance's four
statements on a table of two million rows, and the same four changes as one statement, five times
each, with the disk quiet and then beside a process that writes 8 MiB at a time and syncs it
(`cause/fsync.log`):

| | four statements | one statement | the sync of one `ALTER` |
|---|---|---|---|
| quiet disk | 18.5-44.4 ms | 5.9-9.8 ms | 1.2-2.6 ms |
| beside the writer | 81.8-98.8 ms | 12.2-19.5 ms | 1.1-19.5 ms |

**Merges do not explain it.** `cause/probe_alter_merges.py` runs the statements while ClickHouse
merges the table's parts - after an insert of 40 parts, and during an `OPTIMIZE ... FINAL` started on
another connection - and they took 5 to 413 ms in all (`cause/merges-running.log`). A first version
of the probe met no merge at all (`cause/merges-none-running.log`): its eight parts were not merged
within the time it waited.

The settle record's 0.6 to 1.3 s per statement fits the same sync at a much busier moment: that
machine's load average was 11.2 in one of its runs.

## The change

An epoch's bounds, and a barrier's release, are each one call of the fence's backend,
`change_constraints` (`changeConstraints` in TypeScript), in all three libraries:

- PostgreSQL and ClickHouse send one `ALTER TABLE` of several clauses. PostgreSQL takes the table's
  lock once; ClickHouse writes and syncs its metadata once.
- Elasticsearch writes its pipeline once. With the last hold among those dropped, the shard-level
  write block is lifted first, as dropping a hold alone lifts it.
- The old constraints give way to the new at one instant. Only bounds older than the new epoch,
  named as the state read before showed them, are dropped, and the state read after refuses a
  newer epoch found there, with the table closed, as before.

The shared fencing vectors `migration/046`, `051`, `054` and `055` record the new call
(format contract §7c).

## Before and after

`measure_fence_change.py` drives the library's own `WriteFence` on a table of two million rows,
merged into one part before any round: ten rounds of freeze, `advance` and `release`, with the disk
quiet and then beside the same writer. It ran five times with the fences of `main` (`9ed3f8f`, from
a tree at `894d96d`, whose only difference is the release workflow) and five with this change
(`7c5b4e1`), one after the other, alternating (`runs/`):

| | before: medians of the five runs (worst round) | after |
|---|---|---|
| `advance`, quiet disk | 62.2-70.2 ms (80.8) | 32.6-43.3 ms (53.6) |
| `advance`, beside the writer | 137.4-153.2 ms (3,751.7) | 50.0-58.2 ms (330.2) |
| `release`, quiet disk | 36.5-39.1 ms (63.0) | 32.3-34.8 ms (42.8) |
| `release`, beside the writer | 68.2-75.7 ms (3,108.3) | 45.4-59.9 ms (460.3) |

Each `advance` and `release` also reads the fence's metadata from ClickHouse, which the change does
not touch: the [settle record](../cutover-settled/README.md) measures those reads under its next
steps.

`runs/first-version/` is the same comparison before the table was merged first: the faster side
reached its rounds while ClickHouse still merged the parts just inserted, and those merges' writes
beside the writer put second-long spikes into its first rounds. Its medians agree with the table
above.

## What this does not show

- The settle record's slow step itself: it was not reproduced on a quiet machine, so the effect is
  shown on the statements, beside a writer of our making, and not on that pause.
- PostgreSQL: one lock instead of four is not measured here; an autovacuum in the way of the
  barrier is waited for once instead of up to four times ([write fences](../../write-fences.md)).
- Another disk, file system, or ClickHouse version.

## Reproducing

```bash
export SDE_CLICKHOUSE_DSN=clickhouse://default:sde@127.0.0.1:58123/sde
<tree>/python/.venv/bin/python -I measure_fence_change.py --rows 2000000 --rounds 10 \
    --pressure <a file on the engine's disk> --out run.json
```

`cause/probe_alter_fsync.py <out.json> <pressure file> [rounds]` and
`cause/probe_alter_merges.py <out.json> [rounds]` take the same variable;
`cause/query_log_alters.py '<since, UTC>' <out.json>` reads the fences' `ALTER`s from `query_log`.
`SHA256SUMS` covers every file here but this one.

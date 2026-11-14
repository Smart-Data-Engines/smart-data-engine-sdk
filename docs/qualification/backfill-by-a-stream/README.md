# The backfill before a cutover, from a ClickHouse source

[`sde-operator backfill`](../../local-cutover.md#filling-the-copy-before-the-pause) fills a staged
copy before its cutover, while the application keeps writing. It read its source by a statement a
chunk: the next thousand rows after the last key copied. Under `FINAL`, ClickHouse answers such a
statement by merging the table's parts again from that key
([measured](../cutover-repair/README.md#the-three-changes-one-at-a-time)), so a source read whole
that way cost more than in proportion to its size. The backfill now reads a ClickHouse source by
one statement, from where the last run stopped. This record measures the difference.

One laptop, in use while it ran (ThinkPad E570, Intel i3-7100U, 2 cores / 4 threads, ~16 GB),
PostgreSQL 15.19 and ClickHouse 24.8.14.39 in containers on it, 14 November 2026. Rows of three
integer columns, 1000 rows a chunk. [`measure_backfill.py`](measure_backfill.py) seeds the source,
stages the copy and times `LocalCutover.backfill` to its end. Each size ran once on `main`
(`428a683`, a statement a chunk) and once on this change (`f43ada6`, one statement), one after the
other, so a drift of the machine falls on both; the load before each run is in its file.

| move | rows | a statement a chunk (s) | one statement (s) | | µs a row, before and after |
|---|---|---|---|---|---|
| ClickHouse → PostgreSQL | 200 000 | 8.9 | 4.3 | 2.1× | 44.7 → 21.5 |
| ClickHouse → PostgreSQL | 450 000 | 24.0 | 9.5 | 2.5× | 53.4 → 21.2 |
| ClickHouse → PostgreSQL | 900 000 | 63.3 | 20.0 | 3.2× | 70.3 → 22.2 |
| ClickHouse → PostgreSQL | 2 000 000 | 134.9 | 42.5 | 3.2× | 67.5 → 21.3 |
| ClickHouse → PostgreSQL | 4 000 000 | 248.5 | 60.9 | 4.1× | 62.1 → 15.2 |
| PostgreSQL → ClickHouse | 900 000 | 24.9 | 22.6 | | 27.6 → 25.1 |

Every run copied every row (`rows_in_the_copy` in each file) and ended complete. The last row is
the control: a PostgreSQL source is read by its key's index a chunk, as before, and the change
leaves it where it was, within what two runs on this machine differ by.

By one statement a row costs the same whatever the size of the table, 15 to 22 µs here, most of it
the copy into PostgreSQL and the marker written after each chunk. By chunks it cost 45 to 70 µs,
more as the table grew. How much more depends on how many parts the table is in: the
[earlier probe](../cutover-repair/README.md#the-three-changes-one-at-a-time) read a table of seven
parts in 22.2 s by chunks and 1.6 s by one statement, a wider gap than these freshly seeded tables
show.

## What stays as it was

- **A run that stops is continued by the next.** The marker is written after each chunk, as
  before; the next run finds the key at the marker and begins its statement strictly after it.
- **A row written while the statement runs is not in it.** It is the fan-out's, as a row above the
  marker always was, and a run that begins later reads it. The cutover's pause repairs what
  neither reached.
- **A relayout inside one engine reads by chunks**, since a stream holds its engine until it ends.
- **A cutover's own pass** read its ClickHouse source by one statement already; this is the same
  read, given a key to begin after.

## What this does not show

- A wide row, a key of several columns, a table in many parts, or any machine but this one.
- One run of each: a difference of two between the columns is the result, a difference of a
  tenth is not.

## Reproducing

```bash
sudo make engines-up && export SDE_POSTGRES_DSN=... SDE_CLICKHOUSE_DSN=...
cd python/tests
../.venv/bin/python ../../docs/qualification/backfill-by-a-stream/measure_backfill.py \
    --sources clickhouse --rows 200000 900000 4000000 --label stream --out stream.json
# the same script against another tree of the library: that tree's python/src first on PYTHONPATH
```

`run.log` is the order the runs were made in. `SHA256SUMS` lists every file here but this one.

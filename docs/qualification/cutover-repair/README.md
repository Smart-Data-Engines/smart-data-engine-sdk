# A cutover's pause when the copy is filled before it

[`../cutover-limits/`](../cutover-limits/README.md) measured how large a group a move fits into its
pause, and found the pause covers copying the group. A [staging](../../staging.md) creates the copy
and copies nothing; the [cutover](../../local-cutover.md) that followed emptied the copy, refilled
it from the frozen source and compared every value. This record measures the same move after three
changes, each on the SDK's own code paths:

1. **`sde-operator backfill`** fills the copy before the pause, while the application keeps writing.
2. **The pause repairs what differs.** It reads the frozen source and the copy side by side and
   writes over the rows that differ, then compares, under its final barriers, the rows it wrote and
   both counts ([why that is the whole comparison](../../local-cutover.md#filling-the-copy-before-the-pause)).
3. **A ClickHouse table is read in key order by one statement**, where it was read by one a chunk.

Everything here ran on one laptop (ThinkPad E570, Intel i3-7100U, 2 cores / 4 threads, ~16 GB),
against the SDK's test engines in containers on the same machine - PostgreSQL 15.19 and ClickHouse
24.8.14.39 - on 14 November 2026. The signed pause budget is the live tests' 30 000 ms. The rows
are those of the live tests' group: three integer columns (`id`, `value` and the write generation).
A wider row costs more, so for a real group these row counts are an upper bound.

**The laptop was in use while this ran**, with a load average of 2.4 to 8.9 before a run (it is in
the raw results). So the ways of one run were measured side by side - `refill`, then `backfill`, then
the next run - and a table gives the lowest and the highest run, never an average. Compare the
ways within a table; a number of one table against another of an hour earlier says less.

## The ways

[`measure_repair.py`](measure_repair.py) seeds N rows into the source, stages the copy with
`LocalCutover.stage`, and then:

| way | what it does | what its pause covers |
|---|---|---|
| `refill` | the cutover, with `repair = "refill"` | emptying the copy, copying the group, comparing every value: the cutover as it was |
| `backfill` | `LocalCutover.backfill`, then the cutover | comparing the two, writing what differs, comparing what was written |
| `backfill-lost` | the same, with 1 row in 1000 written to the source alone after the backfill | the same, with those rows to write |
| `backfill-refill` | a backfill, then the cutover with `repair = "refill"` | the control: a backfill without the repair of what differs changes nothing in the pause |

The pause is the receipt's `elapsed_ms`. `repair` and `comparison` are the two phases that grow
with the group, timed between the operator's own step markers; the rest of the pause is the access
and generation changes, about half a second whatever the group.

## 200 000 rows

The branch of this change at `1f8f87e`, the `commit` in each file. What was merged differs from it in
comments, tests and documents, and in one check that sends nothing: the repair's pass holds the
rows it read to the source's count, which it was taking already. Three runs of each way, 1000 rows
a chunk
([`final/200k-chunk-1000.json`](final/200k-chunk-1000.json)):

| move | way | pause, each run (s) | repair (s) | comparison (s) | backfill before (s) | rows written |
|---|---|---|---|---|---|---|
| ClickHouse → PostgreSQL | `refill` | 11.5, 11.9, 11.5 | 6.0-6.1 | 5.1-5.5 | - | all of them |
| ClickHouse → PostgreSQL | `backfill` | 2.6, 2.2, 2.4 | 1.5-1.7 | 0.2-0.3 | 6.0-6.7 | 0 |
| ClickHouse → PostgreSQL | `backfill-lost` | 2.1, 2.4, 2.3 | 1.5-1.7 | 0.2-0.3 | 6.0-9.6 | 200 |
| ClickHouse → PostgreSQL | `backfill-refill` | 11.7, 11.3, 14.0 | 5.6-6.8 | 5.2-6.8 | 5.6-6.2 | all of them |
| PostgreSQL → ClickHouse | `refill` | 10.6, 8.1, 8.3 | 3.5-4.6 | 4.2-5.4 | - | all of them |
| PostgreSQL → ClickHouse | `backfill` | 3.5, 3.5, 2.5 | 1.6-2.3 | 0.3-0.5 | 4.8-6.7 | 0 |
| PostgreSQL → ClickHouse | `backfill-lost` | 2.4, 3.8, 2.7 | 1.6-2.4 | 0.3-0.4 | 4.6-6.1 | 200 |
| PostgreSQL → ClickHouse | `backfill-refill` | 9.1, 8.6, 8.4 | 3.4-3.9 | 4.0-4.9 | 4.7-6.4 | all of them |

At 5000 rows a chunk ([`final/200k-chunk-5000.json`](final/200k-chunk-5000.json)):

| move | way | pause, each run (s) | repair (s) | comparison (s) | backfill before (s) | rows written |
|---|---|---|---|---|---|---|
| ClickHouse → PostgreSQL | `refill` | 7.0, 7.9, 5.6 | 2.5-3.7 | 2.5-4.1 | - | all of them |
| ClickHouse → PostgreSQL | `backfill` | 3.3, 1.8, 2.4 | 1.3-2.3 | 0.2-0.3 | 3.1-4.0 | 0 |
| PostgreSQL → ClickHouse | `refill` | 4.3, 4.5, 5.1 | 1.4-2.1 | 2.3-2.6 | - | all of them |
| PostgreSQL → ClickHouse | `backfill` | 1.9, 2.1, 2.2 | 1.2-1.4 | 0.2 | 1.6-2.0 | 0 |

A move from ClickHouse into PostgreSQL paused 18 to 23% of what it paused with a refill, and a move out
of PostgreSQL 30 to 43%, in the runs side by side. The control behaves as a refill: the backfill by
itself changes nothing, since a refill empties whatever the copy holds.

## How large a group fits 30 s

One run of each size, the copy filled before the pause, 1000 rows a chunk
([`final/limits-ch-pg.json`](final/limits-ch-pg.json),
[`final/limits-pg-ch.json`](final/limits-pg-ch.json),
[`final/limits-larger.json`](final/limits-larger.json)):

| move | rows | pause (s) | repair (s) | comparison (s) | backfill before (s) | outcome |
|---|---|---|---|---|---|---|
| ClickHouse → PostgreSQL | 450 000 | 3.7 | 3.1 | 0.2 | 16 | success (matched) |
| ClickHouse → PostgreSQL | 900 000 | 7.1 | 6.3 | 0.4 | 33 | success (matched) |
| ClickHouse → PostgreSQL | 1 500 000 | 18.0 | 17.0 | 0.4 | 90 | success (matched) |
| ClickHouse → PostgreSQL | 2 000 000 | 18.8 | 17.8 | 0.5 | 115 | success (matched) |
| ClickHouse → PostgreSQL | 3 000 000 | 24.3 | 23.2 | 0.7 | 142 | success (matched) |
| ClickHouse → PostgreSQL | 4 000 000 | stopped at 30.2 | | | 230 | aborted by `resume` in 0.67 s (`recovered_before_decision`) |
| PostgreSQL → ClickHouse | 900 000 | 8.0 | 6.6 | 0.6 | 24 | success (matched) |
| PostgreSQL → ClickHouse | 1 500 000 | 13.6 | 11.2 | 0.9 | 37 | success (matched) |
| PostgreSQL → ClickHouse | 2 000 000 | 21.3 | 17.6 | 1.1 | 52 | success (matched) |
| PostgreSQL → ClickHouse | 3 000 000 | 29.4 | 26.6 | 1.2 | 86 | success (matched) |
| PostgreSQL → ClickHouse | 4 000 000 | stopped at 30.2 | | | 105 | aborted by `resume` in 0.71 s (`recovered_before_decision`) |

With a refill, the earlier record fitted about 450 000 rows from ClickHouse into PostgreSQL and
about 900 000 the other way, the second at the edge. A group that does not fit is stopped by the
operator's watchdog at the budget and aborted by the recovery, with the source as it was: the last
rows of the table are that, measured.

The backfill before the pause is not part of the pause and takes longer than the pause it saves:
it copies the group, by chunks, while writes continue.

## The three changes, one at a time

The first measurement of the day had only the first two changes
([`steps/1-repair-what-differs/`](steps/1-repair-what-differs/)); the second also read a ClickHouse
source by one statement and computed a row's key once ([`steps/2-source-by-a-stream/`](steps/2-source-by-a-stream/),
stopped after its first block); the last reads a ClickHouse copy that way too (`final/`). 200 000
rows, 1000 a chunk, the pause of each run in seconds:

| move | way | repair of what differs, read by chunks | the source by one statement | the copy too |
|---|---|---|---|---|
| ClickHouse → PostgreSQL | `refill` | 10.7, 10.0, 11.1 | 10.8, 10.7, 10.2 | 11.5, 11.9, 11.5 |
| ClickHouse → PostgreSQL | `backfill` | 5.8, 5.8, 5.9 | 2.2, 1.9, 1.9 | 2.6, 2.2, 2.4 |
| PostgreSQL → ClickHouse | `refill` | 6.8, 8.3, 7.8 | 8.1, 7.2, 7.3 | 10.6, 8.1, 8.3 |
| PostgreSQL → ClickHouse | `backfill` | 5.9, 5.5, 5.8 | 4.3, 4.0, 3.9 | 3.5, 3.5, 2.5 |

The repair of what differs alone took the pause of a move into PostgreSQL to a little over half.
What was left was reading. A profile of the pass put 3.1 of its 5.3 s in waiting for ClickHouse to
answer 200 statements, and about one more in this library computing each row's key five times.

**Why a chunk a statement was slow** ([`probe_key_reads.py`](probe_key_reads.py),
[`probe_key_reads.json`](probe_key_reads.json)): under `FINAL` a statement that starts at a key and
takes the next 1000 rows merges the table's parts again from that key to find them. On a table of
900 000 rows it cost from 8 ms near the table's end to 31 ms at its start, against 7 ms for a range
bounded at both ends: the more rows after the starting key, the longer. So a table read whole by
chunks costs more than in proportion to its size - this one took 22.2 s by chunks and 1.6 s by
one statement - which is also why the first measurement of the day did not fit 900 000 rows into
30 s. The adapter's `key_stream` is that one statement; `key_range` is unchanged, and a backfill
still reads by chunks, since it has to be able to stop and continue.

A copy in ClickHouse was read by a bounded range a chunk, which is the cheaper statement, and still
200 of them a pass. Read by one statement beside the source, forward, it is the last column. A row
the stream has not shown is looked up by its key before it is called missing, so the stream does
not have to arrive in the order this process compares keys by.

## An answer that ends before the table does

One statement over a whole table is also one answer that a server may cut. ClickHouse does, and
raises nothing, for a login whose settings cap a result and say to break rather than throw
(`max_result_rows` with `result_overflow_mode = 'break'`; the mode is `throw` unless someone set
it). [`probe_cut_answer.py`](probe_cut_answer.py) gives the adapter's connection those settings, as
a settings profile gives them to a login, and reads a table of 200 000 rows three ways
([`probe_cut_answer.json`](probe_cut_answer.json)):

| the login's cap on a result | `key_stream` read | `key_range`, 1000 rows a chunk, read | `count` said |
|---|---|---|---|
| none | 200 000 | 200 000, in 201 statements | 200 000 |
| 50 000 rows, `break` | 106 497, and no error | 200 000, in 201 statements | 200 000 |
| 100 rows, `break` | 49 153, and no error | 200 000, in 205 statements | 200 000 |

A walk by chunks continues from the last key it was given, so a chunk cut short costs it a
statement and no row. The stream has nothing to continue from: its end is the table's end or the
cap, and it cannot tell which. So the repair's pass holds the rows it read of a source to that
source's count. Where the two differ it does not go on to compare by the rows it wrote: the copy is
refilled by chunks and compared whole, as before this change, and the completed plan's `repair`
record says `"partial": true`.

That is a cap on a result. The same mode on what a statement may read, or on how long it may run
(`read_overflow_mode`, `timeout_overflow_mode`), shortens every answer of the login, a count
included. Since the change that followed this one, every statement the libraries send says that a
limit is an error, so none of these answers comes back short
([whole-answers](../whole-answers/README.md)); the pass's own check stays.

## What this does not show

- A wide row, a key of several columns, or a key whose order two engines disagree on. The last is
  correct and slower: every row of a chunk is then looked up by key, one statement a chunk.
- A group with more than a few rows to repair. `backfill-lost` wrote one row in a thousand; a copy
  that is less than half full is refilled, as before.
- Elasticsearch, whose copy is still refilled.
- Any machine but this one, and this one was in use.

## Reproducing

```bash
sudo make engines-up && export SDE_POSTGRES_DSN=... SDE_CLICKHOUSE_DSN=...
cd python/tests
../.venv/bin/python ../../docs/qualification/cutover-repair/measure_repair.py \
    --sources clickhouse postgres --rows 200000 --repeat 3 --out repair.json
../.venv/bin/python ../../docs/qualification/cutover-repair/measure_repair.py \
    --sources clickhouse --rows 450000 900000 2000000 --ways backfill --out limits.json
python3 ../../docs/qualification/cutover-repair/summarise.py   # the tables, from the files beside it
```

`SHA256SUMS` lists every file here but this one.

# A cutover's pause after a settle

[`../cutover-repair/`](../cutover-repair/README.md) measured a cutover's pause after the copy was
filled before it: under the pause the operator compares both sides of the whole group and writes
what differs, so the pause still grows with the group - about 7 s a million rows on the machine
below. A [generation advance](../../generation-advance.md) marks a point the engines enforce: from
it on every write of the group carries the map's generation, G, so a row below G has not been
written since. [`sde-operator settle`](../../local-cutover.md#settling-before-the-pause) compares
those rows before the pause, while the application keeps writing, and the cutover's pause then
reads by generation only the rows that carry G. This record measures that move, on the SDK's own
code paths, beside the move after a backfill alone.

Everything here ran on one laptop (ThinkPad E570, Intel i3-7100U, 2 cores / 4 threads, ~16 GB),
against the SDK's test engines in containers on the same machine - PostgreSQL 15.19 and ClickHouse
24.8.14.39 - on 19 November 2026. The signed pause budget is the live tests' 30 000 ms, the rows are
those of the live tests' group: three integer columns (`id`, `value` and the write generation), and
the operator reads 1000 rows a chunk. A wider row costs more, so for a real group these row counts
are an upper bound.

**The laptop was in use while this ran** - other sessions, among them one running the engine's
tests, an editor, a browser - with a load average from 1.4 to 12.8 before a run (it is in the raw
results). So the ways of one run were measured side by side, and a table gives the lowest and the
highest run, never an average. Compare the ways within a table.

## The ways

[`measure_settled.py`](measure_settled.py) seeds N rows into the source, stages the copy with
`LocalCutover.stage` and fills it with `LocalCutover.backfill`, and then:

| way | before the cutover | what its pause reads |
|---|---|---|
| `backfill` | nothing more | both sides, whole: the pause of `../cutover-repair/` |
| `settled` | `LocalCutover.advance`, `LocalCutover.settle` | the rows that carry G, on both sides |
| `settled-written` | the same, then 1 row in 1000 of the group written under G: to the source and the copy, as a session and its fan-out write them, and one in ten of them to the source alone | the same, with those rows to compare and the tenth to write |

After the reads, every way compares under the final barriers the copy's rows the pause wrote, and
both counts. The pause is the receipt's `elapsed_ms`; the phases are the time between the operator's
own step markers, and each result keeps every marker (`steps`, in milliseconds since the command
was called). The advance's own pause, with its phases, and the settle's time are kept too: they are
what the shorter pause costs before it.

## The pause, by the size of the group

The branch of this change at `8f0c78b`, the `commit` of the file. The runs of the later sections
are at `4e72179`, which differs from it in tests and in this record only. Two runs of each way and
size ([`final/settled.json`](final/settled.json)), the pause in seconds:

| move | rows | `backfill` | `settled` | `settled-written` |
|---|---|---|---|---|
| PostgreSQL → ClickHouse | 200 000 | 2.07-2.10 | 0.80-0.91 | 0.84-0.86 |
| PostgreSQL → ClickHouse | 1 000 000 | 8.04-8.63 | 1.41-1.50 | 1.67-1.87 |
| PostgreSQL → ClickHouse | 2 000 000 | 15.25-15.26 | 2.25-2.43 | 2.31-2.62 |
| ClickHouse → PostgreSQL | 200 000 | 1.81-2.23 | 0.56-0.57 | 0.59-0.62 |
| ClickHouse → PostgreSQL | 1 000 000 | 6.97-7.19 | 0.87-0.93 | 0.78-1.73 |
| ClickHouse → PostgreSQL | 2 000 000 | 13.97-14.13 | 1.03 | 1.24-1.37 |

By the operator's steps, in milliseconds (the comparison is the one under the final barriers, and
"written since" the rows `settled-written` added after the settle; the pause wrote the tenth whose
fan-out it left out):

| move | rows | way | runs | pause, s | repair, ms | comparison, ms | written since the settle | rows the pause wrote | advance's pause, ms | settle, s | backfill before, s | outcome |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| PostgreSQL → ClickHouse | 200 000 | `backfill` | 2 | 2.07-2.10 | 1305-1314 | 221-246 | 0 | 0 | - | - | 4.1-4.3 | success (matched) |
| PostgreSQL → ClickHouse | 200 000 | `settled` | 2 | 0.80-0.91 | 44-52 | 228-236 | 0 | 0 | 403-415 | 1.2-1.3 | 4.2-4.4 | success (matched) |
| PostgreSQL → ClickHouse | 200 000 | `settled-written` | 2 | 0.84-0.86 | 76-79 | 234-265 | 200 | 20 | 400-497 | 1.3 | 4.4 | success (matched) |
| PostgreSQL → ClickHouse | 1 000 000 | `backfill` | 2 | 8.04-8.63 | 6591-7290 | 540-609 | 0 | 0 | - | - | 21.5-25.8 | success (matched) |
| PostgreSQL → ClickHouse | 1 000 000 | `settled` | 2 | 1.41-1.50 | 113-170 | 527-564 | 0 | 0 | 842-5806 | 6.4-6.9 | 22.3-23.0 | success (matched) |
| PostgreSQL → ClickHouse | 1 000 000 | `settled-written` | 2 | 1.67-1.87 | 302-354 | 594-599 | 1000 | 100 | 670-766 | 6.4-6.6 | 21.8-22.0 | success (matched) |
| PostgreSQL → ClickHouse | 2 000 000 | `backfill` | 2 | 15.25-15.26 | 13244-13438 | 841-857 | 0 | 0 | - | - | 44.4-45.7 | success (matched) |
| PostgreSQL → ClickHouse | 2 000 000 | `settled` | 2 | 2.25-2.43 | 262-298 | 880-991 | 0 | 0 | 970-978 | 13.5-13.7 | 44.3-44.7 | success (matched) |
| PostgreSQL → ClickHouse | 2 000 000 | `settled-written` | 2 | 2.31-2.62 | 327-592 | 901-1116 | 2000 | 200 | 968-1909 | 13.1-13.3 | 44.8-46.2 | success (matched) |
| ClickHouse → PostgreSQL | 200 000 | `backfill` | 2 | 1.81-2.23 | 1258-1694 | 174-175 | 0 | 0 | - | - | 2.8-3.3 | success (matched) |
| ClickHouse → PostgreSQL | 200 000 | `settled` | 2 | 0.56-0.57 | 34 | 173-182 | 0 | 0 | 366-383 | 1.3-1.5 | 2.7-2.8 | success (matched) |
| ClickHouse → PostgreSQL | 200 000 | `settled-written` | 2 | 0.59-0.62 | 57-75 | 178-180 | 200 | 20 | 321-371 | 1.3-1.4 | 2.7-2.8 | success (matched) |
| ClickHouse → PostgreSQL | 1 000 000 | `backfill` | 2 | 6.97-7.19 | 6341-6419 | 264-376 | 0 | 0 | - | - | 14.1-14.2 | success (matched) |
| ClickHouse → PostgreSQL | 1 000 000 | `settled` | 2 | 0.87-0.93 | 156-185 | 334-354 | 0 | 0 | 362-380 | 6.5 | 14.4-15.8 | success (matched) |
| ClickHouse → PostgreSQL | 1 000 000 | `settled-written` | 2 | 0.78-1.73 | 154-407 | 267-687 | 1000 | 100 | 342-364 | 6.2-6.9 | 13.6-13.7 | success (matched) |
| ClickHouse → PostgreSQL | 2 000 000 | `backfill` | 2 | 13.97-14.13 | 12792-13090 | 493-923 | 0 | 0 | - | - | 27.7-30.2 | success (matched) |
| ClickHouse → PostgreSQL | 2 000 000 | `settled` | 2 | 1.03 | 190-249 | 393-459 | 0 | 0 | 359-681 | 13.2-13.3 | 27.8-30.4 | success (matched) |
| ClickHouse → PostgreSQL | 2 000 000 | `settled-written` | 2 | 1.24-1.37 | 333-451 | 474-540 | 2000 | 200 | 348-358 | 12.2-12.7 | 28.2-29.9 | success (matched) |

At two million rows the pause after a settle is 15 to 17% of the pause after a backfill alone from
PostgreSQL into ClickHouse, and 7 to 10% the other way. Every run ended `success (matched)`: the
copy was the source's equal under the final barriers, row by row and in count.

## The rows written since the settle

What the pause reads grows with the rows written between the advance and the freeze. At two
million rows, one run each with 1, 10 and 50 rows in 1000 of the group written after the settle
([`final/settled.json`](final/settled.json), [`final/since-10.json`](final/since-10.json),
[`final/since-50.json`](final/since-50.json)):

| move | written since the settle | pause, s | repair, s | rows the pause read by generation | rows it wrote |
|---|---|---|---|---|---|
| PostgreSQL → ClickHouse | 2 000 | 2.31-2.62 | 0.33-0.59 | 3 800 | 200 |
| PostgreSQL → ClickHouse | 20 000 | 3.90 | 1.69 | 38 000 | 2 000 |
| PostgreSQL → ClickHouse | 100 000 | 9.40 | 7.18 | 190 000 | 10 000 |
| ClickHouse → PostgreSQL | 2 000 | 1.24-1.37 | 0.33-0.45 | 3 800 | 200 |
| ClickHouse → PostgreSQL | 20 000 | 4.19 | 2.26 | 38 000 | 2 000 |
| ClickHouse → PostgreSQL | 100 000 | 7.79 | 6.55 | 190 000 | 10 000 |

A row written since is read twice - by the walk over the source's rows that carry G and by the walk
over the copy's - so the pause read 1.9 rows for each (the tenth the copy lacked is read once), in
pages of 1000: 35 to 60 ms a page here, so about 70 µs of pause for each row written since. That is
the term that depends on the application rather than on the group: a settle of two million rows
took 13 s, so 100 000 rows since is an application writing 7 000 rows a second for the whole of
it. Here those rows were new keys, at the end of the key order. A row written again in the middle
of it carries G as well and is read by the same statements; what they cost there was not
measured.

## A group of four million rows

`../cutover-repair/` found that after a backfill alone 3 million rows fit the 30 s pause in both
directions, and 4 million did not: the watchdog stopped both moves at 30.2 s and the recovery
aborted them. After an advance and a settle, with 1 row in 1000 written since, one run of each
direction ([`final/settled-4m.json`](final/settled-4m.json)):

| move | rows | pause, s | repair, ms | comparison, ms | advance's pause, s | settle, s | backfill before, s | outcome |
|---|---|---|---|---|---|---|---|---|
| PostgreSQL → ClickHouse | 4 000 000 | 3.99 | 1247 | 1468 | 3.91 | 26.5 | 88.6 | success (matched) |
| ClickHouse → PostgreSQL | 4 000 000 | 1.78 | 587 | 755 | 0.40 | 24.5 | 55.1 | success (matched) |

This record does not say how large a group fits 30 s after a settle: nothing larger was measured,
and the parts that grow - below - do not grow in one proportion.

## What still grows with the group

Requirement 3 of the specification asks for a pause that depends on the rows written between the
advance and the freeze and not on the size of the group, measured on two million rows. It is met
in what the pause reads by generation - only the rows written since, none or 3 800 of two million
here - and not in what the pause costs. From 200 000 to 2 000 000 rows the pause after a settle
grew by 1.3 to 1.6 s from PostgreSQL into ClickHouse and by about 0.5 s the other way, against 13
and 12 s after a backfill alone. By the operator's steps, way `settled`, in milliseconds:

| step | PostgreSQL → ClickHouse, 200 000 | 2 000 000 | ClickHouse → PostgreSQL, 200 000 | 2 000 000 |
|---|---|---|---|---|
| `verify_frozen`: the rows the pause wrote, and both counts | 228-236 | 880-991 | 173-182 | 393-459 |
| `repair`: the rows that carry G, on both sides | 44-52 | 262-298 | 34 | 190-249 |
| `maintenance_epoch`: the copy's generation raised | 75-95 | 351-370 | 25-30 | 26-27 |
| every other step | 449-534 | 757-774 | 326-334 | 358-362 |
| the pause | 804-909 | 2250-2433 | 563-575 | 1031-1033 |

[`probe_pause_statements.py`](probe_pause_statements.py) times every statement one cutover sends,
by its shape, in a move of two million rows with 1 in 1000 written since
([`final/statements-2m.json`](final/statements-2m.json), one run of each direction and variant).
In milliseconds, in the variant without an index:

| statement | PostgreSQL → ClickHouse | ClickHouse → PostgreSQL |
|---|---|---|
| ClickHouse barriers reading the table's metadata (`system.tables`, `system.columns`, `system.databases`) | 1016, in 111 statements | 525, in 84 |
| the first page of a read by generation, ClickHouse (`... FINAL WHERE generation = G ORDER BY key LIMIT 1000`) | 411, in 2 | 122, in 1 |
| the first page of a read by generation, PostgreSQL | 148, in 1 | 361, in 2 |
| the later pages, and the answers with nothing that end a walk, both engines | 79, in 5 | 17, in 5 |
| PostgreSQL `count(*)` | 221 | 201 |
| ClickHouse `count() FINAL` | 72 | 14 |
| reads by key on the other side (`rows_for_keys`) | 93 | 110 |
| every other statement: grants, barriers, markers | 165, in 104 | 222, in 150 |
| the pause | 2450 | 1593 |

So what grows is reading: a table scan for the first page of each read by generation - the later
pages start from a key, and the rows since were the last keys here - and for each count. The
largest single part with a ClickHouse table is the barriers' reads of its metadata, which read no
row of the table: three statements each time the operator looks at a table's barrier, about 5 ms
each in a move of 20 000 rows and 9 to 10 ms in one of two million. Why they take longer beside a
larger table was not measured.

## An index by the generation: not in this change

The specification left one decision to this measurement: a partial index on the generation in
PostgreSQL, created after the settle and dropped after the cutover, so that a read by generation
finds its rows without a scan. `probe_pause_statements.py` measures it as its variant `indexed` -
`(key) WHERE __sde_write_epoch >= G` on the group's PostgreSQL table, which answers every read by
generation the pause makes - at two million rows, one run of each
([`final/statements-2m.json`](final/statements-2m.json),
[`final/statements-2m-since-50.json`](final/statements-2m-since-50.json)):

| move | written since the settle | PostgreSQL's reads by generation, ms, without → with | pause, s, without → with | the index created in, s |
|---|---|---|---|---|
| PostgreSQL → ClickHouse | 2 000 | 150 → 7 | 2.45 → 3.01 | 0.6 |
| ClickHouse → PostgreSQL | 2 000 | 364 → 7 | 1.59 → 1.05 | 0.5 |
| PostgreSQL → ClickHouse | 100 000 | 350 → 209 | 10.32 → 12.22 | 0.4 |
| ClickHouse → PostgreSQL | 100 000 | 942 → 189 | 10.83 → 8.40 | 0.4 |

The index does what it is for: PostgreSQL's first page of a read by generation goes from a scan of
the table to a few milliseconds. The pause moved by less than two runs of one variant differ by,
both ways, because the rest of it is elsewhere, where an index in PostgreSQL does not reach: with
few rows since, ClickHouse's barrier metadata, the first pages of ClickHouse's reads by generation
and the counts; with many, the reads by key on the other side - in the moves with 100 000 rows
since, ClickHouse's `FINAL WHERE key IN (...)`, 1000 keys a statement and 14 to 19 ms each, took
2.8 to 4.2 s of the pause. Against that, the index is a statement on the client's table under the
operator's login before the cutover and another after it, its upkeep on every write of the group
in between, and one more thing a crash can leave behind for the operator to remove.

So it is not added now. By what the pause measurably spends, the next steps are, in order:
1. **ClickHouse barriers read a table's metadata in three statements each time they look at it**,
   84 to 111 statements a cutover: 0.5 to 1.2 s of every pause with a ClickHouse table here, and an
   advance's barriers read it the same way. One statement a look, and no second look where nothing
   has changed.

   *Measured after this record:* one statement a look saves little. On an idle server the three
   statements of a look took 3.3, 4.3 and 4.1 ms (the round trip alone, `SELECT 1`, 2.9), the same
   three as scalar subqueries of one statement 11.2 ms, and one statement joining the three system
   tables 8.0 ms: ClickHouse runs each subquery as a query of its own, so the cost follows the
   system tables read, not the round trips. In three moves of 200 000 rows from PostgreSQL into
   ClickHouse, one statement a look spent 390 to 413 ms of the pause on the barriers' looks,
   against 442 to 674 ms with three. What is left to gain is in the number of looks - 24 in such a
   move - each barrier step looking again after its change.
2. **The reads by key on the other side**, with many rows since: two walks each look up the other
   side's rows by key, ClickHouse's under `FINAL`. Both walks read the rows that carry G on both
   sides; reading the two side by side in key order, as a cutover without a settle reads its two
   sides, would look up only the keys one side lacks.
3. The index above: 0.15 to 0.94 s of PostgreSQL's reads at two million rows.

## A PostgreSQL barrier waits for an autovacuum

In both ClickHouse → PostgreSQL moves with 100 000 rows since, one `ALTER TABLE ... ADD CONSTRAINT
... NOT VALID` on the PostgreSQL copy took 1.0 and 1.1 s, a step of a few milliseconds otherwise.
The server's log says why, at the same moments: `canceling autovacuum task` ... `automatic vacuum
of table "...sde_m_..._000001"`, the copy that had just taken 2 million rows and 90 000 more. A
barrier's `ALTER TABLE` needs a lock an autovacuum holds, and PostgreSQL cancels an autovacuum that
blocks another session only after `deadlock_timeout`, 1 s by default: that second was the pause's.
A busy table is vacuumed often, so a barrier on one can wait that long at any of its steps. Not
handled here; a lower `deadlock_timeout` for the operator's login is a setting only a superuser
can make, so it would be the client's administrator's to make.

## What the shorter pause costs before it

Two steps come before the cutover that a move after a backfill alone does not take (the same
tables):
- **The advance's own pause.** The group's writes wait from its first barrier to its last release,
  and no row is read. From ClickHouse into PostgreSQL it was 0.32 to 0.68 s at every size, four
  million rows included. With a ClickHouse copy it grew: 0.40 to 0.50 s at 200 000 rows, 0.67 to
  0.84 s at a million, 0.97 to 1.91 s at two and 3.91 s at four, most of it in the steps on the
  copy. In the run at four million and in one at two (`final/since-10.json`, 4.21 s), 3.0 and 3.3 s
  of it went to raising the copy's generation alone: three of the four `ALTER TABLE ... ADD
  CONSTRAINT` and `DROP CONSTRAINT` statements of that step took 0.6 to 1.3 s each in ClickHouse,
  statements of milliseconds otherwise, and its `query_log` shows them waiting for no lock. Why,
  this record does not know.
  One run at a million took 5.8 s, at a load average of 11.2: 2.9 s of it writing the map's
  markers, 1.6 s raising the copy's generation.
- **The settle.** It reads the group once, writes what differs and pauses nothing: 1.2 to 1.5 s at
  200 000 rows, 6.2 to 6.9 s at a million, 12.2 to 15.2 s at two and 24.5 to 26.5 s at four, in
  both directions - with nothing to write here, since nothing wrote during it. The backfill before
  it, which copies the group, took 28 to 52 s at two million.

## What this does not show

- A wide row, a key of several columns, or a key whose order two engines disagree on.
- Rows written since the settle in the middle of the key order: here they were new keys, the last
  ones.
- A settle beside a heavy writer. The live tests run one beside an application that writes for as
  long as the settle runs (`test_settle_live.py`); here nothing writes during the settle, and its
  time is the time of reading the group once.
- Elasticsearch, which has no read by write generation: its cutover reads the group whole, and a
  settle refuses it by name.
- Any machine but this one, and this one was in use.

## Reproducing

```bash
sudo make engines-up && export SDE_POSTGRES_DSN=... SDE_CLICKHOUSE_DSN=...
cd python/tests
R=../../docs/qualification/cutover-settled
../.venv/bin/python $R/measure_settled.py --rows 200000 1000000 2000000 --repeat 2 \
    --out settled.json
../.venv/bin/python $R/probe_pause_statements.py --rows 2000000 --out statements-2m.json
../.venv/bin/python $R/probe_pause_statements.py --rows 2000000 --since 50 \
    --out statements-2m-since-50.json
../.venv/bin/python $R/measure_settled.py --rows 2000000 --ways settled-written --since 10 \
    --out since-10.json   # and --since 50
../.venv/bin/python $R/measure_settled.py --rows 4000000 --ways settled-written \
    --out settled-4m.json
python3 $R/summarise.py   # the tables, from the files of final/
```

`SHA256SUMS` lists every file here but this one.

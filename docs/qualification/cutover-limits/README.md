# Moving a group between engines: how large a group fits the pause

A move - [staging](../../staging.md) protocol 1, a copy of the group in the other engine, then the
[cutover](../../local-cutover.md) - pauses the group's writes while the operator refills the copy
from the frozen source and compares every value. The pause grows with the group, and a cutover whose
pause passes its signed budget is stopped by the operator's watchdog and aborted on recovery, so the
group stays where it was. This record measures how large a group fits, in both directions between
PostgreSQL and ClickHouse, on the SDK's own code paths; finds where a move into PostgreSQL spent its
pause; and measures again after the change that removed most of it.
[`../in-place-index/`](../in-place-index/README.md) measured the same pause for a relayout within
one engine.

Everything here ran on one laptop (ThinkPad E570, Intel i3-7100U, 2 cores / 4 threads, ~16 GB),
against the SDK's test engines in containers on the same machine - PostgreSQL 15.19 and ClickHouse
24.8.14.39 - on 7 November 2026. The signed pause budget is the live tests' 30 000 ms, and the
operator moves 1000 rows a chunk, its default. The rows are those of the live tests' group: three
integer columns (`id`, `value` and the write generation). A wider row costs more, so for a real group
these row counts are an upper bound. The load average before each run is in the raw results: 0.7 to
2.4 before the change and 1.8 to 4.3 after it, so the later numbers are, if anything, pessimistic.

## Before: the pause, by direction and size

`measure_moves.py` seeds N rows into the source, stages the copy with `LocalCutover.stage` and runs
the cutover with `execute`; the pause is the receipt's `elapsed_ms`. SDK at `c69c0cc`. Raw results:
`before/moves-first.json` (one run of each size), `before/moves-ch-pg.json` and
`before/moves-pg-ch.json`, each with the log of its run.

| source → target | rows | pause, each run (ms) | per row (µs) | outcome |
|---|---|---|---|---|
| ClickHouse → PostgreSQL | 10 000 | 1 378 | 138 | success (matched) |
| ClickHouse → PostgreSQL | 100 000 | 8 337 | 83 | success (matched) |
| ClickHouse → PostgreSQL | 200 000 | 17 334, 16 828, 16 275 | 81–87 | success (matched) |
| ClickHouse → PostgreSQL | 300 000 | 29 096, 26 088, 26 129, 26 431 | 87–97 | success (matched) |
| PostgreSQL → ClickHouse | 10 000 | 964 | 96 | success (matched) |
| PostgreSQL → ClickHouse | 100 000 | 3 787 | 38 | success (matched) |
| PostgreSQL → ClickHouse | 300 000 | 9 975 | 33 | success (matched) |
| PostgreSQL → ClickHouse | 600 000 | 19 173, 20 259 | 32–34 | success (matched) |
| PostgreSQL → ClickHouse | 900 000 | 28 368, 28 577 | 32 | success (matched) |

A move into PostgreSQL cost almost three times as much a row as a move out of it, and 300 000 rows
fitted the budget by under a second in one run of four.

## Where a move into PostgreSQL spent its pause

`measure_phases.py` runs the same move and times the operator's own steps (`_after_step` marks each
phase's start and end), at chunks of 1000, 5000 and 20 000 rows (`before/phases.json`):

| source → target | rows | chunk | pause (ms) | repair (ms) | verify (ms) | the rest (ms) |
|---|---|---|---|---|---|---|
| ClickHouse → PostgreSQL | 200 000 | 1 000 | 17 014 | 12 050 | 4 609 | 355 |
| ClickHouse → PostgreSQL | 200 000 | 1 000 | 16 648 | 11 979 | 4 330 | 339 |
| ClickHouse → PostgreSQL | 200 000 | 5 000 | 13 593 | 10 450 | 2 769 | 374 |
| ClickHouse → PostgreSQL | 200 000 | 5 000 | 12 770 | 10 234 | 2 203 | 333 |
| ClickHouse → PostgreSQL | 200 000 | 20 000 | 12 836 | 10 502 | 1 964 | 370 |
| ClickHouse → PostgreSQL | 200 000 | 20 000 | 12 215 | 9 904 | 1 958 | 353 |
| PostgreSQL → ClickHouse | 200 000 | 1 000 | 7 177 | 2 746 | 3 945 | 486 |
| PostgreSQL → ClickHouse | 200 000 | 1 000 | 6 524 | 2 800 | 3 221 | 503 |
| PostgreSQL → ClickHouse | 200 000 | 5 000 | 4 238 | 1 569 | 2 219 | 450 |
| PostgreSQL → ClickHouse | 200 000 | 5 000 | 4 017 | 1 387 | 2 195 | 435 |
| PostgreSQL → ClickHouse | 200 000 | 20 000 | 4 168 | 1 657 | 2 045 | 466 |
| PostgreSQL → ClickHouse | 200 000 | 20 000 | 3 964 | 1 107 | 2 037 | 820 |

The repair - emptying the copy and refilling it from the frozen source - took 12 of the 17 seconds
of a move into PostgreSQL, and a larger chunk barely shortened it: the cost was per row, not per
round trip. `profile_move.py` ran one such move of 50 000 rows under cProfile
(`before/profile-ch-pg.txt`): `PostgresEngine.copy_in` took 3.7 s of the repair's 4.5 s, about
1.3 s of it converting a statement of 3000 parameters on the client and most of the rest waiting
for the server.

`copy_in` sent each chunk as `INSERT ... VALUES (...), ... ON CONFLICT DO NOTHING`, the form that
makes a resumed backfill's recopied chunk harmless. `probe_insert_paths.py` writes the same 50 000
rows into a table shaped like a repaired copy - a primary key and the write fence's constraints - six
ways, with psycopg directly (`probe_insert_paths.out.json`, after the change, which it does not use):

| path | µs a row, each pass |
|---|---|
| `values_conflict` (what `copy_in` sent) | 43.9, 40.9 |
| `values_plain` | 23.7, 15.6 |
| `unnest_conflict` | 46.0, 35.2 |
| `unnest_plain` | 17.8, 14.2 |
| `copy_then_conflict` | 36.5, 40.2 |
| `copy` | 7.2, 7.3 |

The cost is the conflict clause, whatever carries the rows: every form with it is 35 to 46 µs a
row, `COPY` into a temporary table first included, and a plain `COPY` is 7.

## The change

`copy_in` now sends a chunk by `COPY` and goes back to the conflict form only when that is refused
for a key already there. A `COPY` is one statement, so a conflict anywhere in the chunk writes none
of it, and the conflict form then writes what it always wrote: a backfill resumed after a crash
still recopies its last chunk harmlessly, through the slower path. The first attempt is its own
transaction, or a savepoint inside a caller's. `tests/test_postgres_copy_in_live.py` writes every
neutral type, with the values most likely to differ between a text `COPY` and a bound parameter,
both ways and compares them as the server prints them. It also found that `copy_in` could not send
a `json` value read from PostgreSQL at all, which the change fixes too.

## After

The same moves with the change (`after/`). They ran at `6ecb7bb`, the change on `c69c0cc` before it was rebased onto #172; its `copy_in` is the one merged, and the merged source differs from it only by #172's connection rules:

| source → target | rows | pause, each run (ms) | per row (µs) | outcome |
|---|---|---|---|---|
| ClickHouse → PostgreSQL | 100 000 | 4 922, 4 590 | 46–49 | success (matched) |
| ClickHouse → PostgreSQL | 300 000 | 16 445, 16 427 | 55 | success (matched) |
| ClickHouse → PostgreSQL | 450 000 | 25 601, 26 111 | 57–58 | success (matched) |
| ClickHouse → PostgreSQL | 600 000 | stopped at 30 182 and 30 156 | – | aborted on resume |
| PostgreSQL → ClickHouse | 300 000 | 9 462, 9 386 | 31–32 | success (matched) |
| PostgreSQL → ClickHouse | 900 000 | 29 033, 28 582 | 32 | success (matched) |

At 600 000 rows the operator's watchdog ended the pause at the budget, as designed. Fresh
connections and `resume` - what a customer does next - aborted the cutover
(`recovered_before_decision`) in 433 and 444 ms: the source reopened, the group stayed in
ClickHouse, and no write was lost. A move out of PostgreSQL is unchanged, as it should be: its
target is ClickHouse.

| source → target | rows | chunk | pause (ms) | repair (ms) | verify (ms) | the rest (ms) |
|---|---|---|---|---|---|---|
| ClickHouse → PostgreSQL | 200 000 | 1 000 | 11 256 | 5 903 | 4 900 | 453 |
| ClickHouse → PostgreSQL | 200 000 | 1 000 | 9 686 | 5 132 | 4 206 | 348 |
| ClickHouse → PostgreSQL | 200 000 | 5 000 | 5 527 | 2 099 | 3 035 | 393 |
| ClickHouse → PostgreSQL | 200 000 | 5 000 | 7 400 | 4 357 | 2 414 | 629 |
| PostgreSQL → ClickHouse | 200 000 | 1 000 | 6 957 | 2 748 | 3 388 | 821 |
| PostgreSQL → ClickHouse | 200 000 | 1 000 | 6 627 | 2 702 | 3 391 | 534 |
| PostgreSQL → ClickHouse | 200 000 | 5 000 | 4 131 | 1 292 | 2 145 | 694 |
| PostgreSQL → ClickHouse | 200 000 | 5 000 | 4 070 | 1 385 | 2 150 | 535 |

The repair of a move into PostgreSQL is half what it was. The comparison is now as long as the
repair, and it is the part a larger chunk shortens: at 5000 rows it is 2.4 to 3.0 s instead of 4.2
to 4.9. The operator's chunk is not configurable today, and it also bounds the memory a chunk of
wide rows takes, so this record does not change it.

## What this means

- **Within a 30 s budget on this machine, for rows this narrow: about 900 000 rows from
  PostgreSQL to ClickHouse, and about 450 000 from ClickHouse to PostgreSQL** (300 000 before the
  change). 900 000 is the edge rather than a margin: the first attempt at the after runs
  (`after/first-attempt/`) stopped at the budget in one of its two runs of that size.
- **The budget is signed for each cutover** (`--pause-budget-ms` when the controller prepares it).
  A larger budget moves a larger group in a longer write pause; the pause is close to linear in
  the rows, about 32 µs a row out of PostgreSQL and 46 to 58 µs into it here.
- **A group that does not fit is not moved.** The cutover is stopped at its budget and aborted on
  recovery, with the source as it was. The failure mode is a move that does not happen.
- **The pause is a write pause.** The frozen source stays readable throughout.
- **Qualify the intended group on the intended engines.** These scripts run against any pair the
  live tests can reach.

## Files

- `measure_moves.py` - the pause as a whole. It gained the recovery path (fresh connections and
  `resume` after the watchdog) after the before runs, none of which needed it; a run that needs no
  recovery runs the same code. The first attempt at the after runs, without it, ended at the first
  overrun: its logs are in `after/first-attempt/`.
- `measure_phases.py` - the pause by phase and chunk size.
- `profile_move.py` - one move under cProfile.
- `probe_insert_paths.py` - six ways to write a chunk, with psycopg only.
- `before/`, `after/` - the results, each with the log of its run; `after/run-head.txt` names the
  commit measured. In the logs the interpreter's warning about the relative path it was started by
  is removed and the worktree's path is written `<worktree>`; nothing else in them is changed.
- `SHA256SUMS` covers every file here except this README.

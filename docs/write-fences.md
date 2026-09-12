# Native write generations and barriers

`WriteFence` is a client-side provisioning primitive for coordinating migration cutover. It can
close a table to new INSERTs, wait for started writers, and admit a new write generation. A
successful barrier is a prerequisite for a stable final comparison; it does not itself compare
rows, activate a placement map, or authorize removing a source. The current `Session.save` API
has not yet been connected to this primitive. Use the generation-bearing map and cutover workflow
when those integrations are available; provisioning a fence under an existing session will make
its unstamped writes fail.

Python: `engine.write_fence(table, project_id=...)`. TypeScript:
`engine.writeFence(table, { projectId })`. These factory methods return the synchronous/asynchronous
forms of the same API:

| Method | Effect |
|---|---|
| `state()` | Read and validate ownership, column definition, predicates, epoch bounds and named barriers. |
| `prepare(epoch)` | Provision an absent fence; resume interrupted setup, or return an already matching state. It cannot change an existing epoch. |
| `freeze(request_id)` / `freeze(requestId)` | Close admission and drain started writers. Retrying repeats the drain. |
| `advance(epoch)` | Install a nondecreasing generation while a named barrier remains installed. |
| `release(request_id)` / `release(requestId)` | Retire and remove only this barrier. Other barriers remain; an interrupted generation change cannot be released. |
| `resume(request_id)` / `resume(requestId)` | Restore a table detached by this recorded operation, then repeat its barrier. |
| `resume_prepare(epoch)` / `resumePrepare(epoch)` | The corresponding recovery path for interrupted initial setup. |

Use dedicated provisioning connections and serialize executor commands in one durable project
state directory. PostgreSQL refuses DDL inside an application transaction. Runtime connections
need INSERT/SELECT rights, not permission to change constraints, alter columns, attach/detach a
table, or write the drain-intent log. A failed DDL may leave the table closed; inspect/resume that
operation. There is no implicit rollback after an uncertain server response.

## Stored protocol

This protocol is shared by the Python and TypeScript SDKs. It is independent of the placement-map
format version. `project_id` and barrier ids are 32 lowercase hexadecimal digits. Epochs are positive
safe integers, at most 2^53−1; an integral JSON number such as `1.0` normalizes to `1`, never to a
constraint named `min_1.0`. Fractional values and booleans are refused.

The reserved column is `__sde_write_epoch`: PostgreSQL `bigint NOT NULL DEFAULT 0`, ClickHouse
`Int64 DEFAULT 0`. An unstamped INSERT gets zero and cannot pass a provisioned fence. Each runtime
write must carry its own session's generation, including a deferred copy write; setting a mutable
generation on an adapter shared by old and new sessions would let the old session impersonate the
new one. This is INSERT fencing; it does not protect arbitrary DELETE, mutation or administrative
SQL. Those operations are outside the current runtime API and require their own protocol.

Native CHECK constraints have these exact names and predicates:

| Name | Predicate |
|---|---|
| `__sde_f_owner_<project_id>` | true |
| `__sde_f_min_<epoch>` | `__sde_write_epoch >= <epoch>` |
| `__sde_f_max_<epoch>` | `__sde_write_epoch <= <epoch>` |
| `__sde_f_setup` | false while provisioning |
| `__sde_f_hold_<request_id>` | false while this barrier is active |
| `__sde_f_retired_<request_id>` | true; this completed id cannot be reopened or reused |

PostgreSQL uses `NOT VALID`: preexisting rows need not be rewritten with a new epoch, but new
INSERTs are checked. ClickHouse does not recheck old rows when adding a CHECK constraint. The
metadata readers accept the native spellings of this small predicate grammar, including PostgreSQL's
bigint literal cast. A name alone does not certify its predicate. A quoted identifier containing a
space is a different identifier; removing all whitespace before validation would accept it wrongly.
Unknown names in `__sde_f_`, contradictory ownership and incompatible reserved columns are refused.
The SDK's map, backfill and drain-log tables cannot be provisioned as application write targets.

Epoch advancement adds the new minimum, adds the new maximum, then removes exact older maximum
and minimum names. It never drops a newer bound discovered during the operation. Interruption can
leave `minimum > maximum`, closing admission; resumption completes the change. Completed barrier
ids are recorded **before** dropping their holds. Retries may repeat release, but a completed id
cannot be used for a subsequent freeze. Use a new unpredictable id for each barrier round.

`FenceState.as_record()` / `asRecord()` contains `identity` (the native table identity), `project_id`,
`column` (`absent`, `valid`, or `conflict`), `lower_epoch`, `upper_epoch`, `holds`, `retired` and
`closed`. Absent bounds/project are JSON null. Hold and retired lists are sorted. `closed` describes
these fences; it is not a promise that other engine constraints will accept an arbitrary row.

## Why the drain is engine-specific

PostgreSQL's constraint installation conflicts with active writers and waits for their transactions.
The explicit drain also takes a conflicting table lock, including on retry. The native regression
observes that wait, commits the writer, and requires its row to exist before the barrier returns.
See [PostgreSQL lock modes](https://www.postgresql.org/docs/15/explicit-locking.html) and
[ALTER TABLE](https://www.postgresql.org/docs/15/sql-altertable.html).

ClickHouse's metadata-only ADD CONSTRAINT does not drain an INSERT using an older metadata snapshot.
On 24.8.14.39, a controlled two-second INSERT finished successfully after ADD returned in 5.69 ms.
The implemented drain uses `DETACH TABLE ... PERMANENTLY SYNC`, then ATTACH; it waited for that INSERT
and retained the constraint after attachment. These short operations also temporarily affect reads.
The cutover executor must budget the entire barrier and verification, not only the initial ALTER.
See [ClickHouse constraints](https://clickhouse.com/docs/reference/statements/alter/constraint) and
[DETACH](https://clickhouse.com/docs/reference/statements/detach).

Before DETACH, `__sde_fence_drains` records `table_name String`, `table_uuid UUID`,
`project_id FixedString(32)` and `hold String`, in a local MergeTree ordered by
`(table_uuid, project_id, hold)`. Repeated identical records are allowed. A detached table is
reattached only when the supplied operation has a recorded intent matching its exact Atomic UUID;
its owner and hold must still match afterward. The log is metadata in the customer's engine, with
that engine's durability guarantees. It contains no application rows or credentials. Deleting it
before recovery can leave a detached table that requires explicit operator recovery.

The initial scope is ordinary PostgreSQL tables without inheritance, and local MergeTree or
ReplacingMergeTree tables in a ClickHouse Atomic database. Distributed and replicated tables, foreign
tables, external writers and uncoordinated DDL changes require separate qualification. Application
Session integration, a bounded cutover executor, final verification under the barriers, map handoff,
and the full migration fault/load gate remain separate work. The engine primitive alone does not
make an online migration safe.

## Shared fixtures

`migration/046`–`061` use `fencing.json` and `calls.json`. These cases need no logical model or row
fixture: initialize a recording backend with the supplied metadata, bind the independently supplied
project, execute each step, and compare its state or named refusal plus the complete call sequence.
`add`, `drop`, `column`, `drain` and `restore` calls use the same argument order in both runners.
`conformance/tools/fencing_vectors.py` writes explicit states/traces without importing an SDK.
Real-engine tests separately cover native constraints, quoting, old writers, cross-language DDL,
and recovery after confirmed DETACH.

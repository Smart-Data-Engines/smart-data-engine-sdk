# Local durable cutover operator

The Python SDK provides `LocalCutover` and the `sde-operator` command for executing one signed
[cutover packet](cutover-packets.md) in the customer's environment - a move to another engine
binding (protocol 1) or a relayout within the source's own binding (protocol 2), by the same steps
on distinct physical tables. The operator repairs the copy,
compares frozen data, persists its decision, activates the authorized map and can recover after a
process crash. It serves applications using either SDK; the TypeScript interoperability test uses
an independent Python operator while a TypeScript application's fan-out is suspended.

This is a local execution component. Controller version reservation, receipt acceptance, [staging](staging.md) handoff and workload qualification remain separate integration work. Its
presence does not make the complete product production ready. No package release is implied by a
merge to main; use the reviewed distribution artifact when qualifying a deployment.

## Supported configuration

- One local POSIX project directory, on a filesystem supporting durable `fsync`, atomic replacement
  and `flock`. All operators for that project share this directory. Do not use NFS, multiple copies
  of the directory, or restore an older directory over a live project.
- A dedicated Python process, running in its main thread, with no existing `SIGALRM` timer.
  Application processes own their separate connections and consume the active map.
- Connected stock PostgreSQL, ClickHouse and Elasticsearch adapters, with separate operator and
  runtime-probe connections. PostgreSQL operators use autocommit; probes use the real runtime login
  and schema. ClickHouse repair and metadata writes are synchronous even if the caller enabled async
  inserts. Elasticsearch moves access by blocks rather than grants
  ([below](#elasticsearch-access-by-blocks)).
- Ordinary persistent PostgreSQL tables without inheritance, user triggers, rules or row-level
  security; local MergeTree or ReplacingMergeTree tables in an Atomic ClickHouse database, without
  dependent views or row policies. Replication, Distributed tables and external writers are outside
  this qualification. Schema and access administration must be serialized with this operator.
- Direct runtime logins with SELECT and INSERT on the declared tables and the prepared map
  bookkeeping table. Runtime has no role memberships, ownership, schema CREATE, mutation/DDL,
  grant-option, PUBLIC, wildcard or column grants. Supply every runtime login through its own probe.
  Any other principal with a grant covering a table of the operation is refused, roles included,
  and the refusal names it. An administrator is not: a PostgreSQL superuser, or a ClickHouse user
  with a direct global `ACCESS MANAGEMENT` ([runtime roles](runtime-roles.md#the-operators-login-and-the-principals-beside-it)).
  Database administrators are trusted operators, not application accounts; this protocol does not
  revoke administrative powers.

The operator compares native server, database and table identities. Different aliases that resolve
to the same physical table are refused before repair. A migrating table cannot also serve an
unaffected group. Recovery checks those identities again, including native login identities, and
will not attach or truncate a replacement table. Retired source names on success and abandoned target names on abort remain in local history and
cannot be used for another materialization.

## Elasticsearch: access by blocks

An Elasticsearch API key's privileges are fixed when the key is made, and a change to its owner's
role does not reach it, so the operator cannot revoke one login's access as it does with SQL grants.
It uses the index block instead, which refuses every principal at once: the runtime keys, a login
the operator does not know, and the operator itself. Only a principal that may change the index's
settings can lift one. Such a principal is an administrator, and this protocol does not revoke
administrative powers, as on the SQL engines.

| Step | PostgreSQL, ClickHouse | Elasticsearch |
|---|---|---|
| The pause (`deny_target`, `deny_source`) | runtime grants revoked | nothing revoked: the holds refuse writes, and the frozen data stays readable |
| After the decision (`confirm_source_denied` on success, `confirm_target_denied` on abort) | revoked, and every runtime login seen refused | a read block for good; the retired side keeps its holds, so its write block stays too |
| Opening the side in force (`enable_runtime`, a staging's grants) | granted, and every runtime login seen allowed | no read block, and every runtime key seen to read the index and hold `index`, `read` and `view_index_metadata` on it |

Reads during the pause are safe. The source is frozen, and its rows are the ones the repair copies
and the comparison checks. The copy is read only through the map of a success, which is published
after the decision and after the source's read block. So a reader of the old map cannot see a
source older than the copy. A retired index refuses even the administrator's search until an
administrator lifts the block (`test_elasticsearch_operator_live.py`).

What the operator checks:
- **Its own login** reads the cluster's identity, `GET /`, which needs the cluster privilege
  `monitor`. With `manage_pipeline` for the fences and `all` on the layout's indexes, `sde_m_*`,
  `sde_map_state` and `sde_backfill_state`, that is what a login of its own needs; the cluster's
  administrator has it.
- **Each runtime key**, through `_has_privileges` asked as that key. It needs `index`, `read` and
  `view_index_metadata` on each index it uses and on the fresh names of a staging, and the cluster
  privilege `read_pipeline`. It may not hold `manage`, `all`, `write`, `delete_index` or `delete`
  on any of them, nor the cluster privileges `all`, `manage`, `manage_pipeline`,
  `manage_ingest_pipelines` or `manage_security`: each could lift a block, rewrite a fence, grant
  itself more or - `delete`, and `write`, which includes it - remove documents past the fence. A
  delete runs no ingest pipeline, where an index or an update runs the final one (measured on 9.5.5),
  and the library never deletes a document.
- **The same, by action.** A role can name an action and none of the privileges above, and then
  each privilege that implies the action reads false. So the operator also asks for the action
  `indices:admin/settings/update` on each index, which lifts a block or unsets a fence's pipeline.
  It also asks for the cluster actions that write or delete a pipeline
  (`cluster:admin/ingest/pipeline/put`, `.../delete`) and that restore a snapshot
  (`cluster:admin/snapshot/restore`). And it asks for those that write a role, a role mapping, a
  user or a password (`cluster:admin/xpack/security/role/put`, `role_mapping/put`, `user/put`,
  `user/change_password`). A key holding any of them is refused by the action's name. Measured on
  9.5.5: a key whose role named the settings and pipeline actions and no refused privilege lifted
  a write block, unset `final_pipeline` and wrote a pipeline.
- **The fresh names.** A key that does not cover a staging's fresh name,
  `sde_m_<stage>_<position>`, is refused before anything is created. Give it a pattern such as
  `sde_m_*`, because nothing here grants a privilege.
- **No list of other principals.** A block refuses every principal whatever it holds, so the SQL
  engines' refusal of an undeclared grantee has nothing to do here.
- **Each index** by its `index.uuid`, on the cluster of the operator's `cluster_uuid`. A runtime key
  cannot read the cluster's identity, so it is placed on the same cluster by the uuid of
  `sde_map_state` it reads. An alias or a pattern under a layout's name is refused. So is an index
  with a default ingest pipeline, which changes documents before the fence sees them, as a trigger
  would; one with a lifecycle policy; and one with a block this protocol never sets.

The repair empties the copy with `_delete_by_query`, which takes time in the number of documents,
where SQL's `TRUNCATE` does not. It then copies with `create` actions visible on return. A relayout
(protocol 2) and an [in-place index build](in-place-index.md) are refused on Elasticsearch by name:
a layout there names its fields and nothing else, and every field is indexed already.

## Local configuration and command handoff

Install a reviewed wheel with the `signed`, `postgres` and `clickhouse` extras in an operator venv;
the Elasticsearch adapter needs no extra.
For a source checkout, the equivalent development installation from the repository root is:

```sh
python -m pip install './python[signed,postgres,clickhouse]'
```

`local-operator.json` contains local enrollment and environment variable names, never their values.
The neutral model must describe the model used to issue the maps. For example:

```json
{
  "protocol": 1,
  "project_id": "11111111111111111111111111111111",
  "model": {
    "entities": [{
      "name": "Event",
      "fields": [{"name": "id", "type": "int64"}, {"name": "value", "type": "int32"}],
      "key": ["id"]
    }]
  },
  "public_keys": {"primary": "BASE64_ENCODED_TRUSTED_ED25519_PUBLIC_KEY"},
  "engines": {
    "pg-main": {
      "dialect": "postgres",
      "operator_dsn_env": "CLIENT_PG_OPERATOR_DSN",
      "runtime_dsn_envs": ["CLIENT_PG_APP_DSN"]
    },
    "ch-main": {
      "dialect": "clickhouse",
      "operator_dsn_env": "CLIENT_CH_OPERATOR_DSN",
      "runtime_dsn_envs": ["CLIENT_CH_APP_DSN"]
    },
    "search": {
      "dialect": "elasticsearch",
      "operator_dsn_env": "CLIENT_ES_OPERATOR_DSN",
      "runtime_dsn_envs": ["CLIENT_ES_APP_DSN"]
    }
  }
}
```

`chunk_rows`, optional, is the number of rows a cutover's repair and its frozen comparison move at
once: 1000 when it is absent, at most 20 000. A larger chunk makes fewer round trips inside the
pause - at 5000 the measured pause of a 200 000-row move was a third to a half shorter
([qualification/cutover-limits](qualification/cutover-limits/README.md)) - and holds more rows in the
operator's memory at once, a few copies of each, so a group of wide rows keeps the default.

`repair`, optional, is how a cutover makes its copy the frozen source's equal inside the pause:
`diff` when it is absent, or `refill`. `diff` compares the two and writes over the rows that differ.
`refill` empties the copy and copies every row, which is what every cutover did before `diff`
existed and what `diff` goes back to by itself where it cannot do better
([Filling the copy before the pause](#filling-the-copy-before-the-pause)).

Obtain the project id and public keys through the authenticated enrollment channel, independently
of the supplied packet. Populate the named variables locally. For PostgreSQL, operator and runtime
connections must resolve the same schema/search path. Engine binding names must match the map and
cover every engine the application's signed sessions use for watermark protection.

Before enrollment, provision the signed before-map with `sde.prepare_schema` using operator
connections, including an otherwise unused supplied engine's bookkeeping. It creates the copy's
tables with their mark (format contract §7r) and refuses a table of a copy's name that does not
carry it: a map naming a copy like a table you already have does not make that table the copy. The
operator reads the mark again before every command that writes to the copy. A copy provisioned
before copies were marked is adopted once with `sde-operator adopt-copy --group <group>`, which
lists the tables and marks them only with `--confirm` and the digest of that listing. Set up the restricted
runtime grants described in [runtime roles](runtime-roles.md). Enrollment only records the exact
signed map; it does not provision databases or grant runtime access. It takes one input snapshot
and applies the map contract's integral-JSON normalization before both verification and publication,
so caller changes after verification cannot replace the document being persisted.

```sh
sde-operator --config local-operator.json --project-dir ./client-state enroll --map before.json
sde-operator --config local-operator.json --project-dir ./client-state status
sde-operator --config local-operator.json --project-dir ./client-state backfill
sde-operator --config local-operator.json --project-dir ./client-state execute --plan cutover.json
sde-operator --config local-operator.json --project-dir ./client-state current-map
```

`backfill` fills the copy before the pause, and is what makes the pause short
([Filling the copy before the pause](#filling-the-copy-before-the-pause)).

The same configuration and project directory run the other signed operations: `stage`
([staging](staging.md)), `index` ([in-place index builds](in-place-index.md)) and `advance`
([generation advances](generation-advance.md)). `resume` ends whichever of them is unfinished.

`status` and `current-map` work without database connections or connection environment variables.
`current-map` verifies and prints the same snapshot of the signed file. The status distinguishes
`active_map_version` (the actually published file) from `recorded_map_version` (the last completed
execution), and includes an unfinished plan's phase and durable decision. Application code may use
`load_local_map(directory, model=..., project_id=..., public_key=...)` to load the active file.
An old-write refusal requires an explicit refresh/retry policy; the SDK does not replay a failed
operation against another engine automatically.

The initial handoff requires a prepared source-and-copy before-map. A new map must not be installed
by deleting local state or re-enrolling over history. Successive-map staging belongs to the remaining
controller/setup integration; the current CLI deliberately provides no such shortcut.

## Decision, activation and recovery

The lock covers the entire read/decide/write operation, across threads and processes. State and
active-map files use complete temporary inodes, file fsync, atomic publication and directory fsync;
new directories and files are restricted to the local operator. A versioned checksum envelope
rejects missing/corrupt state and a state file from another project or model. The checksum detects
corruption; it is not an authentication mechanism against a local state administrator.

Before changing access, the operator validates the packet against the signed current map,
qualifies schemas, capabilities, native identities, runtime privileges, existing epochs and holds,
and records its execution intent. It then:

1. Revokes target runtime access, drains the target, freezes the source and revokes source access.
2. Opens the target only to operator repair at E+1. Where the copy is mostly there, it reads the
   frozen source and the copy side by side and writes over the rows that differ - a row the copy
   lacks, a row it holds with another value - stamping each with E+1 and reading it back. Where it
   is not, it truncates that disposable copy and rebuilds it from the frozen source, stamping every
   row with E+1. Either way this includes committed source rows whose original application process
   failed before fan-out.
3. Freezes and drains both copies again and compares, under those barriers. After a rebuild: the
   exact values of every row, and stable counts. After a repair of what differs: the exact values
   of every row of the copy that carries E+1, and stable counts. No AI is involved, and no rows are
   sent to the controller.
4. Persists success or abort before native activation. Success retires the source at E+2 and
   activates the target at E+2. Abort restores the complete source at E+1 and keeps the target closed.
5. Records the terminal map version, atomically publishes its signed map, restores runtime access
   only on the active authority, releases its barriers and records the receipt.

Each native step has a persisted intent. An interruption before a decision is recovered as abort;
a persisted success is completed as success. A lost response never authorizes changing that
choice. A directory-fsync failure after publication is an uncertain outcome, not a rollback.
After a fault, preserve the directory, inspect it, restore access to the same engines and run:

```sh
sde-operator --config local-operator.json --project-dir ./client-state resume
```

The new invocation opens fresh dedicated connections. A permanently detached ClickHouse table is
reattached only with the matching stored UUID and native drain intent. Partial generation changes
are completed without lowering an epoch. A replaced table/login or another active map is refused.
Repeated `execute` for an already completed identical packet returns its saved receipt. `resume`
requires an unfinished execution; a response lost after completion can be recovered with `execute`.
Do not drop retired tables or remove the state on the basis of a missing response. A cutover has no
`abandon`: without a durable decision its recovery aborts it by itself. `abandon` ends an unfinished
[staging](staging.md#abandoning-a-staging-that-cannot-finish) or
[in-place index build](in-place-index.md#abandonment) instead.

## Pause budget and receipt

A process watchdog bounds native waits independently of driver exception translation. Preflight
and each recovery invocation use a 30-second watchdog. The signed pause budget starts after
preflight and covers the pause through reopening the active generation. An interrupted native
statement can have an unknown server-side outcome: the dedicated connections are closed and the
operator reports recovery required. This is not a guarantee that a database failure restores
application availability within the signed budget.

A detected budget overrun before a success decision selects the prepared abort. An interruption
around a durable decision requires inspecting/resuming it. Recovery never changes a persisted
success to abort. `within_budget` is false for every recovered execution. `elapsed_ms` describes the
measured invocation's pause/activation region, not downtime during an intervening process outage.
Qualify the intended volume against the signed budget; a tiny successful test is not a throughput
or availability commitment for a customer's workload.

A receipt contains protocol, plan id and fingerprint, project, group, outcome, terminal map version
and fingerprint, the bound verification result (or null before comparison), a reason, elapsed time,
`recovered` and `within_budget`. It contains no row values, connection strings or passwords.
`CutoverReceipt.as_record()` returns a fresh snapshot. The receipt is an observation from the
customer's operator, not a new signature authorizing placement. The controller must bind its
acceptance to its reserved packet through the authenticated operator handoff.

CLI exit codes: 0 is a result, 2 a refused/invalid input, and 3 an operation or storage failure
requiring inspection. A successful abort returns 0 with `outcome: "abort"`; consumers must read the
outcome. Driver error details are omitted from the JSON output to avoid including local credentials.

## Filling the copy before the pause

A staging creates the copy and publishes the map that fans new writes out to it, and copies
nothing. A cutover that follows it directly therefore copies the whole group inside its pause.
`sde-operator backfill` copies it before the pause instead, while the application keeps writing:

- It fills every copy the map in force names, from its source, with the operator's logins: the
  library's own `sde.backfill`, chunk first and marker after. A run that stops - a signal, a lost
  connection - is continued by the next one from its marker, and a run after the end copies nothing.
  A ClickHouse source is read by one statement from the marker, which costs the same a row whatever
  the table's size: measured, 4 million rows into PostgreSQL in 61 s where reading a statement a
  chunk took 248 s ([qualification/backfill-by-a-stream](qualification/backfill-by-a-stream/README.md)).
- It takes no barrier and pauses nothing. It runs under the project lock, so beside no staging,
  cutover or index build, and is refused while one is unfinished.
- It prints one record a group: the rows copied and whether the end of each table was reached.

The cutover then finds a copy that is the source's equal but for a few rows, and its pause
compares the two and writes over those: a row written by a session still on the earlier map, which
fans nothing out; a fan-out that failed after its source commit; and, on ClickHouse, a row whose
older version the backfill wrote after a newer one had arrived. Nothing is emptied and no row is
deleted.

**The cutover refills the copy whole, as before, where a repair of what differs is not the better
way or not possible**, and its local record says which it did:
- the copy holds less than half the rows of its source, which is the copy that was never filled;
- an engine of the copy cannot be written over and read by generation, which today is
  Elasticsearch;
- the configuration says `repair: "refill"`;
- the copy holds a key the source lacks, which only emptying removes;
- a row the repair wrote could not be read back as written;
- the repair's pass read another number of rows than the source counts: an answer that ended
  before the table did. ClickHouse gave one, without an error, to a login whose settings cap a
  result and say `break`
  ([measured](qualification/cutover-repair/README.md#an-answer-that-ends-before-the-table-does)),
  until every statement said `throw`; the check stays for whatever else ends an answer early.
  What the pass did not read, it did not compare.

**Why comparing the rows the repair wrote is the whole comparison.** While the copy is repaired it
is at the cutover's own write generation, E+1, and admits a write of that generation only: the two
constraints of the [write-fence protocol](write-fences.md), which PostgreSQL holds an `UPDATE` to as
well. So every row of the copy written or changed since then carries E+1, and a row that carries
another generation is as it was. At the decision, under the final barriers, a row of the copy
either carries E+1 and has just been compared with the source's, or carries another generation: it
has not changed since the repair's pass compared it with a source frozen since before that pass,
and had it differed, the pass wrote over it and read it back carrying E+1. The pass visits every
key of the source, which it holds itself to by the source's count, so the copy holds each, and
equal counts leave no room for a key the source lacks or for a row deleted after it was compared.
Nothing is inferred from a count or a checksum.

What this assumes is what the pause already assumed: an engine's own constraints hold for whoever
writes. One write goes past them, measured on ClickHouse 24.8: a mutation (`ALTER TABLE ... UPDATE`)
checks no constraint. No runtime login the operator admits can run one: it qualifies them as
holding `SELECT` and `INSERT` alone. An administrator's mutation was never seen once the comparison
had read its row. For a row the repair did not write, that reading is now the repair's pass, where
it was the comparison under the final barriers: earlier in the same pause, by the seconds between
the two.

It assumes too, as every comparison of this library does, that an answer without an error is the
whole answer, and the library makes that so where an engine would not: every statement it sends to
ClickHouse says that a limit is an error ([engine connections](engine-connections.md#clickhouse-an-answer-is-whole-or-an-error)),
so a login set to break its reads short, or its statements off by time, is answered the server's
error instead of a short count. The repair's pass still holds the one statement that reads a table
whole to the source's count. The operator's PostgreSQL connections refuse a read a row-level
security policy would narrow (`row_security = off`), and a table with a policy is refused where
its identity is taken, on either engine, by a backfill as by a cutover.

The receipt does not change: its comparison carries the same counts, taken the same way. Which way
the copy was repaired, and how many rows that compared and wrote, is in the completed plan's
`repair` record in `project.json`, numbers only.

## Settling before the pause

The repair above reads both sides of the group under the pause, so the pause still grows with the
group. A [generation advance](generation-advance.md) marks a point the engines enforce: from it on
every write of the group carries the map's generation, G, so a row below G has not been written
since, on the source or on the copy. `sde-operator settle` compares those rows before the pause,
while the application keeps writing, and the cutover's pause then reads only the rows that carry
G. The order:

```sh
sde-operator --config local-operator.json --project-dir ./client-state backfill
sde-operator --config local-operator.json --project-dir ./client-state advance --plan advance.json
sde-operator --config local-operator.json --project-dir ./client-state settle
sde-operator --config local-operator.json --project-dir ./client-state execute --plan cutover.json
```

The advance comes after the backfill: a backfill stamps what it copies with the generation in
force, and the rows a settle compares are the rows below the one in force when it runs.

**What a settle does**, for every group whose map names a copy:
- it reads the source in key order and compares each row below G with the copy's: a row the copy
  lacks is copied and one it holds with another value is written over, both under G - as a
  session's fan-out would write them - and read back. A copy row that carries G already is newer
  than what the settle read of the source, and is left;
- it ends only when a fresh statement for the rows below G after the last key it read answers
  none. A row below G cannot be written any more, so this ends however fast the application
  writes, and no count stands in for it: a source that is being written to has no count a walk
  could be held to. A ClickHouse source is read by one statement first, PostgreSQL by those
  statements alone;
- it takes no barrier and pauses nothing. It runs under the project lock, so beside no staging,
  cutover, index build or advance, and is refused while one is unfinished;
- it keeps, in `project.json`, a record a group: the generation, the map's version and
  fingerprint, the native identity of every table, and its numbers - rows read, rows below G,
  rows written, rows not read back. A pass that could not read back a write keeps no record,
  and removes an earlier one. The command prints the records.

**What the cutover's pause does then.** The cutover stands on the record when it was made from
the map the packet starts from - the same fingerprint, so no advance, index build or other map
came between - at the packet's generation, and over the tables the cutover has just qualified, by
their identities. Its repair, under the same barriers as before, is then:
1. the source's rows that carry G against the copy's, by key: a row the copy lacks or holds
   otherwise is written under the cutover's generation, E+1 here as above, and read back;
2. the copy's rows that carry G against the source's, by key: one that differs is written over
   with the source's, and a key the source lacks ends in the whole refill, as above. A copy row
   can carry G where the source's does not: a settle wrote it, or a backfill after the advance;
3. under the final barriers, as before: the copy's rows that carry E+1, and both counts.

The record of the completed cutover's repair says `"mode": "since"`. The record of the settle is
removed when the cutover ends, either way: the map it was made under is not in force any more.

**Why this is still the whole comparison.** At the decision a row of the copy carries E+1, and was
compared under the final barriers; or carries G, and was compared under the pause with a source
frozen since before that comparison - had it differed, it would carry E+1 now; or carries less,
and has not changed since the advance. The settle compared that last kind with the source's row,
which carried less too: if the source's row has changed since, it carries G now, step 1 compared
it with the copy's, found them different and wrote it, so the copy's carries E+1. Every row of the
source either carries less than G - and the settle found the copy's equal, or wrote it under G - or
carries G, and step 1 visited it. Equal counts leave no room for a key the source lacks.

A write of the application's that crosses a settle's - between the settle's read of a row and its
write of it - leaves the copy's row under G with the settle's older value. Both rows carry G, and
step 1 or 2 compares them. Nothing has to be ruled out for a settle to be right, only left
visible for the pause.

**What voids a record**, so that the cutover compares the whole group as above:
- a map other than the one it was made under: another advance, an index build, any cutover;
- an erasure, which removes every record before it deletes its first row;
- a table that is not the one it settled, by its native identity;
- a group on an engine without a read by write generation, which today is Elasticsearch: a settle
  refuses it by name, and its cutover reads it whole.

What still grows with the group under the pause: the counts under the final barriers, and the
first page of each read by generation, which scans the table. The pause also grows with the rows
written between the advance and the freeze, by about 70 µs for each where it was measured
([qualification/cutover-settled](qualification/cutover-settled/README.md)): a group of two million
rows paused 2.3 to 2.6 s from PostgreSQL into ClickHouse and 1.0 to 1.4 s the other way after a
settle, against 15.3 and 14.0 to 14.1 s after a backfill alone, and one of four million 4.0 and
1.8 s.

## How large a group a move fits

A group that does not fit its budget is not moved: the watchdog stops the pause at the budget and
recovery aborts the cutover, with the source as it was. What the pause covers depends on whether
the copy was filled before it, and whether the group was settled. Measured on a laptop (i3-7100U,
4 threads) with PostgreSQL 15.19 and ClickHouse 24.8.14.39 on the same machine, rows of three
integer columns and the 30 s budget of the live tests
([qualification/cutover-settled](qualification/cutover-settled/README.md),
[qualification/cutover-repair](qualification/cutover-repair/README.md), and
[qualification/cutover-limits](qualification/cutover-limits/README.md) for the pause as it was):

| move | the copy was | pause for 200 000 rows | rows within 30 s |
|---|---|---|---|
| PostgreSQL → ClickHouse | never filled: refilled in the pause | 8.1-10.6 s | about 900 000, at the edge |
| PostgreSQL → ClickHouse | filled by `backfill` | 2.5-3.5 s | 3 million, the largest size that fitted |
| ClickHouse → PostgreSQL | never filled: refilled in the pause | 11.5-11.9 s | about 450 000 |
| ClickHouse → PostgreSQL | filled by `backfill` | 2.2-2.6 s | 3 million, the largest size that fitted |
| PostgreSQL → ClickHouse | filled by `backfill`, then advanced and settled | 0.8-0.9 s (2.1 s after a backfill alone, the same day) | 4 million in 4.0 s, the largest measured |
| ClickHouse → PostgreSQL | filled by `backfill`, then advanced and settled | 0.6 s (1.8-2.2 s after a backfill alone, the same day) | 4 million in 1.8 s, the largest measured |

The budget is signed for each cutover, so a larger one moves a larger group in a longer write
pause, and a larger `chunk_rows` moves a larger group in the same one. A wider row costs more than
these. Qualify the intended group on the intended engines with the scripts in those records before
signing a budget for it; an index change needs no move at all
([in-place index builds](in-place-index.md)).

## Evidence

`test_local_cutover_live.py` exercises both directions and both repairs, missing fan-out, a copy
never filled, a key the source lacks, a source answer the server cut short, a write and a deletion
in the copy after its repair, mismatching repair,
checkpoints before and after success/abort decisions, lost completion responses, actual SIGKILL,
a competing process lock, permanent detach recovery, partial native epoch DDL and watchdog
interruptions. `test_operator_native_live.py` exercises actual restricted grants and refusal when
revoking SELECT leaves INSERT, a reader was not declared, or a disconnected probe is mistaken for
a permission denial. CLI subprocess cases use fresh connections and check for connection-string
leakage. `test_cutover_project.py` covers local I/O failure and corruption boundaries.
`test_settle_live.py` exercises a settle and the cutover after it in both directions and for a
relayout: no range or stream of either side read under the pause, a settle beside an application
that keeps writing, every checkpoint of the cutover after a settle, each binding of the record
alone, what voids it, a surplus, a write not read back and a spent pause budget.

`typescript/tests/local-cutover.live.test.ts` suspends TypeScript fan-out after the PostgreSQL
source commit, executes the Python operator, writes a newer value on the new ClickHouse source,
then resumes the old fan-out. The delayed old-generation write is refused and the newer value
survives. These are tests of the local component; the full reservation-to-demo workflow and sustained
workload acceptance remain required.

The operator uses the same SDK engine adapters. Configure customer-local operator and runtime
connections using the [verified TLS profile](engine-connections.md); never include their URIs
or CA files in a controller handoff.

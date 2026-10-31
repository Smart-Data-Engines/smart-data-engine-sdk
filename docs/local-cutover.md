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

Obtain the project id and public keys through the authenticated enrollment channel, independently
of the supplied packet. Populate the named variables locally. For PostgreSQL, operator and runtime
connections must resolve the same schema/search path. Engine binding names must match the map and
cover every engine the application's signed sessions use for watermark protection.

Before enrollment, provision the signed before-map with `sde.prepare_schema` using operator
connections, including an otherwise unused supplied engine's bookkeeping. Set up the restricted
runtime grants described in [runtime roles](runtime-roles.md). Enrollment only records the exact
signed map; it does not provision databases or grant runtime access. It takes one input snapshot
and applies the map contract's integral-JSON normalization before both verification and publication,
so caller changes after verification cannot replace the document being persisted.

```sh
sde-operator --config local-operator.json --project-dir ./client-state enroll --map before.json
sde-operator --config local-operator.json --project-dir ./client-state status
sde-operator --config local-operator.json --project-dir ./client-state execute --plan cutover.json
sde-operator --config local-operator.json --project-dir ./client-state current-map
```

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
2. Opens the target only to operator repair at E+1. It truncates that disposable copy and rebuilds
   it from the frozen source, stamping copied rows with E+1. This includes committed source rows
   whose original application process failed before fan-out.
3. Freezes and drains both copies again and compares exact values and stable counts. No AI is
   involved, and no rows are sent to the controller.
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

## Evidence

`test_local_cutover_live.py` exercises both directions, missing fan-out, mismatching repair,
checkpoints before and after success/abort decisions, lost completion responses, actual SIGKILL,
a competing process lock, permanent detach recovery, partial native epoch DDL and watchdog
interruptions. `test_operator_native_live.py` exercises actual restricted grants and refusal when
revoking SELECT leaves INSERT, a reader was not declared, or a disconnected probe is mistaken for
a permission denial. CLI subprocess cases use fresh connections and check for connection-string
leakage. `test_cutover_project.py` covers local I/O failure and corruption boundaries.

`typescript/tests/local-cutover.live.test.ts` suspends TypeScript fan-out after the PostgreSQL
source commit, executes the Python operator, writes a newer value on the new ClickHouse source,
then resumes the old fan-out. The delayed old-generation write is refused and the newer value
survives. These are tests of the local component; the full reservation-to-demo workflow and sustained
workload acceptance remain required.

The operator uses the same SDK engine adapters. Configure customer-local operator and runtime
connections using the [verified TLS profile](engine-connections.md); never include their URIs
or CA files in a controller handoff.

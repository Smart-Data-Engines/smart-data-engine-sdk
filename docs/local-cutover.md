# Local durable cutover operator

The Python SDK provides `LocalCutover` and the `sde-operator` command for executing one signed
[cutover packet](cutover-packets.md) in the customer's environment. The operator repairs the copy,
compares frozen data, persists its decision, activates the authorized map and can recover after a
process crash. It serves applications using either SDK; the TypeScript interoperability test uses
an independent Python operator while a TypeScript application's fan-out is suspended.

This is a local execution component. Controller version reservation, receipt acceptance, preparation
of successive migration maps and workload qualification remain separate integration work. Its
presence does not make the complete product production ready. No package release is implied by a
merge to main; use the reviewed distribution artifact when qualifying a deployment.

## Supported configuration

- One local POSIX project directory, on a filesystem supporting durable `fsync`, atomic replacement
  and `flock`. All operators for that project share this directory. Do not use NFS, multiple copies
  of the directory, or restore an older directory over a live project.
- A dedicated Python process, running in its main thread, with no existing `SIGALRM` timer.
  Application processes own their separate connections and consume the active map.
- Connected stock PostgreSQL and ClickHouse adapters, with separate operator and runtime-probe
  connections. PostgreSQL operators use autocommit; probes use the real runtime login and schema.
  ClickHouse repair and metadata writes are synchronous even if the caller enabled async inserts.
- Ordinary persistent PostgreSQL tables without inheritance, user triggers, rules or row-level
  security; local MergeTree or ReplacingMergeTree tables in an Atomic ClickHouse database, without
  dependent views or row policies. Replication, Distributed tables and external writers are outside
  this qualification. Schema and access administration must be serialized with this operator.
- Direct runtime logins with SELECT and INSERT on the declared tables and the prepared map
  bookkeeping table. Runtime has no role memberships, ownership, schema CREATE, mutation/DDL,
  grant-option, PUBLIC, wildcard or column grants. Supply every runtime login through its own probe.
  An undeclared table grantee is refused. Database administrators are trusted operators, not
  application accounts; this protocol does not revoke administrative powers.

The operator compares native server, database and table identities. Different aliases that resolve
to the same physical table are refused before repair. A migrating table cannot also serve an
unaffected group. Recovery checks those identities again, including native login identities, and
will not attach or truncate a replacement table. Retired source names remain in local history and
cannot be used for another materialization.

## Local configuration and command handoff

Install a reviewed wheel with the `signed`, `postgres` and `clickhouse` extras in an operator venv.
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
signed map; it does not provision databases or grant runtime access.

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
Do not drop retired tables or remove the state on the basis of a missing response.

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

# Prepare a fresh materialization

A signed staging packet authorizes one new maintained copy of a source-only group. The Python
`LocalCutover.stage` executor creates the copy in the customer's environment and publishes the
prepared map. Python and TypeScript applications can consume that map. The source remains
unchanged and authoritative; existing source-only sessions can continue writing. Staging does
not backfill data or switch reads. The subsequent [local cutover](local-cutover.md) repairs missing
fan-out, compares frozen data and activates one of its separately authorized outcomes.

## Signed packet protocol 1

The exact envelope fields are `kind: "sde-stage"`, `protocol: 1`, `stage_id`, `project_id`, `group`,
`current`, `prepared`, and `signature`. The ids are 32 lowercase hexadecimal digits. The signature
is Ed25519 over the canonical envelope without its top-level signature; its encoding and trusted
key selection follow [cutover packets](cutover-packets.md). Each nested map also has its own valid
signature. The packet fingerprint is SHA-256 of that canonical unsigned envelope. Integral JSON
numbers normalize by the existing map rules before validation and canonical encoding.

Both maps must use contract 4, match the locally enrolled project and model, and contain explicit
layouts. `prepared.map_version` must exceed `current.map_version`. The controller must also reserve
that version above every previously issued or reserved version, including an unused cutover outcome.
Signing or decoding a packet does not make its prepared map active.

Only the selected group changes: the current group contains exactly `source` and `write_epoch`;
the prepared group adds exactly one `derived` materialization and its id in `also_write`. Source,
write epoch, other groups, routing and other map attributes stay unchanged. Two spare generations
must remain for a future cutover. The new copy uses another binding; its layout and the source
layout cover exactly the group's entities and logical column names. Type feasibility is checked
using the connected engines and the controller's schema qualification.

Every target table has the name `sde_m_<stage_id>_<position>`, where position is a six-digit,
one-based index in Unicode code-point sorted group entity order. `staging_table_name` /
`stagingTableName` implement this rule; the maximum position is 999999. Names are 45 ASCII bytes,
and cannot overlap a current table or be reused from local retired/staging history. Use a fresh
random stage id for each preparation. Never recycle a stage id after an abort.

Load with `load_staging_plan(raw, model=..., project_id=..., public_key=...)` in Python or
`loadStagingPlan(raw, { model, projectId, publicKey })` in TypeScript. `StagingPlan` retains immutable
verified provenance, exposes `as_record()` / `asRecord()`, `prepared_payload()` / `preparedPayload()`,
and checks the signed current map with `check_current()` / `checkCurrent()`. Copying a parsed
object does not transfer that provenance. `migration/099`–`121` are shared fixtures, independently
encoded and signed with OpenSSL rather than either implementation.

## Local execution and recovery

Use the [operator configuration](local-cutover.md#local-configuration-and-command-handoff) and
restricted roles already enrolled for this project. Provision the source and bookkeeping for every
configured engine, including an unused target, before initial enrollment. Runtime must have direct
SELECT/INSERT on that bookkeeping; the operator creates and grants the future target tables.
All operators share one local POSIX state directory and serialize schema/access administration.

```sh
sde-operator --config local-operator.json --project-dir ./client-state stage --plan staging.json
sde-operator --config local-operator.json --project-dir ./client-state status
sde-operator --config local-operator.json --project-dir ./client-state resume
```

The executor first qualifies native endpoints, namespaces, source identities/generations, logins,
watermarks and the absence of future names. It persists a creation intent before creating anything.
PostgreSQL creates the table and its protocol comment in one transaction; ClickHouse includes the
comment in the single CREATE statement. CREATE does not use IF NOT EXISTS. A crash after native
creation but before recording the identity can recognize only the exact stage-owned marker.
An unrelated table is refused, even if its columns match. Once recorded, a native table identity
must remain identical. PostgreSQL indexes must match the exact parent table, key columns and
ordinary btree shape, including absence of INCLUDE columns.

The operator installs the current write generation, checks schema and runtime privileges, grants
target access, persists its prepared decision, advances map watermarks, publishes the active map
and finally records its receipt. Resume rechecks identities and repeats idempotent steps. A replaced
source/target/watermark, changed login, foreign hold or generation refuses unsafe continuation.
Staging and recovery use the operator's 30-second watchdog; a timeout requires fresh dedicated
connections and inspection of persisted state. It is not a workload latency guarantee.

Do not delete the project directory or manually replace `active-map.json` for the next migration.
The same directory retains stage receipts, cutover decisions and retired names through subsequent
successes and aborts. After the first staging intent, `project.json` uses storage contract 2; older
operators must refuse it. Completed retries reconfirm filesystem durability before returning the
stored receipt, including a retry after an uncertain final directory fsync.

## Receipt and limits

The metadata-only receipt contains `protocol: 1`, `stage_id`, `stage_fingerprint`, `project_id`,
`group`, `outcome: "prepared"`, `map_version`, `map_fingerprint`, `tables`, `elapsed_ms`, and
`recovered`. Each table identifies its engine binding, logical entity and native `identity`
(`dialect`, `server`, `database`, `namespace`, `object`, `name`). No rows or credentials are included.
`StagingReceipt.as_record()` returns an independent snapshot. The controller must validate the
receipt against its exact reserved packet before recording that prepared map as active.

A prepared copy can be incomplete: applications that still hold the old map write only to the
source. Reads remain on that source until a separately authorized, verified cutover completes.
This component alone does not qualify a customer workload or complete controller onboarding.

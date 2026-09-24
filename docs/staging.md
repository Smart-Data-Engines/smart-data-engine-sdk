# Prepare a fresh materialization

A signed staging packet authorizes one new maintained copy of a source-only group. The Python
`LocalCutover.stage` executor creates the copy in the customer's environment and publishes the
prepared map. Python and TypeScript applications can consume that map. The source remains
unchanged and authoritative; existing source-only sessions can continue writing. Staging does
not backfill data or switch reads. The subsequent [local cutover](local-cutover.md) repairs missing
fan-out, compares frozen data and activates one of its separately authorized outcomes.

## Signed packet protocols 1 and 2

Protocol 1 prepares the copy in **another engine binding** - a move. Protocol 2 prepares it in the
**source's own binding**, under fresh table names - a relayout: a new physical design (key order,
partition, indexes) in the engine the group is already in, taken over by the same cutover as a
move. The two are otherwise identical, and each is strict: protocol 1 refuses a copy in the
source's binding (`migration/109`), protocol 2 refuses one anywhere else (`migration/139`). A
separate number rather than a lifted refusal, because lifting a refusal changes a format: a packet
a released library refuses would become one a newer library executes, under the same number. An
older operator refuses protocol 2 by name. The map itself already refuses a copy in the source's
engine that reuses a source table, so a relayout copy cannot be the source under a second name.

The exact envelope fields are `kind: "sde-stage"`, `protocol` (1 or 2), `stage_id`, `project_id`, `group`,
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
must remain for a future cutover. The new copy uses another binding under protocol 1 and the
source's binding under protocol 2; its layout and the source
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
object does not transfer that provenance. `migration/099`–`121`, `138` and `139` are shared
fixtures, independently encoded and signed with OpenSSL rather than either implementation.

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
successes and aborts. After the first staging intent, `project.json` uses storage contract 2; after
an abandoned staging, storage contract 4, which operators that know contracts 1 to 3 refuse; older
operators must refuse it. Completed retries reconfirm filesystem durability before returning the
stored receipt, including a retry after an uncertain final directory fsync.

## Abandoning a staging that cannot finish

A staging can reach a state no resume repairs: another operation's barrier appeared on a source
table, a runtime login or its grants changed, the target engine refuses the table or does not
answer within the operator's watchdog, or somebody else's object took a copy's name. Before its
decision `prepared`, such a staging is abandoned:

```sh
sde-operator --config local-operator.json --project-dir ./client-state abandon
```

`LocalCutover.abandon()` records the decision `abandoned` first, then removes **only this
staging's own tables**: a table under a copy's name is removed when it carries this staging's exact
creation marker and, once its identity was recorded, that native object - also a table created
just before a crash, whose identity was never recorded. Anything else under the name is left
alone. Indexes and generation constraints go with the table. On ClickHouse the runtime logins'
grants on the copy are revoked as well: measured on 24.8.14.39, table grants outlive `DROP TABLE`
(`qualification/staging-abandon/`). The map in force, the watermarks and every running process
stay as they were. Abandonment needs only the same native database - not the runtime logins, not a
source free of another barrier - which is what keeps it available when a staging cannot finish.
An interrupted abandonment is finished by `resume` or by `abandon` again, and the authorization is
spent: `stage` with the same packet returns the abandonment.

After the decision `prepared` the next map is decided: `abandon` is refused, `resume` publishes the
prepared map, and the copy leaves through its cutover's abort. The same command also abandons an
unfinished [in-place index build](in-place-index.md).

## Receipt and limits

The metadata-only receipt contains `protocol: 1`, `stage_id`, `stage_fingerprint`, `project_id`,
`group`, `outcome` (`prepared`, or `abandoned`), `map_version`, `map_fingerprint`, `tables`, `elapsed_ms`, and
`recovered`. Each table identifies its engine binding, logical entity and native `identity`
(`dialect`, `server`, `database`, `namespace`, `object`, `name`). No rows or credentials are included.
`StagingReceipt.as_record()` returns an independent snapshot. The controller must validate the
receipt against its exact reserved packet before recording that prepared map as active.

A prepared copy can be incomplete: applications that still hold the old map write only to the
source. Reads remain on that source until a separately authorized, verified cutover completes.
This component alone does not qualify a customer workload or complete controller onboarding.

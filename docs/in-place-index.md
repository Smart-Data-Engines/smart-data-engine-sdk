# Build an index in place

A signed in-place build authorization adds indexes to the tables a group already uses, while the
application keeps writing to them. The Python `LocalCutover.index` executor builds them in the
customer's environment with the engines' own non-blocking forms, confirms them from the engine's
catalogue, and publishes the next map, which differs from the map in force only by declaring them.
No row is copied, no write generation is raised, and no process is fenced.

This replaces, for one kind of change, the path a physical design otherwise takes: a fresh copy
under new names ([staging](staging.md) protocol 2) and a [cutover](local-cutover.md) that copies and
compares every row while the source's writes are frozen. That pause grows with the table - measured
at 22.5 s for 300 000 rows on PostgreSQL, and a table of about 400 000 rows passes the cutover's
30 s budget and rolls back. The in-place build of the same index took 355 ms, and the longest gap
between two writes during it was 16 ms. Measurements and scripts:
[qualification/in-place-index](qualification/in-place-index/README.md).

Only adding indexes can be done in place. A new key order, a partition, other tables or another
engine still need a copy, and so does dropping or changing an index in force.

## Signed packet protocol 1

```json
{
  "kind": "sde-index",
  "protocol": 1,
  "index_id": "<32 lowercase hex>",
  "project_id": "<32 lowercase hex>",
  "group": "Event",
  "current": { "<the signed map in force>": "..." },
  "prepared": { "<the signed next map>": "..." },
  "build_budget_ms": 3600000,
  "signature": { "alg": "ed25519", "key_id": "...", "value": "..." }
}
```

The signature is Ed25519 over the canonical envelope without its top-level signature, with the
encoding and trusted-key selection of [cutover packets](cutover-packets.md); each nested map carries
its own valid signature. The packet fingerprint is SHA-256 of that canonical unsigned envelope.
Integral JSON numbers normalize by the existing map rules before validation.

A loader refuses the packet unless every rule holds:

1. exactly these fields; `kind` `sde-index`, `protocol` 1; both ids 32 lowercase hexadecimal
   digits; the locally configured project; valid signatures on the envelope and both maps; both
   maps contract 4 or later with explicit layouts;
2. `prepared.contract` is at least `current.contract` - it may rise from 4 to 5, because an index
   method first appears here - and `prepared.map_version` is greater than `current.map_version`;
3. the same groups; the same routing and other top-level attributes; every group but `group`
   byte-identical;
4. `group` is source-only in both maps - exactly `source` and `write_epoch` - with the same write
   generation;
5. the prepared source equals the current source in everything but `layout.indexes`: identifier,
   engine binding, tables, columns, `key_order`, `partition_by`;
6. the prepared indexes are the current ones, byte for byte and in order, followed by at least one
   new index, and the n-th new index is named `sde_i_<index_id>_<n>` with n as six digits from 1;
7. no new name is used by any layout of the map in force, as a table or an index name;
8. `build_budget_ms` is an integer from 1 through 86 400 000 (a day).

The methods and parameters of the new indexes are checked by the map loader, from the
[physical design](physical-design.md) vocabulary. Whether the source's engine can build them is
checked by the operator, because a map names engine bindings, not dialects.

Load with `load_index_plan(raw, model=..., project_id=..., public_key=...)` in Python or
`loadIndexPlan(raw, { model, projectId, publicKey })` in TypeScript. `IndexPlan` keeps immutable
verified provenance, exposes `as_record()` / `asRecord()`, `prepared_payload()` /
`preparedPayload()`, the new definitions in position order as `added`, and checks the signed map in
force with `check_current()` / `checkCurrent()`. `index_build_name` / `indexBuildName` implement the
naming rule; names are 45 ASCII bytes. `migration/143`-`179` are shared fixtures, encoded and signed
with OpenSSL rather than by either implementation, and every refusal names the fragment of the
message both libraries give.

## Local execution

Use the [operator configuration](local-cutover.md#local-configuration-and-command-handoff) already
enrolled for the project. The build needs no new grants: the tables stay the same tables.

```sh
sde-operator --config local-operator.json --project-dir ./client-state index --plan index.json
sde-operator --config local-operator.json --project-dir ./client-state status
sde-operator --config local-operator.json --project-dir ./client-state resume
sde-operator --config local-operator.json --project-dir ./client-state abandon
```

Before the first DDL the operator records an intent that binds: the signed map in force; the
source's native endpoint and table identities; each table's write barrier complete, at the map's
generation and **without any hold** - a build does not start beside another operation's barrier;
watermarks not newer than the map in force; the runtime logins as staging checks them; the
design in force read back from the catalogue without a finding; and nothing foreign under any new
name. A refusal at this point leaves no state behind.

**PostgreSQL** builds each index with `CREATE INDEX CONCURRENTLY`, never with `IF NOT EXISTS`. An
index of the bound name on the bound table, of the declared method and columns, not unique, without
predicate, expression, INCLUDE columns or sort options, is this build's own: if it is valid and
ready it is done; if it is not - what an interrupted concurrent build leaves - it is dropped with
`DROP INDEX CONCURRENTLY` and built again. Anything else under the name is somebody else's object
and the build refuses it. A concurrent build waits for every transaction with an older snapshot, so
a long transaction anywhere in the database - an idle session left in a transaction included -
holds it until that transaction ends or the build budget does.

**ClickHouse** adds each data-skipping index with `ALTER TABLE ... ADD INDEX`, rendered by the same
function as the index clause in `CREATE TABLE`, and materializes it with an asynchronous
`MATERIALIZE INDEX` mutation. Parts written after the ADD carry the index; the ones before carry it
once the mutation is done, and a finished mutation is what "built" means. The mutation is found
again by its recorded command after a restart rather than started twice. It runs on the server's
merge pool, so a pool that is stopped or saturated holds the build until the budget ends. The
server retries a failing mutation by itself; its last failure reason is carried in the Python
exception that the budget ends the wait with.

Before the decision the whole next layout is read back from the catalogue and must produce no
[physical finding](physical-design.md); every new index must be ready; and the bindings, logins,
tables and barriers are checked again. Then the decision `built` is recorded, the map watermark is
advanced in every configured engine, the next map is published atomically, and the receipt is kept.
Without a recorded decision, recovery builds on - unlike a cutover, where the source stands frozen
until a decision and recovery therefore aborts. Here nothing waits, and an index that gets built is
harmless. After `built`, recovery only finishes the publication.

The deadline is the signed build budget, not the 30-second watchdog of staging and cutover. Nothing
is paused while an index builds, so the budget is not a pause budget; it bounds a build on a server
that stopped answering. When it ends a build the operator closes its connections and stops; resume
or abandon with fresh ones. A PostgreSQL build interrupted this way may run on in the server until
it notices; recovery waits for it, and keeps what it finished or drops what it left unfinished.

## Abandonment

`LocalCutover.abandon()` / `sde-operator abandon` ends an unfinished build without publishing
anything, as long as its decision is not yet `built`. It records the decision `abandoned` first,
then removes this build's own indexes: PostgreSQL with `DROP INDEX CONCURRENTLY`; ClickHouse by
killing the materialization if it is still running and dropping the index with
`SETTINGS alter_sync = 0`, which leaves the catalogue at once instead of waiting for the merge pool
(measured: the default form waits for as long as merges are stopped). Objects under the bound names
that are not this build's are left alone. The map in force, the watermarks and every process stay
as they were, and the prepared map version stays burned - the authorization is spent. Abandonment
needs only the same native database and the bound tables; it does not need a barrier-free table or
unchanged logins, which is what keeps it available when a build cannot finish.

After the decision `built`, abandonment is refused: the next map is decided, and `resume` publishes
it.

## Receipt, state and retries

The metadata-only receipt contains `protocol: 1`, `index_id`, `index_fingerprint`, `project_id`,
`group`, `outcome` (`built` or `abandoned`), `map_version` and `map_fingerprint` of the map active
afterwards (the prepared map, or the map in force), `indexes` (engine binding, entity, index name
and the table's native identity for each), `elapsed_ms` and `recovered`. No rows, values or
credentials. The controller validates it against the exact packet it reserved before recording the
outcome.

After the first build, `project.json` uses storage contract 3, which adds the `indexes` history;
operators that know contracts 1 and 2 refuse it rather than ignore a history they would not keep.
Stage receipts, cutover decisions and retired names are carried unchanged. A retry of a completed
build returns the same receipt and reconfirms the directory's durability first; a retry of an
abandoned one returns the abandonment.

## What this does not do

- It does not drop, rename or change an index in force, and it does not change a key order, a
  partition, a table or an engine. Those are relayouts or moves, through a copy.
- It does not adopt an index somebody created under a bound name, even one of the right shape on
  the right table: a unique or partial index, or one on another table, is refused, and so is a table
  or view holding the name.
- A ClickHouse index counts as built on the evidence of a finished mutation. The server keeps the
  last `finished_mutations_to_keep` finished mutations of a table; a table with that many mutations
  after the build makes a recovery materialize the index once more - slower, never falsely ready.
- The command line does not print engine error text, as for every operator command: it can carry
  values. ClickHouse keeps the reason in `system.mutations.latest_fail_reason`, and the Python API
  carries the last one in its exception.
- It is a local execution component with the same scope as the cutover operator: a local POSIX
  state directory, ordinary PostgreSQL tables, local MergeTree/ReplacingMergeTree tables in an
  Atomic ClickHouse database, DDL only through the operator connection.

## Evidence

`python/tests/test_index_operator_live.py`, on both engines: a build held in the middle by the
engine itself while the application writes, then published; kept indexes and several new ones;
recovery after every durable step and after a killed operator process mid-build (PostgreSQL leaves
a leftover that is dropped and rebuilt, ClickHouse's mutation is found again, not repeated);
abandonment before the decision and its refusal after; an interrupted abandonment finished by
resume or by a second abandonment; foreign objects under the bound name refused before any DDL and
left alone by abandonment; another operation's barrier refused before and during a build; the
build budget ending a held build; the command line. `python/tests/test_physical_live.py` pins the
PostgreSQL physical check that reports a unique or unfinished index.

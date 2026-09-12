# Generation-bearing maps (contract 4)

A contract-4 placement carries the locally enrolled project identity and a write generation for
each colocation group. Every Session INSERT carries that generation, including copies deferred
until a transaction commits. Two sessions sharing an adapter retain their own generations. An old
session cannot gain the new one's write permission merely because the new session opened.

This is the Session integration of [native write fences](write-fences.md). Final stable comparison,
cutover activation/recovery, runtime-role revocation for stale reads and workload qualification still
need the complete cutover executor. A map being loadable does not authorize changing the active
native generation or deleting a source.

## Wire fields and compatibility

- Root `project_id`: exactly 32 lowercase hexadecimal digits. The session receives the same identity
  independently through its local configuration. It must not learn that identity from an input map.
- Each group has `write_epoch`: a positive safe integer, at most 2^53−1. It is separate from
  `map_version`; publishing a new map need not change the write authority.
- Contract 4 has a canonically encodable document. At its JSON boundary, integral numbers (including
  a spelling such as `2.0`) normalize to integers before signature verification and fingerprinting.
  Nonintegral, nonfinite and unsafe canonical numbers are refused. Canonical encoding's general
  strict API is unchanged; this normalization belongs to the new map reader.
- `project_id` and group `write_epoch` cannot be placed in a document declaring an earlier contract.
  Earlier readers must reject contract 4 rather than ignore the write requirement. Existing maps
  of contracts 1–3 continue in their legacy mode.
- The physical `__sde_write_epoch` column and `__sde_fence_drains` table are reserved. The technical
  column is not a logical model field or an application-supplied write value.

The loader owns and freezes the parsed placement. Copied map objects do not inherit loaded
provenance. The generation-aware session checks that provenance, local project and model before
checking each native table's generation and physical columns. These checks precede forward-only
watermark publication: presenting a signed future map whose generation has not been activated
cannot invalidate the map still serving the application.

## Provisioning and runtime

Prepare schema on dedicated provisioning connections before opening a runtime session:

```python
sde.prepare_schema(model, placement, provisioning_engines, project_id=LOCAL_PROJECT_ID)
session = sde.Session(model, placement, runtime_engines, project_id=LOCAL_PROJECT_ID)
```

```typescript
await prepareSchema(model, placement, provisioningEngines, { projectId: LOCAL_PROJECT_ID })
const session = await Session.open(model, placement, runtimeEngines, { projectId: LOCAL_PROJECT_ID })
```

The map is already loaded and verified, and the engine connections and project configuration are
local to the customer. Preparing an existing fence does not advance it. Moving to another generation
requires the explicit native barrier procedure. Provisioning is not a call made by the application
on every startup, and the control plane receives no engine credentials.

For contract 4, `Session.ensure_schema()` / `ensureSchema()` verifies existing schema and generations
without issuing DDL. Both adapters expose `validate_schema()` / `validateSchema()` for that read-only
check. Runtime credentials can therefore omit provisioning powers; complete role qualification and
cutover revocation are separate from this Session check. An adapter without native generation and
read-only schema support is refused. PostgreSQL and ClickHouse implement them; the orderbook adapter
continues with legacy maps and is not claimed to implement this migration protocol.

`get` removes the SDK's technical epoch column before returning a logical row, including when
identifier hashing is enabled. A caller cannot override the epoch by supplying the reserved physical
column. INSERTs are not silently retried after a stale generation is refused. Refresh the local map
and handle the operation's outcome in the application.

## Copying historical data

Rows may retain the epoch in which they were originally written. Advancing a native constraint does
not rewrite those rows. Backfill strips that bookkeeping value and writes the target's current
session epoch; verification compares the logical columns and ignores only the reserved epoch.
Neither a mismatch in those historical metadata values nor their equality proves anything about
application data. All existing loss/difference checks continue to run.

The shared session cases `migration/062`–`070` pin actual stored values and call order. The new map
refusals are `errors/039`–`049`; `signature/008` is signed independently by OpenSSL and includes
integral JSON-number spellings, an expected fingerprint, project and epochs. Live tests exercise
stale sessions on shared connections, future-map watermark refusal, both copy directions and a
late transaction fan-out that must not overwrite a newer generation's row.


## Acceptance record — 12 September 2026

Full suites passed against PostgreSQL and ClickHouse: 849 Python tests plus 10 optional orderbook
skips, 354 TypeScript tests, and 1130 control-plane tests with zero skips. Both conformance runners
pass 178 tests over 165 vector directories. The new cases include an OpenSSL-signed map with
integral JSON-number spellings and 20 map/session scenarios. Forty deliberate source mutations were
detected with named failing witnesses, exact restoration and passing baselines. Live witnesses
cover stale sessions sharing adapters, watermark ordering, changed model columns, historical copy
epochs and a deferred fan-out which cannot overwrite a newer generation.

Locally built wheel/sdist/npm artifacts passed archive checks. Clean wheel and npm installations
loaded a contract-4 map, stamped source and fan-out rows, and returned logical fields. No registry
publication or release-workflow run was performed. These checks do not replace the remaining
cutover, stale-reader, recovery and workload-qualification gates.

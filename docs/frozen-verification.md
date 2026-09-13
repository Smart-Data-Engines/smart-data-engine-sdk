# Exact comparison under native write barriers

`verify_frozen` / `verifyFrozen` closes and drains named native barriers before comparing a
migration's logical rows. It requires both the existing value-comparison verdict and equal stable
row counts, so a target-only row cannot pass. It checks each table's identity, owner, generation and
its own hold again before returning. A released/replaced barrier invalidates the result.

The operation leaves its holds installed on a matching result, a mismatch, or an interrupted
operation. It does not activate a map, grant runtime access, release writes or delete data. The
local cutover executor owns the durable decision that permits those actions. A returned verdict
must not be retained after its barriers are released and reused to authorize a later cutover.

## Operator context

`InspectionContext(model, placement, engines, project_id)` in Python, or
`new InspectionContext(model, placement, engines, { projectId })` in TypeScript, holds the loaded
before-map and local provisioning connections. It checks immutable loaded provenance, project,
model and engine coverage. It does not publish a runtime watermark and has no application
`save`/transaction API.

This distinction matters during maintenance: the source may still be generation 1 while the
prospective target has reached generation 2. A runtime Session must refuse that before-map as an
active instruction; the operator still needs its physical table identities for comparison. The
comparison receives the expected actual generations separately and verifies them against native
metadata. `InspectionContext` is for inspection/verification, not an unchecked runtime session or
a replacement for the existing `backfill(Session, ...)` contract.

Both forms require a `VerificationRequest` for the exact before-map and local project, a fresh
32-digit lowercase hexadecimal hold id, and an epoch mapping covering exactly the source and
`also_write` materialization ids. The request is validated before any table is closed. All physical
schemas and epoch values are checked before installing the first barrier; an incompatible target
must not needlessly close a healthy source.

```python
context = sde.InspectionContext(model, before_map, operator_engines, LOCAL_PROJECT_ID)
report = sde.verify_frozen(
    context, group, request=request, hold_id=hold_id,
    epochs={source_id: source_epoch, target_id: target_epoch},
)
```

```typescript
const context = new InspectionContext(model, beforeMap, operatorEngines, { projectId: LOCAL_PROJECT_ID })
const report = await verifyFrozen(context, group, {
  request, holdId, epochs: { [sourceId]: sourceEpoch, [targetId]: targetEpoch },
})
```

These are operator calls with previously loaded/verified inputs. They are not an application
migration recipe; the executor must still enforce its approval, active-map identity, deadlines,
role qualification, state transitions and recovery procedure.

## Result protocol

The Python `as_record()` and TypeScript `frozenVerifyRecord()` produce the same metadata-only
record:

- `protocol`: 1.
- `comparison`: the existing bound verification record (request, timestamp and counts), excluding
  local row differences and values.
- `barriers`: records sorted by engine, materialization and table in code-point order. Each contains
  `engine`, `materialization`, `table`, native `identity`, `project_id`, `epoch`, and `hold_id`.
- `elapsed_ms`: nonnegative integer duration of this call's barrier/drain/comparison region.
- `matched`: true only when the logical comparison matched and source/target counts are equal.

`elapsed_ms` is not the duration of a hold installed before this call and is not a promise that a
configured maximum pause was met. This primitive does not impose a transport deadline. The executor
must budget all work and handle an unknown engine response as recovery; timing out a client alone
does not prove the server stopped acting.

The ordinary `verify` result retains its live-table meaning: the target contains the observed
source rows. Its counts are sampled at different instants, so equality alone is not a valid live
cutover rule. The frozen operation establishes the additional boundary needed to interpret them.
A mismatch leaves the barriers in place; it does not authorize a threshold of lost or extra rows.

## Native scope and recovery

The [write-fence protocol](write-fences.md) fences INSERTs, including ClickHouse replacement writes.
A qualified workload must use those SDK write paths and restricted runtime roles. Arbitrary DELETE,
UPDATE/mutations, external writes and uncoordinated DDL can change data outside these fences and are
not covered by this comparison guarantee. The current runtime API does not expose those operations.
The final cutover's runtime role revocation/qualification is separate work.

Checking that a CHECK constraint exists is insufficient: an older ClickHouse INSERT can still be
running. This operation calls each native `freeze`, including its drain, even for an already
installed hold. On retry, the drain and comparison run again. A hold retired by release cannot be
reopened. Interrupted ClickHouse DETACH is recovered through the recorded table UUID and the native
fence's `resume` procedure before attempting another comparison.

The post-comparison check detects loss of this hold or a changed identity/project/epoch. It does
not serialize two administrative executors by itself. Use the single durable project executor
and lock required by the native protocol; retain the barriers through the durable activation or
rollback decision.

Shared cases `migration/071`–`078` pin ordering, retry, exact counts plus values, independent epoch
coverage and request identity. A recording native metadata fixture contributes DDL/drain events to
the same call trace as data reads. Live tests independently exercise both SQL engines, different
maintenance generations, released barriers and extra/changed target rows.

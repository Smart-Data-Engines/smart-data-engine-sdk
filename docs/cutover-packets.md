# Signed local cutover packets

`load_cutover_plan` / `loadCutoverPlan` validates the exact authorization for a local migration's
final step. It binds one group, three signed maps, the existing verification request, an approved
query-impact digest, and a finite pause budget. It performs no database operation, reservation,
map activation or watermark adoption. The durable executor and controller reservation workflow
are separate components still under construction.

The caller supplies the locally configured project, logical model and trusted public key or key
set. All three maps and the outer packet must verify against that trust configuration. A customer
can use their own signing keys; loading the packet does not require an account or our service.
Signature key ids remain hints. The returned `verified_with` / `verifiedWith` identifies the actual
key used for the outer packet; parsed maps report their own verifying keys.

## Protocol 1

The JSON object contains exactly these fields:

| Field | Meaning |
|---|---|
| `kind` | `sde-cutover`, separating the document from other signed instructions. |
| `protocol` | 1. Unknown protocols are refused. |
| `plan_id` | Fresh 32-digit lowercase hexadecimal identity. |
| `project_id` | 32-digit lowercase hexadecimal project, independently supplied by the caller. |
| `group` | The one colocation group being moved. |
| `before` | The signed generation-bearing map with the source and exactly one fan-out target. |
| `success` | The signed target-only map, with a higher map version. |
| `abort` | The signed source-only map, with a version higher than success. |
| `verification` | The protocol-1 request for the exact signed before-map, model, group and copies. |
| `query_impact_digest` | 64-digit lowercase SHA-256 identifier of the controller's approved query-impact record. |
| `pause_budget_ms` | Positive safe integer budget for the executor; validation does not enforce elapsed time. |
| `signature` | Ed25519 over canonical bytes excluding this entire block. |

Every signature in a packet uses canonical base64 of exactly 64 bytes. Signature blocks contain
`alg`, `value`, and optionally a string `key_id`. Integral JSON numbers are normalized consistently
before signature verification. Canonical map identifiers follow §7f of the format contract.

The controller must pass the saved-query-impact gate before signing this authorization. The SDK
binds its digest and the exact candidates; it does not rerun private query analysis or infer approval
from missing data. No arbitrary SQL, database endpoint or credential field is part of this protocol.

## Scope and generations

Protocol 1 accepts placement map contract 4 with explicit physical layouts. The migrating group
has one source and one maintained derived copy on different engine bindings. Before-map reads
still use the source. A terminal map contains only the authorized physical copy as its source;
its materialization id can change when derived becomes source, while its engine and layout cannot.
All other groups, generations, routing entries and map attributes are preserved. The migrating
group's explicit read routes disappear so its terminal source is the fallback.

For initial group generation E:

- Maintenance compares source E with target E+1, under the local executor's barriers.
- Abort activates source E+1 and leaves the target closed at no more than E+1.
- Success activates target E+2 and permanently closes the retired source at E+2.

Both spare generations must fit the positive safe-integer range. Separate maintenance and activation
generations prevent a prepared success map from being accepted during target repair. A separate
abort generation prevents its higher version from being accepted before a decision; stronger
source retirement rejects that abort after success. Both outcomes require stale processes to
refresh their maps. A refused old write is not silently retried in another engine.

The actual barriers, grants, deadline handling, final comparison, durable decision and recovery
remain executor responsibilities. Different engine aliases alone do not prove different physical
tables; native identity qualification is required before destructive target repair.

## Loaded provenance

`plan.check_current(current)` / `plan.checkCurrent(current)` requires an immutable loaded, signed
current map with the exact before-map fingerprint. Removing a signature preserves the payload hash
but changes rollback-admission behavior, so an unsigned current map is refused.

Loaded packets and their nested maps are immutable. Python dataclass replacement and TypeScript
copy/construction do not inherit loader provenance. `as_record()` / `asRecord()` returns a fresh
record; changing it or the caller's original JSON cannot alter the authorized candidates.
`candidate_payload('success'|'abort')` / `candidatePayload(...)` returns the signed candidate bytes.
These are prepared operator artifacts, not an instruction to distribute a candidate before the
local executor commits its corresponding decision.

Shared `migration/079`–`098` cases pin the authorized shape and refusal boundaries. Their maps,
requests, digests and OpenSSL signatures are built without importing the SDKs into the generator.


## Acceptance on 13 September 2026

Full suites passed with both native engines available: 935 Python tests, with only the 10 optional
orderbook skips, and 433 TypeScript tests without skips. The shared suite has 201 vector directories
and 214 cases in each runner. Twenty new packet vectors use explicit maps/requests/digests and
OpenSSL signatures without importing an SDK into their generator. Nine per-language tests cover
input/output snapshots, loader provenance, signed current-map mode, and canonical representation.

All thirty intentional mutations were detected by named behavioral failures, including signature,
project/request/current-map checks, version and generation order, unrelated routing/groups,
physical layout, extra copies, base64 padding and copied-plan provenance. Exact sources were
restored and both 223-case selected suites passed. Wheel/sdist/npm archives passed inspection;
fresh wheel/npm consumers validated all twenty packet cases and decoded the signed candidates
without importing the source checkout. No package was published or deployment performed.

This acceptance covers the authorization packet. It does not claim durable executor, grant
revocation, budget enforcement, controller reservation, activation, or recovery is implemented.

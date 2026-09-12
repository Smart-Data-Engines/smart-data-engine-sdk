# @smart-data-engines/sde — TypeScript client library

Declare your data model. We decide which database engine each part of it lives in, how it is laid out
there, and when it should move.

```ts
import { entity, ref, buildModel, T } from '@smart-data-engines/sde'

const User = entity('User', {
  fields: { id: T.uuid, email: T.string },
  pii: ['email'],
})

const Order = entity('Order', {
  fields: { id: T.uuid, total: T.decimal(12, 2), placedAt: T.timestamptz },
  relations: { user: ref('User') },
  residency: 'EU',
})

const Event = entity('Event', {
  fields: { id: T.uuid, name: T.string, at: T.timestamptz },
})

const model = buildModel([User, Order, Event])
```

`User` and `Order` are related, so they are placed together. `Event` is joined to nothing, so it is
free to go somewhere built for it. That split is the decision most applications get wrong once, at the
start, and never revisit.

## Status: Tier 2 for PostgreSQL and ClickHouse, plus hashed identifiers

| Tier | What it covers | Here? |
|---|---|---|
| 0 | model, canonical IR and version, groups, shapes and ids, map parsing and refusals, routing | **yes** |
| 1 | telemetry: measurement, window aggregation, local buffering | **yes** |
| 2 | schema creation and migration participation | **yes**, for `postgres` and `clickhouse` |
| 3 | framework integrations, pooling | not yet |
| — | hashed identifiers (§2a), a mode rather than a tier | **yes** |

The shared vectors for Tier 1 and Tier 2 were written **before** this library claimed those tiers,
which is what §10 of the format contract says has to happen and is worth knowing about the claim:
`telemetry/`, `schema/` and `migration/` exist because of it. Closing that gap found five defects in
the reference implementation, which had been claiming those tiers with nothing shared to check them.

### Where the two libraries differ, and why

Two differences, both deliberate.

**Tier 2 here is asynchronous.** Node's I/O is asynchronous, so a synchronous wrapper around a driver
means blocking the event loop - a worse thing to do to your process than a `Promise` in a signature.
The split follows the tiers exactly: everything up to and including telemetry touches no socket and
stays synchronous, and a `Session` is *opened* rather than constructed, because the forward-only map
check reads a table and a constructor cannot await. You therefore cannot hold a session whose check
has not run.

**The ClickHouse adapter has no driver dependency.** There is an official Node client and it requires
Node 20 or newer; this package supports Node 18 to 22 and its CI runs all three. Dropping Node 18
would narrow a published claim to gain a dependency, and pinning a superseded version of the client
is the conflict the zero-dependency rule exists to avoid - so neither. ClickHouse's HTTP interface
needs no client, and this adapter therefore owns both of its timeouts instead of inheriting a
driver's defaults, which is how the reference implementation discovered that its ClickHouse connect
timeout never fired.

`pg` is an **optional peer dependency**: install it if you place a group in PostgreSQL, and keep the
version you already chose. Importing this library resolves neither driver, and a test over the import
closure of `src/index.ts` pins that - which is what makes the no-account mode free and
`docs/observability.md` true.

`hashIdentifiers` is listed separately because hashing is orthogonal to the tiers - a complete Tier 0
library may omit it. What is not optional is agreement: run two services in two languages against one
model, and if they derive different digests they compute different `model_version` values and each
refuses the other's placement map. This library passes `conformance/vectors/hashing/`, which is the
only claim worth making about it.

## Why the model is declared rather than inferred

TypeScript's types are erased before the code runs, so a library cannot read them the way the Python
one reads annotations. The model is stated as values instead.

That is not a workaround, and it is the reason this was a good second implementation to write. Anything
the format contract had left implicit - anything that was really "whatever Python's introspection
produces" - had nowhere to hide here, because nothing is introspected.

It found one immediately, and it was not small. JavaScript compares strings by UTF-16 code unit; the
contract requires code point order. For anything in the Basic Multilingual Plane the two agree, so no
test written with Latin or CJK identifiers can see the difference. Above U+FFFF they diverge: an astral
character is a surrogate pair starting at 0xD800, so `Array.prototype.sort` places every emoji *before*
U+E000 while code point order places it after. One such field name would have hashed differently in
Python and TypeScript, the control plane would have seen two models where there was one, and nothing
would have failed until half a fleet was writing to the wrong tables.

Hence `compareCodePoints` in `src/canonical.ts`, and two vectors that make sure nobody replaces it
with `.sort()`.

## Conformance

```bash
npm install
npx vitest run

# with both engines, which is what the Tier 2 slices need
export SDE_POSTGRES_DSN=postgresql://postgres:sde@127.0.0.1:55432/sde
export SDE_CLICKHOUSE_DSN=clickhouse://default:sde@127.0.0.1:58123/sde
npx vitest run
```

The vectors live in `../conformance` and are shared with every other language. A divergence is a red
test for whoever caused it, rather than an operation written to the wrong engine in production.

If you are porting this to another language, read `../docs/format-contract.md` first. It is meant to be
sufficient on its own; if it is not, that is a bug in the document and worth reporting as one.

## Licence

Apache-2.0.

## Timestamp values

Both adapters return `Timestamp`, an immutable value with microsecond precision, for `timestamp`
and `timestamptz`. `Date` is still accepted on writes. Use `Timestamp.from(isoText)` for six-digit
inputs, `.toISOString()` for text and `.epochMicroseconds` for exact comparison; `.toDate()` refuses
if converting would lose digits. See [exact timestamps](../docs/timestamps.md) for examples and
migration from the previous `Date` return type.

## Bound migration verification

Configure `Session.open(..., { projectId })` from your local enrollment manifest. Load the
controller's request with `VerificationRequest.fromRecord` and call `verify(session, group,
{ request })`; send `verifyRecord(report)` back. The complete request is checked before comparison,
and row-level differences remain local. See [the request protocol](../docs/format-contract.md#7b-verification-requests-and-bound-reports).
Loaded placement maps are immutable snapshots. Load a new document rather than modifying a layout
or copying an object with its old fingerprint.

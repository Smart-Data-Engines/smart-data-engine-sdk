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

## Status: Tier 0

| Tier | What it covers | Here? |
|---|---|---|
| 0 | model, canonical IR and version, groups, shapes and ids, map parsing and refusals, routing | **yes** |
| 1 | telemetry: measurement, window aggregation, local buffering | not yet |
| 2 | schema creation and migration participation | not yet |
| 3 | framework integrations, pooling | not yet |

Tier 0 means this library can declare a model, agree with every other SDE library about what that
model *is*, load a placement map and tell you where an operation goes. It cannot yet talk to a
database. The Python library is further along; see the repository root for what each one reaches.

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
```

The vectors live in `../conformance` and are shared with every other language. A divergence is a red
test for whoever caused it, rather than an operation written to the wrong engine in production.

If you are porting this to another language, read `../docs/format-contract.md` first. It is meant to be
sufficient on its own; if it is not, that is a bug in the document and worth reporting as one.

## Licence

Apache-2.0.

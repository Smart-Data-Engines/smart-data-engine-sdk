# The format contract

Contract version: **1** (see `conformance/contract-version.txt`)

This document is meant to be sufficient to write a new SDE library from, in any language, without
asking us anything. If it is not, that is a bug in this document and worth reporting as one.

Everything here is pinned by `conformance/vectors`, and
`conformance/vectors/model/001-single-entity` was written by hand from these rules rather than
generated from an implementation — so it is the one vector that proves this document says enough.

## Why this exists

Four libraries computing the same model version is not a nice property, it is the difference between
a working product and a broken one. If Python computes `model_version` differently from Java, the
same declared model arrives at the control plane as two models, gets two placements, and the two
halves of a client's fleet write to two different sets of tables. Nothing about that fails at compile
time and nothing about it looks wrong in a log.

So the parts that have to agree are specified at the byte level, and the parts that do not are left
to each language's taste.

## 1. Canonical encoding

The canonical form of any structure is JSON, restricted so that exactly one byte string is possible
for a given value.

1. **UTF-8, no byte order mark.**
2. **Object keys are NFC-normalised, then sorted by Unicode code point.** In that order. Sorting
   first would place `e` + U+0301 under `e` and the composed `é` at U+00E9, which are different
   positions for what must be the same key.

   **By code point, which is not the same as your language's default string comparison.** JavaScript,
   Java and C# all compare strings by UTF-16 code unit. For anything in the Basic Multilingual Plane
   the two orders agree, so the difference is invisible in any test written with Latin or even CJK
   identifiers. Above U+FFFF they diverge: an astral character is a surrogate pair starting at
   0xD800, so UTF-16 order places every emoji and every CJK extension character *before* U+E000
   while code point order places them after. One such field name would hash differently in two
   libraries, the control plane would see two models, and nothing would fail until half a fleet was
   writing to the wrong tables. This was found by writing the second implementation, which is the
   argument for writing it early: `conformance/vectors/canonical/001-key-order-by-code-point` and
   `model/004-astral-identifier` exist so it cannot come back.
3. **No insignificant whitespace.** `{"a":1,"b":[2,3]}`. No space after `:` or `,`, no newlines, no
   trailing newline.
4. **Every string value is NFC-normalised.**
5. **Escaping is minimal and exhaustive.** Escape only:
   - `"` as `\"`
   - `\` as `\\`
   - U+0008 `\b`, U+0009 `\t`, U+000A `\n`, U+000C `\f`, U+000D `\r`
   - any other code point below U+0020 as `\u00xx`, lowercase hex
   Everything else is emitted as raw UTF-8. In particular **do not** escape non-ASCII characters,
   `/`, U+2028 or U+2029. Several widely used JSON writers do escape some of those; any of them would
   change the hash.
6. **No floating point values.** Integers are JSON numbers; anything fractional is a decimal string.
   A language whose JSON writer emits `1.0` for an integral float must not be used as-is.
   *Note the distinction:* a field may have type `float64`. The IR records the type's *name*, which is
   a string. There is no float literal anywhere in the encoding.
7. **Integers are the shortest decimal form.** No `+`, no exponent, no leading zeros. A language with
   64-bit integers must reject a value it cannot represent rather than truncate it.
8. **Object keys are unique after normalisation.** Two keys that differ only in composition are the
   same key, and a structure containing both is an error rather than a last-one-wins.

Arrays are not this layer's concern: whoever builds the structure sorts where order carries no
meaning, and records an explicit index where it does.

## 2. Identifiers

```
model_version = lowercase_hex(sha256(canonical_bytes(ir)))[:16]
shape.id      = lowercase_hex(sha256(canonical_bytes(shape)))[:16]
```

Sixteen hex characters, which is the first eight bytes of the digest.

## 3. The neutral type vocabulary

No language's own type names reach the IR. Each library maps its host language onto this closed set:

```
bool
int32  int64
float32  float64
decimal(p,s)
string  bytes  uuid
date  timestamp  timestamptz
json
```

`decimal` is written `decimal(12,2)` — precision, comma, scale, **no spaces**. Precision and scale are
both required: a decimal without them is not a storable type in any engine we place data in, and
letting the engine choose would make the physical schema depend on something the model never said.

`timestamp` has no zone; `timestamptz` has one.

A host type with no mapping is an error. Do not guess. If a language has an obvious-looking type that
maps ambiguously — Python's `datetime`, which may or may not carry a zone — pick a default, document
it in that library's own documentation, and provide an explicit way to ask for the other one. The
Python library maps `datetime` to `timestamptz` and offers `sde.Timestamp` for the naive form.

## 4. The IR

```jsonc
{
  "contract": 1,
  "entities": [                     // sorted by name
    {
      "name": "Event",
      "fields": [                   // sorted by name
        {"name": "at", "nullable": false, "type": "timestamptz"}
      ],
      "key": [                      // order matters, so it is explicit
        {"field": "tenant", "position": 0},
        {"field": "id", "position": 1}
      ],
      "pii": [],                    // sorted; always present, possibly empty
      "residency": null             // string or null
    }
  ],
  "relations": [                    // sorted by (from, name, to)
    {"from": "Order", "name": "user", "to": "User"}
  ],
  "atomic": [["Order", "Payment"]], // each group sorted; the list of groups sorted
  "cost_ceiling": null              // or {"amount": "500.00", "currency": "EUR"}
}
```

Two rules worth restating because they are the ones an implementer gets wrong:

**Nothing depends on declaration order.** The order a client happened to write their entities in is
not part of their model, so it must not reach the hash.

**Composite key order is recorded, not implied.** `(tenant, id)` and `(id, tenant)` are different
keys and different models. Recording that as an explicit `position` rather than as array order means
a reader never has to know which arrays in this document are load-bearing.

`atomic` is merged and transitive before it is written. `atomic_with` is symmetric even when declared
on one side, and if A is atomic with B and B with C then all three commit together — there is nothing
else a single engine's transaction could deliver.

## 5. Colocation groups

A group is a connected component of the graph whose vertices are entities and whose edges are:

- every relation, in either direction
- every declared atomicity

The group's name is its alphabetically first member. Groups are returned sorted by name.

Group identity is only meaningful within one model version. Changing membership changes the model,
which changes its version.

## 6. Operation shapes

```jsonc
{
  "group": "Event",
  "kind": "point_read",
  "entity": "Event",
  "fields": ["id"],       // sorted
  "target": null          // the other entity, for relation_walk; null otherwise
}
```

`kind` is one of `point_read`, `range_read`, `aggregate`, `full_scan`, `relation_walk`, `write`,
`bulk_write`.

Enumeration, per entity: one `point_read` on the key, one `write`, one `bulk_write`, one `full_scan`,
one `aggregate`, and one `range_read` per field whose type is ordered — `int32`, `int64`, `float32`,
`float64`, `decimal(...)`, `date`, `timestamp`, `timestamptz`. Per relation: one `relation_walk` from
the source entity, with the relation name in `fields` and the target in `target`.

Ranges over `string` and `uuid` are not enumerated. They are legal in every engine and almost never
what anybody means.

Shapes are returned sorted by `(group, entity, kind, fields, target)` — by that tuple rather than by
identifier, so that a human reading a placement map sees related shapes together instead of scattered
by hash.

**A shape never contains a value.** It is assembled from the structure of an operation and never sees
the arguments, which is what makes telemetry safe by construction rather than by redaction.

## 7. The placement map

```jsonc
{
  "contract": 1,
  "model_version": "54a50916f4326096",
  "map_version": 1,
  "groups": {
    "Event": {
      "source": {
        "id": "Event@pg",
        "engine": "pg-main",
        "layout": {"auto": true}
      },
      "derived": [
        {
          "id": "Event@ch",
          "engine": "ch-1",
          "layout": {"tables": {"Event": "event"}, "columns": {}},
          "lag_budget_ms": 30000
        }
      ]
    }
  },
  "routing": {"<shape id>": "<materialisation id>"},
  "signature": {"alg": "ed25519", "key_id": "k1", "value": "<base64>"}
}
```

Rules a library must enforce, all of them refusals rather than warnings, because this document decides
where data is written:

- `contract` must equal the version the library implements. Not "at least" — equal.
- `model_version` must equal the version of the declared model. A mismatch is refused, never
  reconciled.
- Exactly one `source` per group. It must **not** carry `lag_budget_ms`: the source is where writes
  land, so it is not behind anything.
- Every `derived` materialisation **must** carry `lag_budget_ms`. Without it nobody can tell a healthy
  copy from one that is hours behind.
- Materialisation ids are unique within a group.
- Every group in the model must be placed.
- `layout` is either explicit or `{"auto": true}`, never both.
- `routing` is optional. A shape with no entry routes to the source.

### Signing

The signature is Ed25519 over `canonical_bytes(map without the "signature" key)`.

Three cases, and the middle one is the one to get right:

| Map | Key available | Result |
|---|---|---|
| unsigned | — | **accepted**: this is the no-account mode |
| signed | yes | verify; refuse on failure |
| signed | no | **refused** |

An unsigned map is valid. Hand-write one, point a library at it, and everything works with no key, no
account and no network — that is a supported mode, not a loophole, and it is the honest answer to a
client asking what happens if they stop paying us. What is refused is a map that *claims* to come from
us, by carrying a signature, when there is no key to check the claim against. An unverifiable claim is
worse than no claim.

Publishing the library does not weaken any of this. The public key is in the library, the private key
is in the control plane, and a signature was never a secret.

## 8. Routing

Three conditions, then a lookup:

1. `write` and `bulk_write` go to the source. Always, before anything else is consulted.
2. An operation inside a transaction that has already written goes to the source.
3. An operation that asked for no staleness goes to the source.
4. Otherwise: `routing[shape.id]`, and the source if there is no entry.

Conditions 2 and 3 are correctness rather than policy — a derived copy is behind by design, so it
cannot show a write the caller just made. Everything else is a lookup, which is the point: decisions
need telemetry, a cost model and an explanation, and reimplementing that judgement four times and
keeping the four identical forever is not a plan.

## 9. Capability tiers

An implementation declares which tier it reaches, and "supported" has to mean the same thing across
languages or the word is worthless.

| Tier | What it covers | Vectors |
|---|---|---|
| 0 | model → IR → version; shape enumeration and ids; map parsing, signature, refusals; routing; error semantics | `model/`, `routing/`, `errors/` |
| 1 | telemetry: measurement, window aggregation, local buffering | `telemetry/` |
| 2 | schema creation, and participation in migration (dual write) | `schema/`, `migration/` |
| 3 | ergonomics: framework integrations, async variants, pooling | — |

Tier 0 is not optional. An implementation that does not pass the Tier 0 vectors is not an SDE
library, whoever wrote it.

## 10. Running the vectors

There are four kinds:

| Kind | What it pins |
|---|---|
| `model/` | a neutral declaration, and the exact IR bytes, version, groups and shapes it must produce |
| `routing/` | a map plus cases: `(shape, in a write transaction?, needs freshness?)` to materialisation |
| `errors/` | which error, and at what stage it must be raised |
| `canonical/` | a value fed straight to the encoder, and the exact bytes |

`canonical/` is the newest and the most instructive. It exists because a mutation that should have
failed did not: every object key in the model IR is fixed ASCII, so no model vector reaches the
object-key comparator, and swapping code point ordering for a naive sort passed the entire suite.
Field names do reach the IR - as array elements, through a different comparator. Two call sites, one
covered, and the gap was invisible until somebody deliberately broke the code to see what noticed.

Every expectation under `canonical/` is written by hand from this document, which also makes those
vectors the check on whether this document is complete.

Each library reads `conformance/vectors/**` in its own test runner. Four things matter:

- **Compare `ir.json` as bytes.** Parsing it first and comparing structures would pass two libraries
  that agree on the structure and disagree on key order or normalisation, which is exactly the failure
  these vectors exist to catch.
- **Build the model from `model.json`.** Every library needs a small loader for the neutral form. It is
  a requirement, not a convenience: without it the vectors could not be shared, and unshared vectors
  verify nothing.
- **Check the stage of an error, not only its type.** A library that raises the right error when a
  query runs, rather than when the model is built, has a different bug that a type-only assertion
  cannot see.
- **Fail loudly if you ran zero vectors.** A green suite that found no files is worse than a red one.
- **Break your own code and check that this suite notices.** A vector that passes without reaching
  the code it describes takes the place of one that would have. This is not general advice; it is how
  the `canonical/` vectors came to exist.

## 11. Changing this document

A vector is frozen once committed. Changing one is changing the contract: bump
`conformance/contract-version.txt`, and every library declares which version it implements. There is
no quiet fix — a vector that was wrong was a contract that was wrong, and somebody may have a stored
placement map that depends on it.

If your library cannot reproduce a byte this document requires, the first hypothesis should be that
**this document is wrong** — that it wrote down what one language happens to do rather than something
language-neutral. That has already happened once: the rule "no floating point anywhere" conflated the
encoding with the type system, and had to be split into "no float literals in the encoding" and
"`float64` is a perfectly good field type".

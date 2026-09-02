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

## 2a. Hashed identifiers

A client may replace every identifier in their model with a keyed digest, so that we never see that
they have an entity called `patient_diagnosis`. The salt stays on their infrastructure and is never
serialised into anything — not a model, not a telemetry window, not a support attachment.

Hashing is optional. Its **bytes are not**: a client can run one service on the Python library and
another on the TypeScript one against the same model, and if the two derive different digests they
compute different `model_version` values and each refuses the other's placement map. So a library that
offers hashing at all must derive it exactly like this:

```
digest(salt, parts)  = lowercase_hex(hmac_sha256(salt, join(nfc(parts), U+0000)))[:12]

entity   E           -> "e_" + digest(salt, [E])
field    F of E      -> "f_" + digest(salt, [E, F])
relation R on E      -> "r_" + digest(salt, [E, R])
```

Five details, each of which a port can get wrong while every ASCII test still passes:

- **NFC before the HMAC, not after.** §1 normalises before emitting bytes, which is why two libraries
  that declare `Zamówienie` in different normal forms agree on the model version. Hashing the raw name
  throws that guarantee away and nothing in an English-only test suite notices.
- **U+0000 as the separator, not concatenation.** `("User", "id")` joined without one collides with
  `("Use", "rid")`. Unlikely is not a guarantee when the consequence is two fields sharing one column.
- **The prefix is outside the HMAC.** It labels a digest for whoever reads a table name; it is not part
  of the message. A field of `A` named `B` and a relation on `A` named `B` therefore share a digest and
  differ only by prefix, which is intended.
- **Twelve hex characters**, i.e. 48 bits — half the length of a `model_version`, because these appear
  in table names. A collision would merge two entities into one, so it is **checked and refused**, never
  merged.
- **Fields and relations are hashed with their entity.** The same field name on two entities must give
  two digests, or we learn that both have a field called `email` without being able to read the name —
  the structural leak hashing exists to close.

A seventh detail, for a port rather than for the arithmetic: **the container holding the name map
must not reserve any identifier.** A JavaScript object literal inherits `Object.prototype`, so
`map["__proto__"] = digest` is silently a no-op and reading it back yields the prototype — and
`map[source] ?? {}` returns `Object.prototype` rather than falling through, so the next write pollutes
every object in the process. Our TypeScript library had exactly that while Python, whose `dict` has no
reserved keys, was correct: one model, two languages, a correct answer in one and a corrupt one in the
other. `conformance/vectors/hashing/003-reserved-object-keys` pins the digests for `__proto__`,
`constructor`, `toString` and `valueOf` so no port can lose it. Whatever a language's equivalent hazard
is — a case-insensitive map, a map that forbids an empty key, a name colliding with a built-in — that
vector is what finds it.

A sixth detail, added after it went wrong here rather than in advance: **the salt is a byte string,
and where a library reads it from a file it reads the file verbatim.** No trimming, no whitespace
handling, no text decoding. This is part of the contract and not an implementation choice, because a
Python service and a Node service in one deployment share one salt file and must derive the same
digests from it. Our Python library called `.strip()` on the file, "to tolerate a trailing newline",
which removes six byte *values* — space, tab, LF, CR, `U+000B`, `U+000C` — so a random 32-byte salt
was silently shortened about 5% of the time. The process that generated the salt then used all 32
bytes and every later process used the remainder: one client, one declared model, **two model
versions**. A library that wants to accept a hand-written salt has to define an encoding for the file
(hex or base64) and say so; it must not guess by stripping.

The contract version is **not** bumped for this. Nothing in the derivation above changed; the rule was
always implied by "the salt", and the fix makes one implementation produce the answer the document
already specified. The consequence for an affected client is worth saying plainly all the same: if your
salt happened to begin or end with one of those six bytes, your `model_version` changes when you
upgrade, and you need a new map — the same thing that happens when a salt is replaced, and for the same
reason.

What is **not** hashed, and why:

| Not hashed | Because |
|---|---|
| `residency` | a jurisdiction, and a hard placement constraint; a hashed constraint is unenforceable |
| `cost_ceiling` | a number and a currency |
| types, keys, nullability | the physical schema is derived from them, and none of them is a name |

Two consequences a client has to be told rather than left to discover:

- **Hashing is a model change, not a setting.** A group is named after its alphabetically first member
  and a shape id includes group and entity names, so the IR differs, the version differs, and the
  existing map is refused. Switching hashing on requires a new map.
- **The client's own tables get opaque names** like `e_9c1f2a7b3d40`, because the physical layout comes
  from a map keyed by hashed names. For a regulated deployment that is the point; elsewhere it is a real
  cost, which is why this is off unless asked for.

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

### 3.1 The other direction: an engine need not map every neutral type

The vocabulary above is what a *model* may say. What an engine can store is a separate question, and
the two are allowed to differ — a neutral type an engine cannot hold **faithfully** must be left
unmapped rather than approximated.

The rule is one sentence: **an engine adapter that cannot round-trip a neutral type does not map it,
and the effect is a placement constraint rather than a lossy column.** Deriving a layout raises, the
planner therefore cannot put a group containing such a field in that engine, and the refusal happens
where a map is built rather than where a value is read.

Two live examples, and the point of naming them here is that both look like they work:

- **`bytes` in ClickHouse.** A `String` column stores the bytes correctly — `hex()` and `length()` on
  the server confirm it. The *read* is what fails: the driver decodes the column to text, cannot decode
  bytes that are not valid UTF-8, and returns their hex representation as a string. Nothing
  distinguishes a binary `String` column from a text one on the way back, so no adapter can correct it.
- **`json` in ClickHouse.** PostgreSQL returns a parsed object; a ClickHouse `String` returns the
  original text. The field would change type in the host language when its group moved, which is the
  one thing a placement change must never do.

Both are unmapped today, and that costs real capability — an event payload in a column store is a
natural thing to want. It costs less than a client discovering after a migration that a checksum no
longer matches, or that a field is now a string. The way out of either is a decision about what the
neutral type promises on the way *back*, made once and implemented in every adapter together.

**A library's tier is per engine, and "supported" means round-trips.** A library that maps a type by
storing something adjacent to it has not implemented that type.

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

Three rules worth restating because they are the ones an implementer gets wrong:

**Nothing depends on declaration order.** The order a client happened to write their entities in is
not part of their model, so it must not reach the hash.

**Sort where the IR is built, not where it is called from.** "Sorted by name" above means sorted by the
name that appears *in the IR*, and the sorting belongs to whatever constructs these bytes — not to the
caller who happens to hand it a sorted list. The distinction is invisible until a second caller shows
up. Ours was hashing (§2a): it rebuilds each entity with digests for names while keeping the original
sequence, so one library's field arrays came out ordered by the *real* names. Alphabetical order of
hidden names is a small amount of exactly what hashing hides, and no library that had never seen those
names could reproduce the bytes. One implementation sorted inside the constructor and one trusted its
callers; both passed their own suites for months, and the shared vector is what made them disagree out
loud.

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
  "contract": 2,
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
      ],
      "also_write": ["Event@ch"]   // optional; absent for every map that is not mid-migration
    }
  },
  "routing": {"<shape id>": "<materialisation id>"},
  "signature": {"alg": "ed25519", "key_id": "k1", "value": "<base64>"}
}
```

**`contract` here is the map's own version, and it is not the IR's.** The IR carries a `contract`
too and it is still `1`: nothing about the IR changed when the map gained `also_write`. One counter
for two artefacts means every artefact's version moves when any one of them changes - and because
`contract` is *inside* the IR and `model_version` is a digest of the IR, bumping one number for a
key in a different document would give every client a new model version, invalidate every issued map
and re-bless every model vector. The evidence that the split is right is `model/001-single-entity`,
the hand-written vector whose digest CI pins: adding `also_write` does not move it.

A library reads **`MAP_CONTRACT_FLOOR` through `MAP_CONTRACT`**, which today is 1 through 2.
Backwards compatible, forwards strict, and the asymmetry is knowledge rather than kindness: every
contract-1 document is a valid contract-2 one with a key absent, which reads as "no dual write" -
a complete meaning. What came *after* a library cannot be known, so a higher number is refused
rather than interpreted. Strict equality, which is what contract 1 required, coupled a library
upgrade to a control-plane action - and in the no-account mode there is nobody to issue a new map,
so it would have broken the promise that hand-writing one is enough.

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
- **No group may be placed that the model does not have.** The converse of the rule above, and it
  needs saying separately: checking one direction reads as checking both. Python fell through to a
  lookup on the model's groups and raised a bare `KeyError`; TypeScript accepted the map in silence.
  One missing rule, two languages, two different wrong answers.
- `layout` is either explicit or `{"auto": true}`, never both.
- **`{"auto": true}` derives a PostgreSQL layout, in every implementation.** A layout carries no
  dialect and a materialisation names its engine by *name* rather than by dialect — deliberately,
  since reasoning about an engine from its name is what these libraries refuse everywhere — so there
  is nothing in the document from which the right dialect could be derived. A hand-written map for
  another engine needs an explicit layout. Both non-PostgreSQL engines refuse a PostgreSQL-derived
  layout rather than applying it, so this fails closed; the message names a symptom, not the cause.
  Making `auto` dialect-aware means a new key in a signed document, which by §11 is a loosening and
  therefore a contract bump in every language at once.
- **An engine may impose its own schema rather than accepting one.** The orderbook engine stores L2
  depth in a shape fixed in its own source, so there is no DDL to send it and the relationship
  inverts: a group either *is* that shape or it cannot be placed there. Nothing in this document
  changes for such an engine — a layout for it is an ordinary explicit layout, with the engine's own
  table name and the engine's own column names — and that is the point. An implementation that only
  reads maps needs no knowledge of it. One that applies them needs to know that rendering the DDL
  for such a layout yields **no statements**, and that "no statements" there means "nothing to run"
  rather than "no tables in this layout". The Python library separates the two with
  `schema_is_fixed(dialect)`; an implementation may spell it differently, since it never reaches a
  byte of the format.
- `routing` is optional. A shape with no entry routes to the source.
- **A derived materialisation carries an explicit layout, never `{"auto": true}`.** `auto` derives the
  *normalised* layout, which is the source's shape - so a derived copy asking for it would be a second
  copy of the source in another engine, paying for storage and lag to answer questions the source
  already answers. What a derived materialisation is *for* is a different physical shape, and the
  Python library derives one with `denormalized_layout()`: one wide table at the root entity's grain,
  with intra-group relations flattened in under `<relation>_<field>` - the same convention the foreign
  key uses, so a client reading their own analytical table knows where a column came from.
- **A field declared as personal data is excluded from that wide table unless explicitly allowed for
  that field.** Requirement 5.5, and the rule lives in the derivation rather than in whatever builds
  the map: an analytical materialisation is a second copy in a different engine with different access
  controls, so "your personal data is not copied there" is worth being able to *read* in the library
  rather than take on trust. It fails closed - an empty allowance excludes every declared field, on
  the root as well as on the inlined side - and reports what it left out, because an analyst finding
  no `email` column has to be able to tell a decision from missing data. An implementation may spell
  the allowance differently; nothing about it reaches a byte of this format, since the map carries
  only the resulting layout.
- **Every `routing` value must name a materialisation of the shape's own group**, and every key must
  be a shape this model produces. Checked when the map is loaded, not when the shape is first
  routed — see §8.

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

### Forward only: a signed map is never accepted below one already applied

A signature says a document is authentic. It says nothing about whether it is current, and it cannot:
a signed map for version 3 verifies correctly forever. So replacing a client's map file with an older
signed one loads cleanly, routes writes to the previous placement, and **nothing protests**. Today
that costs a stale schema. Once migration state travels in the map, it costs writes - a library
reverted from dual-write to single-write mid-migration drops exactly the rows the migration exists
not to drop.

Refusing it needs the one thing this library otherwise does not have: **memory**. It lives in the
client's own engines, in a reserved table:

| Name | `sde_map_state` |
|---|---|
| Columns | `map_version` (int64), `model_version` (string), `seen_at` (timestamp, engine default) |
| Writes | **append-only**; the watermark is `max(map_version)` |
| Created | on first read, by the adapter, if it is missing |

`sde_map_state` is **reserved**: a layout naming it is refused when the map is loaded, in every
language. That refusal is a parsing rule, so it belongs here rather than in an adapter - a rule that
holds in one runtime and not the other is one map with two meanings, which is what this document
exists to prevent. Vector: `errors/019-layout-names-the-reserved-bookkeeping-table`.

There is now a second reserved name, and the pair is why the refusal is written over a table of them
rather than over one string:

| Name | `sde_backfill_state` |
|---|---|
| Columns | `materialization` (string), `entity` (string), `rows_copied` (int64), `at` (timestamp, engine default) |
| Writes | **append-only**; the marker is `max(rows_copied)` per (`materialization`, `entity`) |
| Created | on first read, by the adapter, if it is missing |
| Scope | one row count per fan-out target per entity - see the migration section below |

Vector: `errors/025-layout-names-the-reserved-backfill-table`. A reserved name a client's model can
collide with is a collision that fails silently in the direction that matters: their rows read as
progress, our progress written into their table. Both reservations are tightenings and neither is a
version bump.

Four rules, and the first two are what make the mechanism safe rather than merely present:

- **append-only, watermark is `max()`.** No update, no key to enforce, no row to contend over - and
  therefore identical semantics in an engine with a primary key and in one without, which is the
  engine this design was shaped by. A stale row can never lower the bar;
- **every participating engine is written, and the watermark is the maximum over all of them.**
  Losing an engine cannot lose the protection and a lagging one cannot weaken it;
- **an engine that cannot keep the bookkeeping does not take part, and that is reported.** An engine
  whose schema is fixed in its own source has nowhere to put a table. A client whose only engine is
  one of those has no rollback protection and cannot have any; the honest maximum is to say so where
  it can be read, which is why the check's state is public rather than internal;
- **only signed maps are checked.** An unsigned map is the client's own document, and replacing it
  with another is the no-account mode working as documented. In that mode this mechanism does
  nothing at all: no table, no query, no cost.

Equal is accepted - restarting a process against the same map is the ordinary case - and only
strictly lower is refused. The escape for a legitimate rollback is to clear the bookkeeping, and the
refusal says so; it is deliberately not a parameter, because a parameter named `allow_rollback` is
set once during an incident and left set.

**Not part of the byte contract, and the distinction matters.** Nothing above changes an encoding or
a document, so there is no version bump: the reservation is a *tightening* of a refusal (section 11),
and the bookkeeping is behaviour in a runtime that has engine adapters. A runtime with none - the
TypeScript one today - implements the reservation and nothing else, which is the whole of what a
Tier 0 library can do here.

### The backfill marker: a row count, and never a key

The `also_write` key below says where a copy goes. What says how much of it has arrived is one
integer per (fan-out target, entity), and the choice of an integer over "the last key copied" is the
one decision in a migration a future language port must not get wrong.

A key marker resumes exactly and needs a **codec**: every type a key can be has to survive a round
trip through whatever column the marker table has, in every language that grows an adapter. The
failure mode of a lossy round trip is a resume point *past* rows that were never copied, which is
silent data loss - and it would be data loss that one language has and another does not, which is
precisely the divergence this document exists to prevent. A row count has no codec and cannot fail
that way: resuming means asking the source for the key of row *N*, and if rows have been inserted
below that point since, row *N* is now earlier than it was, so the backfill redoes work. Every error
in that derivation points at recopying.

Recopying is free because two further rules hold each other up:

- **the chunk is written before the marker moves.** A crash between them costs a recopy; the other
  order costs the chunk, permanently;
- **the copy is idempotent, using the target's own key semantics.** `ON CONFLICT DO NOTHING` where
  there is a primary key, a `ReplacingMergeTree` collapsing under `FINAL` where there is not. An
  engine that can offer neither cannot be a fan-out target, and that is a named refusal rather than
  a silent skip.

Two consequences a port should not have to rediscover. The marker is **never a key value**, so it
never appears in a log line either - a log is the last place a client's own data should turn up. And
a source with fewer rows than the marker claims were copied **refuses**: rows left the source outside
the library, so the marker describes a table that no longer exists.

**A limitation, stated rather than left to be found.** The watermark is per engine, and this format
has no field naming which stream of maps a document belongs to. An engine shared by two independent
map streams would therefore have the higher one refusing the lower. The fix is a separate database
per stream, which a shared engine wants anyway; inventing a stream identifier would mean a new key in
a signed document, which is a loosening and so a bump in every language at once.

### `also_write`: where a write goes, additionally

Optional, a non-empty list of materialisation ids within the group, absent when there is no fan-out.
This is how a **migration** reaches a library, and the reason no phase name appears anywhere in a
map.

A library does not need to know what `DUAL_WRITE` means. It needs to know where writes go and where
reads go, and both of those were already things a map says - so a migration is a map with one more
key, and the library starts writing to two engines because it was handed a new document rather than
because something called it. Putting the phase in the map as well would be a second representation
of a fact the fan-out and the routing table already carry, and a signed document with two
representations of one fact is one that can contradict itself.

Four refusals, validated **when the map loads** rather than at the first write. A map is handed over
once and obeyed for months, so a defect in it belongs at arrival and not at the request that happens
to touch it - and during a migration that request is a write, whose failure mode is a row that goes
to one engine when the document says two.

| Shape | Refused because | Vector |
|---|---|---|
| names the source | the source is where writes already land; listing it either writes the row twice or reads as though the source were optional | `errors/020` |
| names an id that is not a derived copy of the group | a fan-out target that does not exist is a write with nowhere to go, and during a migration that is a row the copy never receives | `errors/021` |
| an empty list | absent means "writes go to the source alone"; empty would claim fan-out was considered and none chosen, which is a stronger thing to say | `errors/022` |
| names the same copy twice | a duplicated row, or a document nobody meant to write | `errors/023` |
| present in a map declaring contract 1 | a key from a later contract in an earlier document. Refused because of *who* makes this mistake: a producer that grew the key and forgot to raise the number | `errors/024` |

That last one is a **tightening**, so no bump, and it is worth its own line because it caught a real
producer: the control plane emitted the IR's contract number into the map, so its first dual-write
map declared 1 and carried the key. The library refused it, which is the only reason this paragraph
is not a bug report.

Two properties the format does **not** state and a runtime must: a write to an `also_write` copy is
**additional and never authoritative**, and its failure does not interrupt the caller. That is
behaviour rather than encoding, so it lives with the runtimes that have engine adapters; a Tier 0
runtime implements the parsing and the four refusals and nothing else. `routing/002-dual-write-fan-out`
pins the parse in both, and its cases assert the thing most likely to go wrong quietly: a **write
shape still resolves to the source**.

## 8. Routing

**The routing table is validated when the map is loaded.** It used to be validated at the first read
that routed through a broken entry, which is the worse of the two places: a map is a document handed
over and applied, so an inconsistency that only surfaces when one particular shape is issued fails
inside the client's request path at a moment nobody can predict — and a run that never issues those
operations is green while the map looks applied. Nothing in the check needs runtime information, so
nothing in it waits for runtime.

Two levels, because the model is optional at load:

| Model | Checked |
|---|---|
| absent | the target is an id declared somewhere in this map |
| present | the target is declared in **the group the shape belongs to**, and the key is a shape this model produces |

The second is the one that matters. Materialisation ids are unique only *within* a group, so a target
that exists in some other group is not evidence of anything — routing a shape at another group's copy
reads the entity out of a table that does not hold it, which is a wrong answer rather than an error. A
routing key that is not a shape of this model is a divergence too: the model version already matched,
so the two sides enumerated shapes differently, and that is how one library's write lands in a table
another library never looks at.

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

**Name hashing (§2a) is orthogonal to the tiers.** It is a mode, not a level: a Tier 0 library may
omit it entirely and still be complete, and a Tier 3 library may not have it. What is not optional is
agreement — a library that offers hashing must pass `hashing/`, because a client running two languages
against one model needs both to derive the same digests or each refuses the other's map. Declared
separately from the tier, for the same reason "supported" has to mean one thing: a library that says
"Tier 0 + hashing" is making a claim the vectors can check.

## 10. Running the vectors

There are five kinds:

| Kind | What it pins |
|---|---|
| `model/` | a neutral declaration, and the exact IR bytes, version, groups and shapes it must produce |
| `routing/` | a map plus cases: `(shape, in a write transaction?, needs freshness?)` to materialisation |
| `errors/` | which error, and at what stage it must be raised — `model` or `map` |
| `canonical/` | a value fed straight to the encoder, and the exact bytes |
| `hashing/` | a salt, a model, and every digest §2a must derive from them |

**Three of the sets named in the tier table above do not exist yet:** `telemetry/`, `schema/` and
`migration/`. That is worth stating in the contract rather than leaving it to be discovered from an
empty directory, because §9 says a tier claim is one the vectors can check, and for Tier 1 and Tier 2
that is currently false. The reference implementation has both, covered by its own tests and by a
slice against a real PostgreSQL — which verifies that it works, not that a second implementation would
agree with it. The gap costs nothing while one library claims those tiers and everything on the day two
do, so the vectors are written before a second claim is accepted, not after.

An `errors/` case carries a `stage`. `model` cases feed `model.json` to the model builder; `map`
cases build the model **first, outside the assertion**, then feed `map.json` to the map loader with
the options in `load` (`require_signature`, `public_key`). That ordering is the point: a vector whose
model was broken by accident would otherwise throw at the model stage and satisfy an assertion that
looks only at the class and the message — a failure at the wrong stage entirely, which is the bug
`stage` exists to catch. A runner that meets a stage it does not implement **fails**; it does not
skip. A stage nobody runs is a rule nobody checks.

The `map` stage exists because there was no shared coverage of §7 at all. Every refusal there — each
one deciding where a client's data gets written — was checked in the reference implementation's own
tests and in nothing the two libraries share. The cost showed up as a message that rendered a literal
`{CONTRACT}` in one language and the version number in the other: a lost `f` prefix on a continuation
line inside an implicitly concatenated string, invisible in review because the group reads as one
string, and invisible to the vectors because they compare encodings and this path produces only a
diagnostic.

`hashing/` is only run by a library that offers hashing, and skipping it has to be visible: an
implementation that quietly runs zero of these while claiming to support the mode is the failure the
vectors exist to make impossible. One of its cases carries an identifier in two Unicode normal forms,
which is the case that fails if a port hashes before normalising — the defect that section exists to
name.

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

**There are two numbers, and they move independently.** `conformance/contract-version.txt` is the
**IR's**, which is what the vectors embed and what `model_version` is a digest of. The placement map
has its own, because the two documents change for different reasons and a single counter makes every
`model_version` move when a key is added to a map. Splitting them was itself a change to this
document and is recorded here rather than in a commit message.

**A library reads a range, not one number: the versions it implements and every earlier one it can
still read.** Forwards it is strict, because what came after cannot be known. Backwards it is not,
because it can: an older document is one this library once produced, and reading it is a matter of
knowing which keys were absent. A library that grows a key must therefore state what its absence
means - for `also_write`, "writes go to the source alone" - and that sentence is the whole of
backwards compatibility. When there is a version whose absence cannot be given a meaning, the floor
moves and that is a contract change like any other.

**Tightening a refusal is not a format change. Loosening one is.** Adding a rule that refuses a
document which was already internally inconsistent does not make `contract: 1` ambiguous — it makes
the two libraries agree, which is the whole point of the number. Dropping a refusal does: an older
library would reject what a newer one accepts, and then the meaning of a stored map depends on which
version happens to be installed. That is the same failure as `{"auto": true}` deciding table names
from the installed library, and it is refused for the same reason. Both directions get vectors either
way, because the argument above is only trustworthy if the newly refused shapes are written down.

If your library cannot reproduce a byte this document requires, the first hypothesis should be that
**this document is wrong** — that it wrote down what one language happens to do rather than something
language-neutral. That has already happened once: the rule "no floating point anywhere" conflated the
encoding with the type system, and had to be split into "no float literals in the encoding" and
"`float64` is a perfectly good field type".

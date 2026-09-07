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

- **`timestamp` in ClickHouse, until 7 September 2026.** The other two are limits of a driver; this
  one was ours. `DateTime64(3)` round-trips a millisecond faithfully and PostgreSQL keeps six digits,
  so the type was *mapped* and the two engines still disagreed — which this rule does not catch,
  because it asks whether one engine round-trips a value and not whether two agree about it. The
  consequence surfaced as a copy that changed every row silently. Both render `DateTime64(6)` now.
  **The rule above has a companion, and this is it: where the product copies between two engines,
  a type has to round-trip *and* the two spellings have to hold the same thing.** The reason the
  first version said three is worth keeping: it was chosen against ClickHouse's plain `DateTime`,
  which is second-resolution, rather than against the engine standing beside it.

Both `bytes` and `json` are unmapped today, and that costs real capability — an event payload in a column store is a
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

## 4a. The neutral declaration form

The IR above is what a library *produces*. This is the JSON it is produced **from**, and until this
section was written the format was defined only by example — by whatever the conformance vectors
happened to contain. That was a real gap rather than a tidiness one: `conformance/vectors/**/model.json`
is the only input every implementation shares, so a library that reads it differently computes a
different `model_version` from the same file and the vectors cannot see it, because a vector asserting
a refusal passes whatever model was built on the way to the refusal.

It is also not only a test format. The control plane stores a client's declared model in exactly this
shape and rebuilds it to issue every map, so a rule invented here reaches a client's physical schema.

```jsonc
{
  "entities": [
    {
      "name": "Order",                                   // required
      "fields": [                                        // required, non-empty
        {"name": "id", "type": "uuid"},                  // nullable defaults to false
        {"name": "note", "type": "string", "nullable": true}
      ],
      "key": ["tenant", "id"],                           // required, non-empty, order is the key's
      "pii": ["email"],                                  // optional; absent means none
      "residency": "EU"                                  // optional; absent means null
    }
  ],
  "relations": [{"name": "user", "from": "Order", "to": "User"}],   // optional
  "atomic": [["Order", "Payment"]],                                 // optional
  "cost_ceiling": {"amount": "500.00", "currency": "EUR"}           // optional; absent means null
}
```

**No key is ever invented.** Both of our libraries used to default an absent `key` to `["id"]`, which
is the one thing §3 forbids in the neighbouring case — "a host type with no mapping is an error, do
not guess" — and it had two costs. A third implementation reading the vectors could not know the rule
existed, so it produced a keyless model and a different `model_version` for the same document with
nothing failing. And a client who omits the key would be issued a map for a model with a primary key
they never declared, on a table our layout then creates with it. Python spelled the default `or` and
TypeScript spelled it `??`, which differ on exactly one input: `"key": []` invented a key in one
language and stayed keyless in the other. One declaration, two model versions, and the vectors were
silent because no vector declares an empty key.

Seven refusals, all `DeclarationError`, and all of them tightenings under §11:

1. a model declares at least one entity;
2. an entity declares at least one field — an entity that stores nothing cannot be placed;
3. entity names are unique within a model. Names reach the IR and the colocation graph, and a
   duplicate makes "which entity is this" unanswerable in a document whose whole job is to answer it;
4. field names are unique within an entity, or the layout has two columns of one name and the refusal
   arrives from the client's engine at `CREATE TABLE`;
5. `key` is present, non-empty, written as a **list of field names** rather than in the IR's form
   (`errors/036` — see the paragraph below), names fields of its own entity, and names each of
   them once. A key is
   what makes a row addressable, migratable and verifiable: §12's backfill compares rows by it, so an
   entity without one is a group that cannot be moved, and that would be discovered during the
   migration rather than when the model was declared;
6. `pii` names fields of its own entity. This one is not cosmetic — a `pii` entry that is not a field
   silently protects nothing, and §7 says the exclusion of personal data from a derived copy is meant
   to be *readable* in the library rather than taken on trust;
7. `relations` has at most one entry per (`from`, `name`), and every `from` and `to` is a declared
   entity.

A field and a relation on one entity **may** share a name, and the rule that would have refused it
was written and then deleted: §2a says the digest collision between them is intended, and
`hashing/003-reserved-object-keys` declares exactly that shape. It is legal because a relation does
not reach a layout under its own name — it reaches it as `<relation>_<target key field>` — so there
is no column for it to collide with. Our TypeScript library refuses it in `buildModel` today, which
means the model that vector pins cannot be declared through that library's own front door. That check
is the one that should go.

**The IR is not this document, and the difference is one key.** They are close enough to be
confused: an IR entity has `name`, `fields`, `pii` and `residency` in the same places, and differs
where a key is written — `[{"field": "id", "position": 0}]` there, because array order is not
load-bearing anywhere else in the IR, and `["id"]` here, because that is what a person writes. Three
of our own control-plane tests handed the IR over as a declaration and nothing noticed for weeks,
because nothing read the stored document back. A library that offers a way *out* of its own model
type — ours are `neutral_declaration()` and `neutralDeclaration()` — spares a client writing their
model twice, and the confusion is worth a named refusal rather than whatever a dictionary lookup
raises: `errors/036`.

These are enforced where the IR is assembled rather than at each front door. Both libraries had some
of them on the decorator path and not on the loader path, so the vectors ran a weaker validator than
any application does — which is the suite's own failure mode written down in §10: a vector that
passes without reaching the code it describes takes the place of one that would have.

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

## 6a. The telemetry window document

This is what a Tier 1 library hands over: one document describing one window of traffic, aggregated
**in the client's process**. Nothing here is sent by a library — `docs/observability.md` says so and
a test measures it — so this section describes a document a client produces and gives us, in the
same sense §4a describes the one they declare.

It is written down for the same reason §4a was. Until this section existed the format was defined
only by example, by whatever `conformance/vectors/telemetry/` happened to contain, and that is a
real gap rather than a tidiness one: the control plane parses this document to score a placement, so
two libraries disagreeing about a field name or a denominator hand the planner different features
for identical traffic and nothing raises. A third implementation reconstructed all of it from four
vectors, which worked, and found two rules that were undetermined while doing it.

**It is the one document not bound by §1.** §1 rejects floating point outright because a float's
textual form differs between languages, and this document is almost entirely floats. It is not
signed, not hashed and never compared for equality, so that rule does not bind it. What makes it
comparable is narrower: **every number in it is either a ratio of two integers or a bucket edge
divided by a million**, and IEEE 754 requires division to be correctly rounded, so two languages
compute the same double even where they would print it differently.

**It carries no clock**, which is what makes it deterministic. Two of its features would need a
duration and both are declared unmeasurable instead.

```jsonc
{
  "model_version": "1ff3e78b85e2ec40",   // the model this traffic was measured against
  "complete": true,                       // false if a window was evicted; see dropped_windows
  "dropped_windows": 0,                   // windows a full buffer discarded, oldest first
  "groups": {                             // only groups that saw traffic
    "Event": {
      "calls": 15,
      "read_write_ratio": 2.75,
      "shape_mix": {"point_read": 0.4, "write": 0.2},
      "latency_p50_ms": 0.004,
      "latency_p99_ms": 2.048,
      "result_cardinality_p50": 3.0,
      "result_cardinality_p99": 4100.0,
      "pk_access_share": 0.4,
      "has_time_dimension": true,
      "distinct_shapes": 7,
      "error_share": 0.06666666666666667,
      "missing": ["total_bytes"],          // sorted; see below
      "complete": true,
      "copies": [                          // optional, absent when the group has no derived copy
        {"group": "Event", "materialization": "Event@ch", "writes": 3, "failures": 1,
         "lag_p50_ms": 0.008, "lag_p99_ms": 1.024, "complete": false}
      ]
    }
  }
}
```

**A group with no traffic is not in the document at all** — a document lists what was observed. A
caller reporting on every group asks for its features directly and gets the measurements whose
answer is zero (`calls`, `shape_mix`, `distinct_shapes`) with everything else in `missing`, and
`no_traffic` named there as the reason. `telemetry/004`.

| Field | How it is derived |
|---|---|
| `calls` | operations recorded for the group, failures included |
| `read_write_ratio` | reads ÷ writes. **Omitted** when there are no writes, because the alternative is a division; `shape_mix` still carries the fact |
| `shape_mix` | per §6 `kind`, that kind's calls ÷ `calls`. Only kinds with traffic appear |
| `latency_p50_ms`, `latency_p99_ms` | the **upper edge** of the bucket the percentile falls in, in milliseconds |
| `result_cardinality_p50`, `result_cardinality_p99` | percentiles over the **mean rows per call of each read shape** — one sample per shape, not per call. Omitted when no read shape has a successful call |
| `pk_access_share` | `point_read` calls ÷ `calls` |
| `has_time_dimension` | whether any entity of the group declares a field of type `date`, `timestamp` or `timestamptz`. By **type**, never by name: a `created_at` of type `string` is not one |
| `distinct_shapes` | distinct shape identifiers seen, failures included |
| `error_share` | failed calls ÷ `calls` |
| `missing` | sorted names of every field above whose value is unknown, plus `no_traffic` when there was none |
| `complete` | whether this group's own measurement is whole |

**`missing` is derived from the values, never written by hand.** It exists so a reader never has to
infer absence from a null, and the planner treats unknown and zero as opposite evidence. Five
features are in it always, because no library can measure them from traffic: `daily_growth_bytes`,
`index_to_table_ratio`, `time_filtered_share`, `total_bytes`, `write_burstiness`. A hand-written
list of them was wrong by one the day a sixth field was added, which is why it is derived.

### The histogram

Twenty-five buckets, the first one every duration below a microsecond and the last one clamped:

```
bucket(ns) = 0                                  if ns / 1000 == 0
             min(24, 1 + floor(log2(ns / 1000)))  otherwise      // integer division
edge(i)    = 1000 * 2^i nanoseconds                              // reported in milliseconds
```

The index is arithmetic and not a logarithm, and the reason is not that a logarithm is wrong here:
`telemetry/002` pins the three rows around every power of two and a `log2` passes all of them,
because the libms available are exact at a power of two. A property no output can distinguish is not
one a vector can hold, so both reference libraries check it over their own source instead and say
so. What the integer form has is no libm in it at all.

### One rank rule for the whole document

A percentile is **nearest-rank**: the smallest sample that at least `fraction` of the data is not
above — index `ceil(fraction × n) − 1` of the ordered sample, and for the histogram the first
bucket whose cumulative count reaches `fraction × n`.

That sentence is here because the two percentiles in this document used to disagree. The histogram
took the ceiling and the cardinality took the floor, and the two coincide on every sample set with
an odd count — which every case in `telemetry/` had — so one document carried two conventions and
nothing could see it. They differ exactly when `fraction × n` is an integer: two samples at p50.
`telemetry/007`.

**A failed call counts in the histogram and not in the cardinality**, and the two answers have
different reasons rather than one convention. A failure took time, so dropping it would flatter a
window precisely when an engine is in trouble. It returned no rows because it failed rather than
because the data is sparse, so averaging that zero in understates what a read of that shape
returns — and `error_share` already carries the failure rate, so smearing it into a second feature
puts one fact in two places. `telemetry/008`.

### Copies

A `copies` entry is what a derived copy's write cost and health look like from the client's process,
and there is exactly one thing to know about it: **a fan-out is not a shape.** It is not an
operation the application asked for, so recording it as a write would move `read_write_ratio` and
`shape_mix` — the features a placement is scored on — *because a copy exists*, and the same
application would look twice as write-heavy under a map with a copy. `writes` counts every attempt,
`failures` the ones that did not land, `complete` is `failures == 0`, and the lag percentiles come
out of the same histogram as everything else. Entries are sorted by materialisation.

`complete` on a copy is what a lag figure hides: **a copy missing a thousand rows can have an
excellent p99.** Both are reported for that reason.

### Refusing a model it did not measure

Serialising a window needs exactly one fact from a model — whether a group carries a time
dimension — and reading it from a different model would attach that fact to the wrong groups and
claim `has_time_dimension: false` for a group that has one. False is a claim. So a window refuses to
be serialised against a model whose version is not the one it recorded, rather than defaulting: this
document is what a placement decision is adjudicated against years later. The error class is not
part of the contract, because this is a caller's mistake rather than a document a library was
handed; the message is. `telemetry/006`.

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

**The two directions are two refusals and they name different numbers**, which is worth saying
because a library that merges them into one message about the range fails `errors/006`. Too new
names the **ceiling**: the document is from after this library and the ceiling is the fact that
matters. Too old names the **floor**: the oldest it still reads. A single message naming
`1 through 2` names both and tells the reader neither of the two things they came for, and a third
implementation wrote exactly that.

Rules a library must enforce, all of them refusals rather than warnings, because this document decides
where data is written:

- `contract` must equal the version the library implements. Not "at least" — equal.
- `model_version` must equal the version of the declared model. A mismatch is refused, never
  reconciled.
- `map_version` must be present and a positive integer. Both libraries used to read it as
  `int(document.get("map_version", 0))`, so a document without one loaded as version **0** — and this
  is the number the forward-only rule below compares against a watermark in the client's own engine.
  A map that forgot to say which version it is cannot be the one that decides whether an older map is
  being replayed.
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

#### `key_id`, and why a library takes a *set* of public keys

A library accepts **either one public key or several under names of the caller's choosing**. The
several-keys form is what makes rotating the signing key possible without breaking anybody: while
both are configured, maps signed with either verify, so the two sides do not have to change in the
same second. Our signing key is never overwritten — doing that silently would invalidate every map
in the field and the libraries would rightly refuse to start — and the client's side is a
configuration change in their own deployment.

A library reports back **which** of the caller's keys verified the map. That is not decoration:
adding a key is safe and reversible, removing one is not, and a client cannot know when the old key
is safe to drop without seeing which key the maps they are actually receiving were signed with.

`key_id` in the signature block **orders the attempts and decides nothing**. Every configured key is
tried. The reason is in the definition above: the signature covers
`canonical_bytes(map without the "signature" key)`, so the whole signature block — `key_id`
included — is *outside* what is signed, and anybody can edit it. A library that treated it as
authoritative would refuse a perfectly good map on the strength of an attacker's annotation.
Authenticating it would mean changing what the payload is, which recomputes every signature ever
issued and is a bump in every language at once, for no gain: as an ordering, being wrong about
`key_id` costs one failed verification and reaches the same answer.

Two refusals belong to the caller's configuration rather than to the document, and are raised before
verification is attempted:

| Configuration | Result |
|---|---|
| a public key that is not 32 bytes | **refused**, naming the key and its length |
| an empty set of keys | **refused**: this is not the no-account mode, it is a configuration that can verify nothing |

Both exist because of what the alternative looks like. A key pasted a byte short would otherwise
present as "this map does not verify" — identical to a map from the wrong source, and with a
completely different fix.

**What this does not give you is expiry.** A library is never told when a map was issued (section
7 has no date in it anywhere) and has no clock to compare one against, so it cannot stop trusting a
key on its own: **an old key verifies for as long as the client keeps it.** Revocation is therefore
the client removing a key from their configuration, and a rotation forced by a compromised key is
that same act done urgently rather than an overlap — the two are different runbooks and the
difference is the order of the steps. This is the same absence that makes "you stop paying and
nothing breaks" true, and it is not a gap to be closed later.

None of this is a change to the document. The bytes a producer writes are the same; the
`signature` block is unchanged, `key_id` was already in it, and a library that ignores `key_id`
entirely still reads every map correctly. Section 11's rule is about what a *document* may say — so
this is neither a tightening nor a loosening, and the contract version does not move.

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

#### A fifth refusal, and it cannot be made at load

A Tier 2 runtime refuses one more shape, and where it refuses is forced by this format rather than
chosen: **a fan-out into a dialect that stores fewer sub-second digits than the source, or into one
this library holds no precision facts about.**

The second half of that sentence is the reachable one, and the history matters more than the rule.
Until 7 September 2026 PostgreSQL kept six sub-second digits for `timestamp` and `timestamptz` and
ClickHouse kept three, so a copy between them changed essentially every row — an ordinary "now"
carries microseconds — and changed them **silently**, because the insert succeeds and the value
comes back different. Measured on live servers: `09:30:15.123456` written once came back unchanged
from PostgreSQL and as `09:30:15.123` from ClickHouse, with no error on either side.

**Then the pair was removed rather than kept refused.** The three digits were a choice this project
made, and it had been made against a comparison with ClickHouse's plain `DateTime`, which is
second-resolution — true, and the wrong comparison, because the engine standing beside it in the
same product keeps six. Keeping the refusal instead would have meant that no group with a time
column could have a ClickHouse copy at all, which is the shape this product is sold on. Both
dialects render six now (§3.1, §7a), and **no pair of the three dialects shipped here truncates**.

What is left is a ratchet, and it will fire on the day somebody writes a fourth adapter: a neutral
type with no precision recorded for one of the two dialects refuses rather than being guessed at. A
type nobody classified is a type nobody checked.

The refusal cannot be made while reading the document. **A map names engines by name and carries no
dialect** — deliberately, because a name is the client's and reasoning from it is refused
everywhere here — so at load time the question has no answer. The earliest door that can answer it
is the one holding the adapters, which is where a session is built. So a Tier 2 runtime raises
`MigrationRefused` there, and `errors/` gains a third stage for it: `session`.

Three things about it are pinned, and the two positives are what make the refusal mean anything:

| Shape | Answer | Vector |
|---|---|---|
| a copy into a dialect with no precision facts recorded | refused, and **before any engine is touched** | `errors/038` |
| both engines of one dialect, a `timestamptz` column | opens, and the row reaches both | `migration/020` |
| PostgreSQL source, ClickHouse copy — the central shape | opens, since both keep six digits | `migration/021` |

`errors/038` carries an empty `calls.json` and that is the load-bearing half. A map that can never
work must not create a table or issue a query on the way to being rejected; a runtime that gathered
the watermarks first and refused afterwards would give the same answer with the price already paid,
which is the defect `migration/001` was written for in the other direction.

**The table is not what protects an existing table, and this is worth knowing before writing an
adapter.** The rule above reads what a *dialect* keeps. What a client's table actually holds is a
different fact, and it is checked where it can be read: since the same day, both adapters compare
the column types the server reports against the ones the layout declares, after applying the schema
(§7a). That is what makes a change to a rendered type safe — a table left at three digits by an
older map is refused by name rather than written into.


## 7a. The DDL a layout renders

A Tier 2 library turns a layout into statements. They are **bytes a server receives**, so two
libraries have to produce the same ones: a client running two languages against one model would
otherwise end up with two physical schemas, which is the failure §4a's `model_version` exists to
prevent one level up. `schema/` compares them exactly.

Written down here for the reason §6a is: until this section existed the statements were defined by
whatever the vectors happened to contain, and a third implementation reconstructed them from ten
cases. That worked, and it also found that the escaping rule was not pinned by any of them.

**The layout is the authority on type spelling and this renderer never translates one.** A
layout's `columns` already carry dialect types — the planner put them there — so rendering supplies
syntax and nothing else. `schema/002` is the case that says so out loud: it renders a ClickHouse
layout with the `postgres` dialect and expects `DateTime64(6, 'UTC')` to come out unchanged. A
renderer that mapped neutral types to dialect types would pass every other case in the family and
fail that one, and it would be a second copy of the type mapping to keep in step with the planner's.

### Order

| What | Order |
|---|---|
| tables | code point order of the **entity** name, not the document's key order (`schema/012`) |
| columns | code point order of the column name (`schema/003`, `schema/009`) |
| the key | **the order the model declared**, never sorted — `(tenant, id)` and `(id, tenant)` are different keys (§4) |
| indexes | code point order of the index name; each index's columns in the order the layout gives |

Reading either order off the document is the mistake to avoid, and it is not hypothetical: the
compatibility view below read its column order off the layout on the stated grounds that the
document was sorted, and it is not, because a foreign-key column is appended per relation. Every
entity with a relation had a view and a table listing the same columns two ways.

### Identifiers

**The two dialects escape differently and there is no escaper to share.** Both rules are measured
against the servers this repository tests against, by creating the table and reading the name back
out of the catalogue rather than by asking whether the statement was accepted:

| Dialect | Delimiter | Escape |
|---|---|---|
| `postgres` | `"` | the delimiter is **doubled**. A backslash, a backtick and an apostrophe are literal, and backslash-escaping the delimiter is a *syntax error* |
| `clickhouse` | `` ` `` | the delimiter **and the backslash** take a backslash escape. A double quote and an apostrophe are literal |

The ClickHouse backslash is the half that had been missing and the way it fails is worth knowing:
inside a backtick-quoted identifier that lexer reads a backslash as an escape introducer, so a field
called `a\nb` reaches the server as a column called `a`, a newline and `b` — a different name,
accepted in silence. A backslash before a letter the lexer does not know (`back\slash`) survives
untouched, which is exactly what makes the defect look absent. `schema/011` pins both dialects, and
nothing in the family carried a delimiter at all before it: the escaping could be deleted outright
and every schema vector stayed green.

### What the server holds afterwards

**A Tier 2 runtime verifies the columns it just applied, by name *and by type*.** `CREATE TABLE IF
NOT EXISTS` accepts a table of that name whatever shape it is in, so a leftover from an older map,
another application or a hand-run migration is kept in silence — and the first write then fails in
the client's request path with an error naming a column rather than the cause.

Types were left out of this check until 7 September 2026, on a stated reason that was a true
observation about the wrong catalogue: PostgreSQL's `information_schema.data_type` reports `numeric`
for a `numeric(8,2)` column, so comparing against it would flag differences that are not
differences. `pg_catalog.format_type(atttypid, atttypmod)` reports the canonical type *with* its
modifier. Measured against every type this document defines, eleven of thirteen come back as the
exact string the renderer wrote and the two that do not are the timestamp aliases, which the server
resolves itself through `to_regtype`. ClickHouse needs no resolution at all: `system.columns.type`
returns the rendered string exactly, down to the space in `Decimal(12, 2)`.

What the omission cost, measured: a table whose `at` column was **`text`** where the map said
`timestamptz` passed the check and was reported as a good schema.

Two rules for an implementer:

- compare **literally first**. That is the answer for every type here except an alias, and it is the
  only comparison that catches a modifier changing — `numeric(12,2)` against `numeric(8,2)`, which
  an alias resolver collapses to `numeric` on both sides and calls equal;
- resolve an alias by **asking the server**, not from a table in your library. A table of alias
  spellings is a second copy of the renderer, and the day the renderer learns a type it will be a
  copy that disagrees.

A missing column is refused. An **extra** column is not: a client may have added one outside SDE,
the map does not name it, and refusing would make the library an obstacle to work it has no opinion
about.

### Statements

```sql
-- postgres
CREATE TABLE IF NOT EXISTS "order" ("id" uuid, "tenant" uuid, PRIMARY KEY ("tenant", "id"))
CREATE INDEX IF NOT EXISTS "payment_order_idx" ON "payment" ("order_tenant", "order_id")

-- clickhouse
CREATE TABLE IF NOT EXISTS `order` (`id` UUID, `tenant` UUID)
  ENGINE = ReplacingMergeTree ORDER BY (`tenant`, `id`)
```

Every statement is idempotent, and that is not decoration: a statement that is correct once is a
deployment that works until the first restart. The `schema/` statements are executed against a real
PostgreSQL and a real ClickHouse **twice each** for that reason.

Four things a renderer refuses rather than approximating:

- **an index for ClickHouse.** It has no B-tree to put one in. A layout carrying an index is a valid
  document that signs and loads correctly, and this is where it stops being applicable — which is
  why the control plane asks a library rather than reimplementing the answer;
- **a layout with no columns for an entity it names a table for** — there is nothing to create;
- **a key naming a column the layout does not have** — the `PRIMARY KEY` or `ORDER BY` would name a
  column that is not in the table;
- **no key for a table** — a table without one cannot be addressed, migrated or verified (§4a.5).

And two things are not refusals:

- **`partition_by` is refused too, and the refusal is why this bullet exists.** The key is parsed,
  the control plane emits it when non-empty, and **no renderer has ever applied it**: a layout
  declaring it produced an unpartitioned table and said nothing. Nothing populates it today, so no
  issued map has ever carried one, but a hand-written map legally may — the no-account mode is a
  documented mode — and silently ignoring a storage decision in a signed document is the worst of
  the three available answers. Rendering it would mean designing two dialect-specific features with
  no requirement behind them and interpolating a caller's SQL fragment into DDL. So it fails closed
  until partitioning is implemented, at which point accepting it again is a *loosening* and
  therefore a contract bump, which is the right price for the key starting to mean something.
  A tightening under §11, so no bump now. `errors/037`;
- **an engine whose schema is fixed in its own source renders no statements at all**, and "no
  statements" there means "nothing to run" rather than "no tables in this layout". The two answers
  are different questions and a library needs both, so they are separate calls: "render this
  layout" and "does this engine take DDL from us". They also refuse an unknown dialect
  *differently* — "there is no DDL for that dialect" against "that is not a dialect" — because the
  questions are different (`schema/005`).

### Compatibility views

A view on the **target**, under the table name the group had in the engine it left, so hand-written
SQL naming the old table keeps working across a migration. It renders on the target because a view
cannot cross engines: the old table is in the old engine and no dialect here can select from another
server.

The column list is sorted the same way `CREATE TABLE` sorts it — a view exists for hand-written SQL
alone, so a view and a table listing one table's columns in two orders is the whole defect this
sort was written to fix. The two dialects differ in two places, both measured:

| Dialect | Opening | Select |
|---|---|---|
| `postgres` | `CREATE OR REPLACE VIEW` — it has **no** `CREATE VIEW IF NOT EXISTS`, which is a syntax error there | plain |
| `clickhouse` | `CREATE VIEW IF NOT EXISTS` | `... FROM \`t\` FINAL` |

`FINAL` is the part that costs money if it is missed: a query moved verbatim onto a
`ReplacingMergeTree` counts a row written twice under one key twice, until a background merge
collapses it. Measured with merges stopped: two rows against one.

A table can also have **no** view, and each reason is reported rather than skipped: the old name and
the new one are the same (the dialect moved, not the name), the source layout gives no old name at
all, or the engine imposes its own schema and has nowhere to put one. `schema/008` is the
name-did-not-move case and `schema/013` is the one where it did — and before `013` existed those two
answers were the same input for ClickHouse, so a library refusing every ClickHouse view passed.

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

## 8a. The order a refusal comes in

A document can have two defects. Which one a library reports has to be the same everywhere, or one
document has two meanings again — this time in the message rather than in the routing, which is the
half of a library a person actually reads during an incident.

This is not hypothetical and it was not free. `errors/019-layout-names-the-reserved-bookkeeping-table`
carries **two** defects: a reserved table name in the layout of group `Event`, and a layout that is
both `auto` and explicit in group `Order`. It pins the first one, and it did so only because `Event`
happens to be written earlier in the file and both of our languages iterate an object in insertion
order. A third implementation in Go, whose maps are deliberately iterated in a randomised order,
failed that vector in **5 of 20 runs** — a conformance suite that is flaky against correct code,
which is the shape of test that teaches people to press the button again.

So the order is fixed, and it is fixed by name rather than by document order. Nothing in either list
depends on how the caller's JSON parser preserves keys.

**The model stage:**

1. every field's type is in the vocabulary (§3);
2. every relation's `from` and `to` is a declared entity, and every atomicity names declared
   entities — references before constraints, because a name that points nowhere makes every later
   check about a thing that is not there;
3. the seven refusals of §4a, in the order they are written there.

**The map stage:**

1. the document is an object; `contract` is in range; `model_version` matches; `map_version` is a
   positive integer;
2. `require_signature`, then the signature itself. Before the structure, deliberately: a document
   whose origin cannot be established is not worth a detailed reading, and the refusal a client needs
   is the one about the key rather than the one about the seventh group;
3. every group, **in name order**, and within a group: the source, then the derived copies in
   document order (an array's order is the document's), then id uniqueness, then `also_write`;
4. group coverage, both directions (§7);
5. `routing`, entries **in shape-id order**.

**The session stage** — Tier 2 only, and it exists because §7's fifth `also_write` refusal needs the
adapters:

1. every engine the map names has an adapter (`EngineError`);
2. the fan-out precision rule: groups **in name order**, within a group the `also_write` copies in
   document order, within a copy the entities **in name order** (`MigrationRefused`);
3. the forward-only check (§7, *Forward only*).

Two of those three orderings are not new rules and are written down here only so nobody has to
re-derive them: groups are already sorted by name by §5, and a group's members with them, so the
first two levels follow from the model rather than from this list. Both reference implementations
sort the entities again at this point, and **that second sort cannot be tested** — measured, by
removing it in each language and watching every vector stay green, because no input the loader can
produce is unsorted. It is kept as a local statement of a rule whose source is one module away, and
it is recorded here as unmutatable rather than left to look like coverage.

What *is* a choice is **2 before 3**: a map that can never work must not create a bookkeeping table
or issue a query before it is rejected. `errors/038` pins that with an empty call list, and it is a
signed map for that reason — against an unsigned one the forward-only check does nothing at all, so
an unsigned case would stay green whichever way round the two were run.


Both libraries already sorted the routing entries and neither sorted the groups, which is how a rule
gets half-applied: the reason for sorting was understood in one loop and read as a detail in the
other.


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

There are nine kinds:

| Kind | What it pins |
|---|---|
| `model/` | a neutral declaration, and the exact IR bytes, version, groups and shapes it must produce |
| `routing/` | a map plus cases: `(shape, in a write transaction?, needs freshness?)` to materialisation |
| `errors/` | which error, and at what stage it must be raised — `model`, `map` or `session` |
| `canonical/` | a value fed straight to the encoder, and the exact bytes |
| `hashing/` | a salt, a model, and every digest §2a must derive from them |
| `signature/` | which of the caller's keys verified a signed map, or which refusal it must raise |
| `schema/` | the DDL a layout renders, per dialect, and the compatibility view beside it |
| `telemetry/` | a window document, compared parsed rather than as bytes — see below |
| `migration/` | taking part in a migration: the record a gate reads **and** the calls that produced it |

This table said "five" and listed five for as long as there were nine, with the other four argued
in the prose below it. Nothing counted them, which is the failure mode of a number about our own
artefacts; `test_implementer_documents.py` derives both the count and the rows from the tree now.


**Every set named in the tier table exists.** That sentence replaces one that listed what was
missing, and the history is worth keeping because §9 makes a tier claim into something the vectors
can check, and for a while that was false: `telemetry/`, `schema/` and `migration/` were named in
the tier table and were not written. The paragraph that admitted it also said what would close it —
*the vectors are written before a second claim is accepted, not after* — and on 6 September 2026 a
second library reached Tier 2, so all three were written first. Reading them in that order is the
cheapest way to see what a tier means.

The gap cost nothing while one library claimed those tiers, and closing it found five defects in the
one that did. Each is described with the family that found it.

`schema/` is worth reading for what writing it found: the DDL renderer and the compatibility-view
renderer listed one table's columns in two different orders, because the view read the order off the
layout document on the stated grounds that the document was already sorted — and it is not, since a
foreign-key column is appended per relation. The test meant to hold that property used a fixture
with no relations. Both renderings are sorted by name now, `schema/009` feeds a document whose
columns are deliberately reversed so that removing either sort changes a different string, and every
statement in the family is executed against a real PostgreSQL and a real ClickHouse **twice** —
because every statement here claims to be idempotent, and one that is correct once is a deployment
that works until the first restart.

`telemetry/` was written the same day and for the same reason, and it is the one family whose
expectations are compared **parsed rather than as bytes**. That is an exception to §1, which rejects
floating point outright because a float's textual form differs between languages — and a window
document is almost entirely floats. It is not signed, not hashed and never compared for equality, so
the rule does not bind it; what makes the family checkable is narrower and is the sentence to
remember: **every number in a window is either a ratio of two integers or a bucket edge divided by a
million**, and IEEE 754 requires division to be correctly rounded, so two languages compute the same
double even where they would print it differently. The document also carries no clock, which is what
makes it deterministic.

Two findings came out of writing it, and the second is about a mechanism rather than about code.
The reference had **no serialiser for the window at all** — it measured traffic and the walkthrough
typed the resulting document in by hand, so every client in every language would have written their
own and two clients of one model would have produced two documents from identical traffic. And
`missing`, the set that exists so a reader never has to infer absence from a null, was a
hand-written list of four names against a feature vector with five unknown fields; it is derived
from the values now. The set of kinds that count as **writes** is pinned by `telemetry/001`, which
records one operation of every kind: removing `bulk_write` from it survived its first mutation
because no vector had ever recorded one.

`migration/` is the last of the three and the only family whose cases pin **the calls a library
makes** as well as the answer it reaches. That is not thoroughness: a library that arrived at the
same counts by scanning the whole table and filtering in memory would satisfy every number here and
be unusable against a real one, so the sequence is the part that says *how*. It also makes the
shared fixture self-checking — an in-memory engine lives in each library's `testing` package rather
than in each runner, because a runner that writes its own is a runner whose fixture can be the thing
that differs, and a red vector would then say "one of two tables disagreed".

The case worth reading first is `001`, whose expected call list is **empty**. The no-account mode
promises no table, no query and no cost, and the TypeScript port gathered every engine's watermark
and *then* noticed the map was unsigned — the right answer, with the promise broken, which is the
one shape of defect a record of the decision cannot show.

**An `errors/` case pins the message, not only the class.** Its `match` field is a substring that
the refusal's own text must contain, compared literally and case-sensitively. That makes diagnostics
part of this contract, which is deliberate and was earned: a refusal of an incompatible contract
version once rendered a literal `{CONTRACT}` in Python and the number in TypeScript, and no vector
reached it because the suite compared encodings and that path produces only a diagnostic. A library
may say more than `match` and in any language it likes; it may not say less.

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

A vector is frozen once committed. Changing one is changing the contract, and every library declares
which version it implements. There is no quiet fix — a vector that was wrong was a contract that was
wrong, and somebody may have a stored placement map that depends on it.

**Which number moves depends on which document the vector pins, and this paragraph is a correction.**
The rule here used to say "bump `conformance/contract-version.txt`" for any vector change, which was
right when every vector embedded the IR and stopped being right when `schema/` was added on
6 September 2026. That number is the **IR's**, and the IR carries it *inside itself* — so bumping it
recomputes every `model_version` and invalidates every map in the field. Doing that for a change to
rendered DDL, which appears in neither document, would be the most expensive possible way to record
the smallest kind of change.

So: a vector that changes what the **IR** encodes moves `contract-version.txt`. A vector that changes
what a **placement map** means moves the map's own number. A vector that changes only the **DDL a
layout renders** moves neither — no stored artefact contains it, it is derived from a map plus a
dialect at apply time, and what protects a client whose table was created by the older rendering is
not a version number but the type check in §7a, which names the table and both types.

The one that is easy to get wrong: a change to what a library *does* with a map moves the map's
number even when no key changes. Equalising the ClickHouse timestamp precision changed which
`also_write` maps a library accepts — a contract-2 library refuses a fan-out a contract-3 one
performs, on the same document — so the map contract went to 3 with no new key anywhere.

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

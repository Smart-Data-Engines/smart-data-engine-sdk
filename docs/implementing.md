# Writing an SDE library

This is the practical companion to [`format-contract.md`](format-contract.md). That document says
what the bytes are; this one says how to get from an empty directory to a green conformance run, in
what order, and which parts of your language will fight you.

It exists because "the contract is a public document, sufficient to write a new implementation from
without asking us anything" is a claim, and a claim needs a measurement. The measurement is at the
bottom of this page, together with what it found.

**Everything below is either in the contract or is a property of a language, measured.** Where the
two disagree, the contract wins and this page is the bug.

## What you are building

A Tier 0 library. §9 of the contract defines the tiers; Tier 0 is the one that is not optional:

- a declaration turns into canonical IR bytes and a `model_version`;
- colocation groups and operation shapes, with their identifiers;
- a placement map parses, verifies its signature, and refuses everything §7 says to refuse;
- routing resolves;
- the error semantics of §6 of the requirements — which error, at which stage.

No network, no engine drivers, no telemetry. A Tier 0 library never opens a socket. Ours has zero
runtime dependencies for exactly that reason: it is the shape of the promise, not an optimisation.

## The order to build it in

Each step is passable with only the steps above it, so you always have a green suite to break.

**1. The canonical encoder (§1). Vectors: `canonical/`.**
Eight vectors, all hand-written from the document, and they are the ones to start with because
everything else is a digest of their output. Feed each `value.json` straight to your encoder and
compare the bytes. Two of them expect a refusal instead.

**2. The neutral declaration loader (§4a). No vectors of its own.**
Every implementation needs one: a conformance vector cannot contain your language's classes, so the
declaration in a vector is plain JSON. It is also not only a test format — the control plane stores a
client's declared model in exactly that shape — so read it literally and invent nothing. Both of our
libraries defaulted an absent `key` to `["id"]` and it cost two model versions for one document; see
the measurement below.

**3. The IR and the version (§4, §2). Vectors: `model/001-single-entity`.**
Do this one alone before the other model vectors. It is the only vector written by hand from the
rules rather than generated from an implementation, so it is the one that tells you whether you have
read §1 and §4 correctly rather than whether you agree with us.

**4. Groups and shapes (§5, §6). Vectors: the rest of `model/`.**
`groups.json` and `shapes.json` are ordered; compare them in order. `shapes.json` is absent from
`model/001` because that vector predates it — treat it as optional and assert it when present.

**5. The declaration refusals (§4a, §8a). Vectors: the `stage: model` half of `errors/`.**
Order matters here and the contract fixes it: a declaration with two defects must produce the same
refusal in every language. Put these checks where your IR is assembled, not at each entry point —
both of our libraries had them split across two entry points and enforced a different subset in
each, so the vectors ran a weaker validator than any application did.

**6. The placement map (§7). Vectors: the `stage: map` half of `errors/`, then `signature/`.**
Longest step. Every rule in §7 is a refusal, and every refusal decides where a client's rows get
written. The signature is checked before the structure (§8a): a document whose origin cannot be
established is not worth a detailed reading.

**7. Routing (§8). Vectors: `routing/`.**
Three conditions and a lookup. The vectors hand you a shape *identifier*, so your runner needs to
find the shape it belongs to — enumerate the model's shapes and index them by id.

**8. Hashed identifiers (§2a), if you offer them. Vectors: `hashing/`.**
Orthogonal to the tiers: a Tier 0 library may omit hashing entirely and still be complete. What is
not optional is agreement, because a client running two languages against one model needs both to
derive the same digests. If you skip it, say so where a user can read it, and make skipping it
visible in your test output.

Everything above is Tier 0 plus the hashing mode, and it is where most implementations should stop
until somebody is using them. The three steps below are Tier 1 and Tier 2, in the order the vectors
make checkable.

**9. Telemetry (Tier 1). Vectors: `telemetry/`.**
Counts and a histogram, no values, and never a lock on the path that records. The vectors feed
durations as integers rather than measuring anything, because a vector that timed something would
pin your machine. Two things in here have been defects in our own libraries and are the reason the
family exists: the set of shape kinds that count as **writes** — it had four copies once, and two
copies in one process is how the same operation becomes a write for routing and a read for scoring —
and the `missing` set, because unknown and zero lead a planner to opposite conclusions.

**10. Schema (Tier 2, first half). Vectors: `schema/`.**
A layout and a set of keys in, statements out, and no connection anywhere: render DDL as a value in
a module your engine adapters import, not inside the function that applies it. The vectors compare
statements **exactly** — they are bytes a server receives — and compare refusals by substring, like
`errors/`. Sort identifiers by **code point**, not by whatever your platform's default comparator
does; `schema/003` is the case that tells the two apart, and `schema/009` pins that a view and its
table list columns in the same order, which is a defect we shipped.

**11. Migration participation (Tier 2, second half). Vectors: `migration/`.**

`frozen.json` cases add the [exact native-barrier comparison](frozen-verification.md): epochs can
differ during operator maintenance; a runtime session and its watermark must not stand in for an
inspection context. Data values stay local, while barriers and comparison counts form the record.

Contract-4 `generation.json` cases additionally configure independently observed native epochs,
open a session, and compare every stored row and call. See [generation-bearing maps](generation-maps.md).

Cases with `fencing.json` implement the [native write-fence protocol](write-fences.md). Initialize
the supplied metadata and run its steps against a recording DDL backend; compare states, refusals
and every call. These cases replace the model/map/row inputs with a metadata snapshot.

Also execute each `verification.json` case as specified in [§7b](format-contract.md#7b-verification-requests-and-bound-reports):
project identity comes from local session configuration, and the complete request is checked before
comparison. Preserve it in the outgoing report. A stored fingerprint cannot describe a mutable
parsed map; retain an immutable input snapshot and reject copied objects that have not been loaded.

Dual write, the resume marker, and the forward-only check. A migration reaches a library as a map
with `also_write` and nothing else — there is no phase name in the document, and adding one would be
a second representation of a fact the fan-out and the routing table already carry. The marker is a
**row count and never a key**: a key needs a codec, and a lossy codec resumes *after* rows nobody
copied, which is silent data loss that differs per language.

These are the only vectors that need an engine, so they come with one: put an in-memory engine in
your `testing` package rather than in your test runner, matching `MemoryEngine`'s behaviour. Two
things about the cases are worth knowing before you start. They pin **the calls you make**, in one
sequence across the whole engine set — because the guarantee that a row reaches the source before
anything is attempted against the copy cannot be expressed in per-engine lists, which is a mistake
this family made first. And `001` expects **no calls at all**: an unsigned map is the client's own
document, so the no-account mode reads nothing, and gathering the watermarks before checking whether
the map is signed is the right answer with the promise broken.

The third stage of `errors/` belongs to this step too — `stage: session`, and it is the only stage
that cannot be reached by reading a document. §7's fifth `also_write` refusal is about what two
**dialects** do to a value, and a map names engines by name and carries no dialect, so there is
nothing to answer at load time; the earliest door that can answer is the one holding the adapters.
`errors/038` is a fan-out into a dialect the library holds **no precision facts about**, refused
with an **empty** `calls.json` — the refusal is half the claim and "it cost nothing" is the other
half. That is the case you will meet first: your dialect is new, so nothing in `DIALECT_PRECISION`
describes it, and the honest answer is a refusal rather than a guess. `migration/020` and `021` are
the two shapes that must still open, and they stop you passing `038` by refusing every map with a
timestamp in it, or every map whose two engines differ.
The two dialects shipped here both keep six sub-second digits, so **no pair of them truncates** and
the "would lose digits" branch has no reachable case today. It is still the rule; record what your
dialect stores and the branch becomes reachable for you.


### If your language's I/O is asynchronous

Tier 0 and Tier 1 touch no socket, so they can be synchronous in any language. Tier 2 talks to a
database, and in a runtime whose drivers are asynchronous a synchronous wrapper around them means
blocking the thread that runs everything else — which is a worse thing to do to a client's process
than a promise in a signature. So split the surface the way the tiers already do rather than
choosing one style for the whole library: the TypeScript implementation is synchronous through Tier 1
and asynchronous from Tier 2, and its session is *opened* rather than constructed, because the
forward-only check reads a table and a constructor cannot await. That last point is not a
workaround — it means a caller cannot hold a session that has not been checked, which is the
guarantee the reference gets from doing it in its constructor.

Two consequences to expect. The overhead budget below still applies to the work **your** library
adds, which is synchronous and sits between two awaits; measure that, not the round trip you are
inside. And a naive wrapper — one promise per row, or an await inside a loop that could have batched
— is the failure mode this note exists for, because it does not show up as a slow function but as a
saturated event loop under a load nobody tested.

## Running the vectors

Read `conformance/vectors/**` in your own test runner, in your own CI. That is the whole mechanism by
which implementations stay identical: a divergence becomes a red test for whoever caused it, instead
of an operation written to the wrong engine in somebody's production.

Five conventions that are not obvious from the tree:

- **`ir.json` and `bytes.json` hold bytes, not a document.** They have no trailing newline. Compare
  them as bytes. Parsing them first and comparing structures would pass two libraries that agree on
  the structure and disagree on key order or normalisation, which is the failure these vectors exist
  to catch.
- **`version.txt`, `salt.hex` and the other `.txt` files are text and do end with a newline.** Strip
  it.
- **`expected.json`'s `match` is a required substring of your refusal's own message**, compared
  literally. Diagnostics are part of the contract; §10 says why.
- **An `errors/` case may carry a `load` block in `expected.json`,** and it is arguments for the
  loader rather than anything about the refusal. `errors/015` sets `require_signature` there because
  its map is unsigned and §7 accepts unsigned maps, so the case is unpassable without it — and the
  natural conclusion from a red run is that the vector is wrong rather than that the runner missed a
  field. One case in thirty-seven, which is what makes it worth a line.
- **An `errors/` case's `stage` says where the refusal belongs.** A `map` case builds the model first,
  outside the assertion, then loads the map. A runner that meets a stage it does not implement
  **fails**; it does not skip. A stage nobody runs is a rule nobody checks.
- **The model stage is the loader *and* the builder.** A refusal about the shape of the declaration
  comes out of the loader, so calling it outside the assertion makes those cases unpassable — which
  is exactly how a Go runner failed `errors/036` while implementing the rule correctly. A `map` case
  is the other way round: its model must build, and that has to happen outside the assertion, or a
  vector whose model was broken by accident satisfies an assertion looking only at the class.
- **Fail loudly if you ran zero vectors.** A green suite that found no files is worse than a red one.

And one that is advice rather than convention: **break your own code and check that this suite
notices.** The `canonical/` family exists because a mutation that should have failed did not.

## What your language will do to you

Every row here was measured, most of them by a defect. None of it is in the contract because none of
it is about the format.

| Hazard | Where | How it shows up |
|---|---|---|
| Default string comparison is not code point order | JavaScript, Java, C# compare UTF-16 code units | Agrees with code point order for everything in the Basic Multilingual Plane, so the difference is invisible until one identifier is an emoji. `canonical/001`, `model/004` |
| ... and in some languages it is | Go compares strings by byte, and UTF-8 byte order **is** code point order | Nothing to do. Do not add a comparator you did not need |
| NFC is not in the standard library | Go (`golang.org/x/text`), Rust (`unicode-normalization`), C/C++ (ICU) | Measured: a standard-library-only Go build cannot pass `hashing/002` or `canonical/002`. This is the contract's only requirement that forces a third-party dependency, and it is worth knowing before you pick one |
| The JSON parser loses a number's lexical form | Go decodes to `float64` unless you ask for `json.Number`; Java to `Double` unless you ask for `BigDecimal` | `canonical/005` carries 2^53-1. Round-tripped through a binary float it re-emits as `9.007199254740991e+15`, which is bytes no other language produces |
| Integral floats are indistinguishable from integers | JavaScript: `Number.isInteger(1.0)` is `true` | You cannot refuse `1.0` as a float there. `canonical/007` uses `1.5` deliberately. It is a limitation, not a pass |
| Map iteration order is randomised | Go, deliberately | Any refusal that depends on iteration order becomes flaky. Measured: iterating a map's groups unsorted failed `errors/019` in **5 of 20 runs**. §8a fixes the order for exactly this reason |
| Object containers reserve identifiers | JavaScript: `{}` inherits `Object.prototype` | `map["__proto__"] = x` is silently a no-op and reading it back yields the prototype. `hashing/003` pins the digests for four such names. Whatever your language's equivalent is — a case-insensitive map, one that forbids an empty key — that vector finds it |
| "Tolerating" whitespace in the salt file | Python: `bytes.strip()` removes six byte values | A random 32-byte salt was silently shortened about 5% of the time, so the process that generated it and every later process derived different digests. §2a: read the file verbatim |
| An empty collection is not a missing one | Any language where `[]` and `{}` are falsy | An empty set of public keys is a configuration that can verify nothing and is refused; *no* key set is the no-account mode and is accepted. `signature/006`. Python's `or` and TypeScript's `??` differ on exactly this, and it cost two model versions for one declaration |
| JSON writers escape more than §1 allows | JavaScript-aware writers escape U+2028; many escape non-ASCII and `/` | Any of them changes the hash. `canonical/004` |
| A typed unmarshal is not a loader | Go, Java, C#, Rust: the obvious way to read `model.json` is into a struct | Then every malformed document fails with the *runtime's* error, and an `errors/` case at the model stage asserts the *contract's* error class. Map your parse failures onto it. Measured: `errors/036` was unpassable in Go until the unmarshal error was wrapped |
| A driver ignores a bound your caller wrote | Node: `pg` derives its connect timeout from a code-level option and *overwrites* whatever the connection string said | So "apply our default unless the DSN already sets one" - which is right in psycopg, where the parameter reaches libpq - leaves **no bound at all**. Translate the parameter yourself. Found by the test that asserts the caller's value wins, which failed by hanging until its own deadline |
| An unhandled driver event kills the process | Node: `pg.Client` is an `EventEmitter` and emits `error` when the server terminates a connection between queries | Node throws an `error` event with no listener, with no call of the client's on the stack. A restart of their database becomes an uncatchable crash in their event loop. Listen, record it, and put it in the next call's message |
| A skipped suite is not a reported skip | Node: vitest fails a file whose every suite was skipped, and calls it "no tests" | So a CI guard looking for the word "skipped" sees nothing and passes, and the file is unrunnable for anybody without servers. One test at the top level whose purpose is its own skip fixes both |
| A capability cannot be a missing method | Rust, Java, C#, Go with non-optional interfaces | The reference asks "can this engine keep the bookkeeping" and "can it be a migration target" by looking for members. In a language with static dispatch that question does not exist: a type implements a trait or interface at compile time. Make the capability **data on the value**, which §7 already requires anyway - "an engine that cannot keep the bookkeeping does not take part, and that is reported" - so a readable field is the shape that works in both kinds of language |
| ... and asking about presence is not asking about callability | Python `hasattr`, JavaScript `in` | The reference asks whether the member is *there*, on purpose: one that exists and is not callable fails at the call with a message naming it, which beats a capability check that quietly answers "no". A port that asked `typeof === 'function'` disagreed on exactly that input |
| The JSON parser loses a number's lexical form, again | Rust: `serde_json` decodes to `i64`/`u64`/`f64` unless the `arbitrary_precision` feature is on, and it is off by default | Same hazard as Go's `json.Number` and Java's `BigDecimal`, and the same vector finds it: `canonical/005`. Worth knowing that the fix is a feature flag rather than a different call |
| Map iteration order is randomised, again | Rust: `HashMap` seeds its hasher per process | §8a's ordering rules bind here exactly as they do in Go. `BTreeMap` sidesteps it and is the right default for anything that reaches a document |
| A skipped suite still runs its body | Node: `describe.skipIf` evaluates its callback, because it has to register the tests it then skips | An engine constructed at that level parses a DSN that is not there and takes the file down at collection, with a refusal that reads like a real one. Build the connection in the hook |

## Measuring your overhead

Requirement 3.5 gives the library one percent of an operation, and it is release-blocking. Measure it
**in your own runtime**: a garbage collection pause in one language and a naive async wrapper in
another are different problems with the same threshold, so a number carried over from another
implementation is not a measurement of yours.

Two rules and one warning, all of them learned here rather than reasoned out:

- **Measure the work you added, not the difference between two totals.** A round trip over loopback
  is hundreds of microseconds with a run-to-run spread of several percent, and the thing being looked
  for is a fraction of that. Time the added path directly — resolving a shape, finding a table name —
  and divide by a separately measured round trip.
- **Gate on the median.** Both of our tests once divided a tail by a tail, and the added work's own
  p99 is a collection pause rather than a property of the library: measured, a one-microsecond
  Python call's p99 moved 2.7–9.9 µs run to run while its p50 sat at 0.98 µs. Assert the tail too,
  against an allowance well above that noise — ours is five times the budget — so a tail that is a
  *code path* still fails.
- **If your library performs no operation, say what you divided by.** A Tier 0 library opens no
  socket, so there is nothing for it to be one percent *of*. Our TypeScript library measures against
  the **floor** — the cheapest round trip that runtime can make at all, a loopback socket the test
  starts itself — because everything a real engine does is slower, so one percent of the floor
  implies one percent of every real operation. Measured on an i3-7100U: the floor's p50 is ~115 µs,
  a ClickHouse query over HTTP from the same process is ~3.4 ms, and the library adds ~140 ns, which
  is 0.13% of the floor. Reporting the **break-even** is worth more than the ratio: an operation
  would have to complete in under ~14 µs before this library cost one percent of it.

**The C++ library measured itself and found more than routing.** It times the whole of a point read
through its session against an engine that hands back a row it already holds, less that engine's own
call timed the same way, so everything the session does is in the number and nothing over a socket
is. Built optimised on the same i3-7100U, the first measurement was 0.64 µs at the median, 2.5% of
its loopback floor of ~25 µs - C++ makes the cheapest round trip of the three - because every call
copied the key, sorted it, built a lookup key of copied strings and found the group, the route, the
engine and the table by name. Decided once per session instead, it is 0.11-0.12 µs without
telemetry over four runs, 0.37-0.47% of the floor, with a break-even of 11-12 µs. Measuring latency
costs two reads of the clock and the recorder's atomics: 0.24 µs, 0.6-0.95% of the floor and 0.09%
of a PostgreSQL point read through the adapter, so with telemetry the gate is the real operation, and
the floor's number is printed. Two rules come out of it for a compiled language. Gate only an
optimised, unsanitised build: a debug build of the same code measured nine to fifteen times slower,
which is the compiler. And measure the cheapest round trip *your* runtime makes: the floor is a
property of the runtime, and Node's is four times C++'s.

## Versions, tiers, and the word "supported"

Declare two things and mean them.

**The contract version you implement**, as a range: the version you were written against and every
earlier one you can still read (§11). Forwards you must be strict — what came after you cannot be
known — and backwards you need not be, because an older document is one this format once produced and
reading it is a matter of knowing which keys were absent. There are two numbers and they move
independently: the IR's in `conformance/contract-version.txt`, and the placement map's own.

**Your tier**, per §9, and per engine where it is Tier 2: "supported" has to mean the same thing in
every language or the word is worthless. A library that maps a neutral type by storing something
adjacent to it has not implemented that type.

`docs/implementations.md` is the list, and it says which implementations are ours and which are the
community's. Passing the Tier 0 vectors is the condition for being on it at all, and it is the one
thing we do not negotiate. What we do not ask for is a particular API: the contract is identical and
the ergonomics are idiomatic. A Java library that reads like transliterated Python is a worse Java
library and not a better SDE one.

## What this page deliberately does not tell you

- **How to map your language's types onto §3.** That is the one place where a language's own taste
  belongs. Where a host type maps ambiguously — Python's `datetime`, which may or may not carry a
  zone — pick a default, document it in *your* documentation, and offer an explicit way to ask for
  the other one. Do not guess silently, and do not extend the vocabulary.
- **How to shape your API.** See above.
- **Anything about Tier 1 or Tier 2 behaviour that the vectors do not cover.** That used to be a
  much larger category and it used to end with "talk to us first", because three of the sets §9's
  table names — `telemetry/`, `schema/` and `migration/` — did not exist, so those two tiers were
  claims nothing could check. They exist now, every set in the tier table does, and the two
  documents they pin have sections of their own: §6a for the window a Tier 1 library produces and
  §7a for the DDL a Tier 2 library renders. Steps 9 to 11 above are the path through them.
  What is left in this category is narrower and worth naming, because a third implementation found
  it by having to choose: a fan-out to **two** copies, and a `verify` that **matches**, are both
  legal and neither had a vector until the measurement below. If you meet something the vectors
  underdetermine, the answer is not to ask us — it is that the vector is missing, which is a bug
  report we would rather have than a conversation.

If something you need is in none of these documents, that is a bug in the contract and worth
reporting as one. It is not a matter of politeness: a contract that needs a conversation is a
contract that will be implemented differently by whoever does not have it.

## The measurement

On 6 September 2026, contract version 1, this claim was tested the only way it can be: by writing a
Tier 0 implementation in a third language — Go, chosen because no SDE library exists in it, so
nothing could be copied — from `format-contract.md` and `conformance/` alone, with the Python and
TypeScript sources deliberately not opened.

**Result: all 49 vectors passed on the first run.** Byte-exact IR, matching versions and shape
identifiers, and a signature verified against a payload canonicalised independently — which is the
strongest single check in the suite, because it agrees with an openssl-produced signature over bytes
this implementation had never seen.

So the encoding half of the document was sufficient. What was **not** sufficient was everything the
vectors did not reach, and that is where the value was. Six defects in shipped code, found by having
to make a decision the document did not make:

1. **A map's groups were validated in the document's own key order**, in both languages, so a
   document with two defects refused differently depending on the JSON parser's key handling.
   Measured in Go: `errors/019` failed in 5 of 20 runs. Both libraries already sorted the *routing*
   entries — the reason for sorting was understood in one loop and read as a detail in the other. Now
   §8a, and pinned from both sides by `errors/019` and `errors/035`.
2. **An absent `key` in the neutral form was defaulted to `["id"]`.** Python spelled the default `or`
   and TypeScript spelled it `??`, which differ on exactly one input: `"key": []` invented a key in
   one language and stayed keyless in the other — **one declaration, two model versions**, and no
   vector could see it because none declared an empty key. Worse than the divergence: a client who
   omits the key would be issued a map for a model with a primary key they never declared, and our
   own inference emits a declaration with no key on purpose and says in a note that the key is theirs
   to choose. Now refused; `errors/026`.
3. **`map_version` was optional and defaulted to 0** in both languages — the field the forward-only
   rule compares against a watermark in the client's own engine. Now required; `errors/034`.
4. **Four refusals lived at one of two entry points.** `pii` naming a non-field, a duplicate entity
   name, an entity with no fields, a repeated key column: each was enforced on the decorator path in
   one language, on neither path in the other, and on the vector path in neither. The vectors
   therefore ran a weaker validator than any application does, which is the suite's own stated
   failure mode. They now live where the IR is assembled; `errors/027` to `errors/033`.
5. **A rule that contradicted the contract.** TypeScript's `buildModel` refused a field and a
   relation sharing a name — a shape §2a explicitly calls intended, and which
   `hashing/003-reserved-object-keys` declares. That library refused a model its own conformance
   vector pins. The check is gone.
6. **`PlacementMap.contract` reported the library's version rather than the document's** in
   TypeScript, so a contract-1 map loaded by a contract-2 library said it declared 2.

A seventh came out of fixing the second one. The control plane takes a client's model as the neutral
document of §4a, and three of our own tests handed it a model's **IR** instead — a different document
that looks close enough — which nothing noticed because nothing read the stored file back. The
reference answered it with `TypeError: unhashable type: 'dict'` from inside the encoder. Both
libraries now have a way out of their own model type (`neutral_declaration`, `neutralDeclaration`),
so a client does not write their model twice, and the confusion has a named refusal: `errors/036`.

Eleven vectors were added for the rules that came out of this, and the first ten were mutation-tested
in **both** runners: twenty mutations, all of them fatal. The Go implementation was then pointed at
the amended document and the new vectors, and needed three corrections — one per class, all of them
worth having: a message that did not carry a vector's `match` string, a typed unmarshal whose parse
failures were the runtime's error rather than the contract's, and a runner that called the loader
outside the assertion and so could not pass a refusal the loader raises. The last two are in the
tables above now, because they are properties of statically typed languages rather than of this
format.

**What the measurement is not.** It is not an outsider. The isolation was of the source tree, not of
the person: the same engineer who maintains the reference implementation wrote it, and no discipline
about which files to open changes what somebody already knows. That is why the finding worth trusting
is not "all the vectors passed" — it is the list above, every item of which is a place where two
implementations had already made different choices, or would have. The honest version of the claim in
§16.7 of the requirements is therefore: **this document has now been implemented from twice, and the
second time cost six defects and eleven vectors.** Whoever does it third should expect to find more, and
that is the bug report we want most.

The experiment is repeatable and cheap — a day, one language, one file of vectors — and it is worth
repeating whenever this document changes in a way that adds a rule rather than a sentence.

### The second measurement, and why there was one

The turn that raised a second library to Tier 2 added three vector families and their rules, which
is exactly the trigger the paragraph above names. So it was done again the same day, in **Rust** —
chosen for the same reason Go was, that no SDE library exists in it, plus one it does not share: its
traits are statically dispatched, so a capability cannot be a missing method and the reference's way
of asking has no translation. Tier 0, Tier 1 and Tier 2, all nine families, from this page and
`format-contract.md` and `conformance/` alone.

**Result: 234 assertions, one red.** The red one was `errors/006`, whose `match` requires the
refusal to name the ceiling and got a message naming the range instead — a mistake on the
implementation's side, and the reason §7 now says the two directions are two refusals.

The value was again in what the vectors did not reach. Ten findings, and the seven about the suite
were each demonstrated by a mutation that **survived**:

1. **This page told an implementer Tier 1 and Tier 2 were unavailable**, in the closing section,
   for a release after their vectors were written — contradicting steps 9 to 11 of the same file and
   ending with "talk to us first", against requirement 16.7. The guard built for exactly this went
   quiet at exactly the wrong moment: it compared family *names* against the tree rather than the
   guide's claim about them, and both are consistent when the guide is wrong.
2. **The window document and the DDL had no prose anywhere.** Everything Tier 1 and Tier 2 need was
   reconstructed from fourteen vectors, which worked — and is the same gap §4a was written to close
   for the neutral declaration, with the same consequence, because the control plane parses a window
   to score a placement. They are §6a and §7a now.
3. **Two percentile conventions in one document.** The histogram took the ceiling and the
   cardinality took the floor. They agree on every sample set with an odd count, which every case in
   `telemetry/` had. `telemetry/007`.
4. **A failed read's zero rows were averaged into the result cardinality**, which understates what a
   read of that shape returns for a reason `error_share` already carries. `telemetry/008`.
5. **The ClickHouse identifier quoter escaped the backtick and not the backslash.** Inside backticks
   that lexer reads a backslash as an escape introducer, so a field called `a\nb` became a column
   called `a`, a newline and `b` — accepted in silence. Nothing in `schema/` carried a delimiter at
   all, so the escaping could be deleted outright and the family stayed green, and the live test
   stopped at "the server accepted it". `schema/011`, and the live test now reads the names back out
   of the catalogue.
6. **`partition_by` was in the format, emitted by the control plane, and rendered by nobody** — a
   layout declaring it produced an unpartitioned table and said nothing. The fifth field of this
   shape after `provisional`, `basis`, `in_use` and `key_id`. Refused on both sides now;
   `errors/037`.
7. **The live schema test's coverage depended on vector order.** One statement in the family — a
   ClickHouse layout rendered with the `postgres` dialect — had never executed, because an earlier
   vector created a table of the same name and `CREATE TABLE IF NOT EXISTS` never parsed the body.
   Cases get a schema each now and the unrunnable one says so.
8. **`verify` never had to match.** Both verify cases were failures, so `matched: false`
   unconditionally passed the family — the lesson the control plane had already written down for its
   own migration gate. `migration/017`.
9. **A fan-out to two copies was unexercised** on both the write path and the backfill path, so a
   library serving only the first `also_write` target passed. Both references were right; the
   vectors could not see it. `migration/018`, `migration/019`.
10. **Two smaller ones**: a compatibility view's refusal was undetermined for a non-PostgreSQL
    engine, because the only ClickHouse case had the old and new names equal (`schema/013`); and the
    table-order claim in `schema/001`'s own note was not exercised, because every layout in the
    family already writes its tables sorted (`schema/012`).

Nine vectors were added, one existing case gained a `runs: false` flag, and all of them were
mutation-tested in **both** runners: twenty mutations, every one fatal, and each killing only the
vector written for it.

**What this measurement is not** is the same as last time and worth repeating: not an outsider. The
isolation was of the source tree, not of the person, and this time it was weaker still — the same
engineer had written the three families being probed a few hours earlier. What survives that
weakness is the class of finding that does not depend on ignorance: a document that is silent, a
rule the language forces differently, and a mutation the suite does not notice. All ten above are
one of those three. Neither implementation is in this repository, and the reason is 17.6: publishing
either would be a claim of support nobody is maintaining, plus a fourth and fifth implementation to
keep in step with every change to this document.


### The third implementation: C++

On 6 October 2026 a C++ library reached Tier 0 with hashing, and unlike the Go and Rust measurements
it stays: it is a supported library ([`implementations.md`](implementations.md)). It is also not the
same kind of measurement. The reference's source was open the whole time, because a library that has
to give the same answers and the same messages on documents the vectors never show it is a port of
behaviour, not only of this document. What it measured is therefore where the reference's behaviour
is a rule, and where it is an accident of its runtime.

**Result: every vector of the claimed tier passed** - all of `canonical/`, `model/`, `routing/`,
`signature/` and `hashing/`, and the 76 `errors/` cases at the model and map stages. The other six are
at the session and write stages, which need engines; the runner passes them as outside the claim at
those two stages only, and fails on any stage it does not know. Thirty-one source mutations
followed. Four survived every vector and every unit test, and a fifth survived the unit tests because
each of their cases was also refused by a later check; all five are fatal now. The vectors could not
see these:

- entities of `key_order` and `partition_by` checked in document order, where §8a says name order;
- the upper bounds of an index's `granularity` and `max_rows` (only the lower ones are pinned);
- an uppercase `project_id`;
- an empty `partition_by` below contract 5, which the reference reads as absent;
- a write shape routed at a derived copy: §8 sends it to the source anyway, no vector routes one, and
  nothing refuses the entry at load;
- the shadowing refusal, which has no vector at all;
- padding inside a signature value's quantum, which Python refuses and TypeScript decodes.

**Where the two references coerce, or fail with their runtime's own error, this library refuses
with the contract's.** Each of these is accepted, or crashes, somewhere in the other two:

- a neutral `atomic` group that overlaps another, repeats a member or has one member;
- `nullable` that is not a boolean;
- a `cost_ceiling` that is not exactly `{amount, currency}` strings;
- a relation without `from` or `to`;
- a `residency` that is not a string;
- a materialisation `id` or `engine` that is not a string (`str(True)` is `"True"` in one language
  and `String(true)` is `"true"` in the other);
- a `lag_budget_ms` that is negative, fractional or a string;
- a layout table that is not a name, and `columns` that is not an object of type names;
- `derived` that is not a list;
- a materialisation that is not an object;
- a signed map whose payload has no canonical form, which both references let out of the loader as
  their canonical encoder's error rather than a map error.

Two smaller divergences between the references themselves came out of choosing which to follow:
- a signature block that is not an object gets a different message in each;
- a key set's names sort by code point in Python and by UTF-16 unit in TypeScript.

One finding is about the suite's own documentation. This page says a `match` is a substring
compared literally, both reference runners compile it as a regular expression, and four `errors/`
vectors write `Thing\.amount` - which only a regular expression reads as `Thing.amount`.

Each of these becomes a vector and a rule in every library in its own change. Until then, this
library is stricter on exactly these inputs, and says so in its README.

What the vectors do not exercise at all was compared with the reference directly, and agreed
everywhere:
- the full Unicode lowercase under an auto layout's table names, on all 1,112,064 scalar code points;
- the final sigma on 300,000 generated strings, against Python and JavaScript both;
- `snake_case` on 100,025 names against Python's and the TypeScript library's;
- signature base64 against Python 3.12's `b64decode(validate=True)` on 449,593 strings, every one up
  to six characters over a hostile alphabet among them;
- a value written into a message, against Python's `repr`, on 100,000 generated strings and numbers;
- the whole message of every refusal at the model and map stages, all 76 of them, against the
  reference's own: identical, apart from one function name spelled the C++ way.

#### Tier 2, and the packets no vector signs again

On 7 October 2026 the C++ library passed every `schema/`, `query/` and `migration/` vector, and the
`errors/` cases at the session and write stages, and claimed Tier 2 before it had an engine adapter:
the tier is the vectors of §9, and [`implementations.md`](implementations.md) keeps the engines in a
column of their own. Its runner checks a refusal's exact class, where both reference runners accept a
subclass.

The signed packets of §7g, §7h and §7j carry their own refusal order, and a vector can pin only one
defect at a time. So the three loaders were compared with the reference's on 102,384 packets: every
accepted packet vector, changed at each of its paths - a member removed, another type or a
neighbouring value put in its place, a member added - and in random pairs, so that two defects meet
and the order between them is compared too. A change inside a candidate map signs that map and then
the packet again with a key made for the run, so it reaches the rule it is about instead of stopping
at a signature. Outcome, fingerprint, record, epochs, payload and the whole message agreed in every
case outside three classes:

- **this library's map loader is stricter**, as the previous section lists. The reference accepted
  342 staging packets whose prepared copy declares a column type that is not a type name (`null`,
  `true`, `{}`), an engine that is not a string or a lag budget that is not a non-negative integer,
  and an operator would have been handed each of them;
- **the reference failed with its runtime's own error**: its cutover loader let 1,704 `TypeError`s,
  `ValueError`s and `AttributeError`s from a malformed layout out as themselves, its staging and
  index loaders 819 `AttributeError`s, and in 3,567 more cases those two put the runtime's text into
  the refusal (`'int' object is not iterable`);
- **an integer past 64 bits**, which §1 requires a language with 64-bit integers to refuse rather
  than truncate, where the reference went on to the packet's next rule, or accepted it (21 times).

One divergence between the two references came out of it. The reference's staging refusals that
come from helpers it shares with the cutover packet name a cutover - `cutover stage_id must be 32
lowercase hexadecimal digits` for a staging - and TypeScript's name a staging. The staging vectors
pin the class only, apart from two, so neither runner saw it. This library names the packet it
refuses, as TypeScript does.

Mutation testing followed: 121 source mutations of backfill, verification, its requests, the
comparison under barriers and the three packets, each a rule removed or loosened, and three controls
that must survive. The first run left 37 alive, and every one was a rule nothing reached. Most
staging vectors pin their class only, and several rules refuse an input that a later rule refuses
as well, with a message of its own, so removing either one left the class right. Each now has a unit test -
ported from the reference's own tests where it has one - and kills its mutation, except two that
no input reaches: a cutover's check that its one copy is written to, behind the verification
request, which already refuses a map without that fan-out; and its check of the terminal map's
engine, behind the comparison of the whole materialisation, engine included. Both stay, as in the
reference. One rule changed on the way: the comparison under barriers asked an engine to check its
tables with the keys, so a real adapter would also have read the physical design; it now checks the
columns alone, as the reference does.

#### The PostgreSQL adapter, against the reference on one server

The first engine adapter, PostgreSQL over libpq, came the same day. Its live tests are the
reference's PostgreSQL slice ported test by test, and wherever one compares a message the expected
text was captured from the reference's adapter against the same server. The cases the reference does
not test - fence DDL inside a transaction, a table with inheritance and a view, the reserved column
made by hand in five other definitions, a group whose table is gone - ran against it the same way,
and agreed in class and text. Two things for whoever writes the next adapter over libpq:

- **the driver's defaults are part of the reference's behaviour.** psycopg gives the adapter a
  connection timeout the DSN can override, UTF-8 whatever the DSN says, the server's message without
  its severity, a class name for each SQLSTATE in `sde.write.failed`, and silence about a server's
  notices. libpq prints every notice on stderr unless told otherwise, and every schema statement
  after an application's first start raises one;
- **the reference's own test of a barrier's wait cannot see the drain's lock.** The first freeze waits
  for writers in its `ALTER TABLE`, so removing the `LOCK TABLE` leaves that test green. The lock is
  what proves the table quiet on a retry, when the constraint exists, no DDL runs, and a delete - which
  no `CHECK` refuses - still holds the table. The C++ suite tests that case, the reference's adapter
  waits in it too, and the same test is a finding for the other two libraries;
- **the reference applies its connection bound when the DSN does not contain the word.** It tests
  `"connect_timeout" not in dsn`, so a DSN that merely mentions it - in an application name, a
  password - loses the bound: against a host that accepts and never answers, measured, its connect
  was still waiting after 20 s, where the plain DSN gave up at 10. TypeScript matches the parameter,
  and this library hands libpq its default before the DSN, so only a `connect_timeout` the DSN sets
  overrides it; a live test holds the mentioned case at ten seconds.

#### The ClickHouse adapter, against the reference on one server

The second adapter, ClickHouse over its HTTP interface through libcurl, is ported the same way: the
connection URI first, compared with the reference's parser on 102,000 generated URIs - outcome,
message and every field equal, the timeouts to the bit - and then the adapter, whose every compared
message was captured from the reference against the same server. Three things for whoever writes
the next one:

- **the driver's settings are part of the reference's behaviour.** clickhouse-connect runs every
  exchange in a UTC session with ISO output, keeps a silent connection alive with progress headers
  and never resends an exchange. An adapter on plain HTTP has to ask for each of these on every
  request, or a server's time zone moves a moment and a lost reply becomes a second insert;
- **text formats let the server change a value.** The reference's driver moves rows in ClickHouse's
  binary Native format. Rows sent as JSON are clamped by the server to the range it prints -
  measured on ClickHouse 24.8, a `Date32` of year 1 is stored as 1900-01-01 and a `DateTime64` of
  year 9999 as 2299-12-31, without an error - so this library uses `RowBinaryWithNamesAndTypes` both
  ways;
- **a decimal with more digits than its column is changed silently, and differently per engine.**
  Measured through the reference's adapters on 7 October: `Decimal("1.239")` saved into a
  `decimal(12,2)` field is stored as 1.23 in ClickHouse, truncated by the driver, and as 1.24 in
  PostgreSQL, rounded by the server - one save, two values - and `Decimal("12345678901.23")` is
  stored in ClickHouse as 12345678901.20, where PostgreSQL refuses it with `numeric field
  overflow`. This library's ClickHouse adapter refuses both before sending anything. The fix that
  makes the engines agree belongs to the contract: admission (§8b) refusing a decimal with more
  fractional or integer digits than its field declares, in every library. The same driver cuts a
  moment's fraction a `DateTime` or `DateTime64(3)` does not keep, stores an enum name its type
  does not declare as 0 - a row no later read can print - and on reading cuts nanoseconds; none of
  these is in a layout this library renders, all of them are in a table a hand-written map can
  name;
- **the statement is the request's body.** The reference's driver sends it there, and only an
  INSERT that carries rows names its statement - which holds no value - in the URL. A URL is what a
  proxy between the application and the server logs: the TypeScript library sends every statement
  in one, a get's key and a read's filters with it, and so did this one until a test read the
  request line;
- **a drain's intent is written synchronously whatever the login's defaults.** A login can make
  inserts asynchronous and unacknowledged by default; the intent row has to be in the table before
  the DETACH it describes, or an interrupted drain has no record to resume by. The insert carries
  `async_insert=0` and `wait_for_async_insert=1` itself, and a test holds it with a login whose
  defaults say the opposite;
- **a catalogue's refusal is the server's code, read where the code is.** The reference tells a
  login refused a size from an engine that failed by its driver's error - SQLSTATE 42501, code 497.
  This library looked for "42501" in the adapter's message, which carries the server's words and
  never the code, and reported every PostgreSQL refusal as a failure; its unit test passed because
  the fake put the code in the text. The adapter now says it by an error's type;
- **libcurl checks a certificate's host name after the handshake.** Python's `ssl` checks it
  during, so a server sees a TLS alert from the reference and none from this library for another
  host's certificate. libcurl 8.18 checks an expired chain and an unknown CA after it as well, where
  8.5 checks them during. What all of them guarantee - no application byte reaches an unverified
  peer - is the claim to test;
- **the catalogue's text is a server release's.** From ClickHouse 26.5 the catalogue keeps the
  parentheses of a single expression written in them: the one-column sort key every library renders
  as `ORDER BY (id)` reads back `(id)` in `system.tables`, and a partition key or a skipping index's
  expression written in parentheses likewise, where 26.4 and every earlier release - 24.8, 25.3,
  25.8, 25.12, 26.3 measured - read `id`; a list of two or more reads `a, b` on all of them. The
  reference's catalogue parser refuses the parentheses, so on 26.5 every table it created reads as
  another design: a session reports a physical mismatch for each, and provisioning, staging and a
  copy under a new generation refuse - its own slice fails there, and so did 30 live tests of this
  library on 26.9 before its parser took one pair of parentheses around a whole list. The same
  release changed the words of a refused read (`identifier 'missing'. In scope` for `'missing' in
  scope`), so a test that compares a server's words asks the server for them. CI tested 24.8 alone;
  it now runs this library's live tests against a current release too.

#### The orderbook adapter, over the engine's own protocol

The third adapter speaks our engine's text protocol itself, over TCP or TLS 1.3, as the TypeScript
one does: the engine ships a Python client and no C++ one, and a library that linked the engine
would put the engine's code into every client that places nothing there. The reference does not
speak the protocol at all - it calls the engine's Python client - so for this adapter **the
reference is two programs**: the adapter, whose decisions and words this one ports, and the client
it calls, whose framing, timeouts and words the adapter puts into its own messages (`query failed:
…: TCP recv timeout`). Both were read at the engine commit the CI job pins, and every expected text
was captured from them: the DSN against the reference's parser on 280,000 generated DSNs, the
answer and STATUS parsing against the client's own functions, the connection failures - TLS among
them - against the reference connecting to the same servers. What whoever writes the next one
needs:

- **the DSN's numbers are Python's.** The timeout is `float()` of text and the client reads every
  number of an answer with `int()`, so `1_0` is ten seconds, an Arabic-Indic three is three, `inf`
  is accepted, and U+001C to U+001F - which `str.isspace()` calls whitespace - are not stripped,
  because `float()` strips C's whitespace once Python's has become a space. A port that reads them
  with its own parser agrees on every DSN a person writes and disagrees on the ones a generator
  writes; this one disagreed twice before the differential found it, the second time on a
  four-hundred-digit mantissa no corpus had;
- **every wait is bounded by silence, not by the answer.** The client sets one socket timeout, so a
  slow answer that keeps arriving is waited for. TypeScript bounds each answer as a whole;
- **an exchange that does not finish closes the connection, and the reason is kept.** The rest of
  its answer may still arrive, and the next command would read it as its own (the engine's #171).
  The reference keeps the closed client and `connect()` does nothing until `close()`; this library
  says the reason on every call in between and lets `connect()` open another;
- **a write that cannot be sent is not a write that may have been stored.** The reference reads its
  client inside the write's own failure, so it reports a write it never sent as one whose outcome is
  unknown. This library refuses it first, as TypeScript does;
- **the client's limits are not the contract's.** It opens IPv4 sockets only, so a DSN with an IPv6
  address that its parser accepts cannot connect, and a timeout its parser accepts can still be one
  CPython cannot wait for, refused only at connect. This library connects to either family and
  reproduces the second, which is a rule for the DSN in every library;
- **TLS refusals are CPython's words without CPython's line.** `[SSL: CERTIFICATE_VERIFY_FAILED]
  certificate verify failed: Hostname mismatch, certificate is not valid for 'host'.`, an end of the
  stream inside the handshake as `EOF occurred in violation of protocol`, a reset as the socket's
  `[Errno 104]`; CPython appends `(_ssl.c:1000)`, which names its own source.

Each difference from the reference or from TypeScript is a finding for the contract, to become a
rule in every library: the DSN's grammar as vectors, a symbol's characters (TypeScript accepts an
invisible format character the reference refuses), a key of another type in a point read
(TypeScript answers "no row" where the reference reads it with `int()`), and the four above.

### Verification-request protocol check

On 12 September 2026 a standalone Go checker implemented section 7b from its description and
vectors, without importing SDE source. Its binding and refusal decisions matched all 24 new
verification-request cases, including integral JSON numbers and a map signature checked with Go's
Ed25519 implementation. This was a protocol-only measurement on the ASCII map fixtures, not a new
SDK, a Tier claim or a proof that arbitrary Unicode canonicalization had been implemented. The same
engineer performed it; the separation concerns the implementation and source imports, not the person.

The same change was probed with 26 deliberate source mutations across the two reference SDKs.
All were detected, with a named failing assertion for every new verification vector. The probes
removed session/project binding, time and shape checks, altered signature hashing and matching
results, and allowed mutation or copying of a loaded map's provenance. Baselines passed before and
after exact source restoration. Local wheel and npm tarball installations also exercised the new
root exports, map binding and wrong-project refusal outside the source checkout; this was a local
artifact check, not a registry publication or a run of the release workflow.

The optional [signed cutover packet](cutover-packets.md) is an authorization envelope over existing
map and verification contracts. Its current-map check preserves signed admission mode, not only
the payload fingerprint. `migration/079`–`098` exercise both accepted packets and named refusals;
implementing this decoder does not imply a durable local executor is available.


**12. Logical reads and exact summaries (Tier 2). Vectors: `query/`.**

Tier 2 implementations offering the logical read API run the `query/` family in addition to
schema/migration vectors. It pins normalized filters, typed positions, bounds and exact decimal
summary results. Native tests must separately execute the generated SQL against each supported
engine; a normalized plan alone does not prove ordering, null handling or arithmetic in that engine.
The query family does not change the model/map bytes or migration checkpoint ordering.

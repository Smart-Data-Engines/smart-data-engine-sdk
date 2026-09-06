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
- **Anything about Tier 1 or Tier 2 behaviour that the vectors do not cover.** Three of the vector
  sets §9's table names do not exist yet: `telemetry/`, `schema/` and `migration/`. Until they do, a
  Tier 1 or Tier 2 claim is not something the vectors can check, which is stated in §10 rather than
  left to be discovered from an empty directory. If you are going there, talk to us first — not
  because we want to approve it, but because the vectors get written before the second claim is
  accepted rather than after.

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
second time cost six defects and ten vectors.** Whoever does it third should expect to find more, and
that is the bug report we want most.

The experiment is repeatable and cheap — a day, one language, one file of vectors — and it is worth
repeating whenever this document changes in a way that adds a rule rather than a sentence.

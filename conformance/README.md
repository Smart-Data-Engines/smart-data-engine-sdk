# Conformance vectors

Every SDE library runs these, in its own test runner, in its own CI. That is the whole mechanism by
which four implementations stay identical: a divergence between Python and Java becomes a red test for
whoever caused it, instead of an operation written to the wrong engine in somebody's production.

## Layout

```
contract-version.txt          the format contract version these vectors describe
vectors/
  model/<nnn>-<name>/
    model.json                a model declared in neutral JSON
    ir.json                   the exact canonical IR bytes it must produce
    version.txt               the exact model_version
    groups.json               the colocation groups, in order
    shapes.json               every operation shape, with its identifier - optional, and absent
                              from 001, which predates it
  routing/<nnn>-<name>/
    model.json
    map.json                  a placement map
    cases.json                (shape, in a write transaction?, needs freshness?) -> materialisation
  errors/<nnn>-<name>/
    model.json
    map.json                  for the `map` and `session` stage cases
    engines.json              the engine set to build, for `session` cases - the same document
                              the migration family uses, and `dialect` is the field that matters
    calls.json                for `session` cases: what the library was allowed to do to those
                              engines on its way to refusing, which so far is nothing
    keys.json                 the public keys the caller holds - only where a case needs one
    expected.json             which error, at what point it must be raised, and optionally a
                              `load` block of arguments the runner must pass to the loader
  signature/<nnn>-<name>/
    model.json
    map.json                  a signed placement map
    keys.json                 the public keys the caller holds, by the caller's own names
    expected.json             which key verified it, or which error it must raise
  canonical/<nnn>-<name>/
    value.json                a value fed straight to the encoder
    bytes.json                the exact bytes it must produce
    expected.json             for cases that must be refused instead
    why.txt                   what would break if this vector were not here
  schema/<nnn>-<name>/
    model.json
    map.json                  a placement map, so the layout reaches the renderer through the
                              same loader production uses rather than a second parser
    cases.json                (materialisation, dialect) -> the exact DDL, or the refusal
  migration/<nnn>-<name>/
    model.json
    map.json                  signed for the forward-only cases, because only a signed map is
                              checked - an unsigned one is the client's own document
    keys.json, load.json      the public key the caller holds, and how to load with it
    engines.json              the engine set: tables, markers, watermarks, and which capabilities
                              each adapter has
    watermark.json            the forward-only decision, or the refusal
    backfill.json             the progress record, or the refusal
    verify.json               the seven counts, and the differences that stay on the client's side
    calls.json                every call each engine received, in order
    why.json
  telemetry/<nnn>-<name>/
    model.json
    operations.json           operations to record, each naming a shape by identifier
    fan_out.json              writes to a derived copy - optional
    window.json               the exact window document the library must produce
    features_for.json         features for a group with no traffic, which is not in the document
    buckets.json              (nanoseconds, bucket index) fed straight to the histogram
    percentiles.json          the upper edge each bucket reports
    expected.json             for the case that must be refused instead
    why.json                  what would break if this vector were not here
```

`schema/` is Tier 2 and compares statements **exactly**, because a statement is bytes a server
receives. Two conventions of its own: a case pins `fixed`, which is the answer to "does this engine
take DDL from us at all" and is what distinguishes no statements from no tables; and `views`, whose
`create` and `drop` are exact while its `not_possible` reasons are pinned as a **list** of
substrings, because the useful reasons carry two claims - which name moved, and what a query has to
become - and one substring cannot straddle both.

The statements in this family are also executed against a real PostgreSQL and a real ClickHouse, in
`python/tests/test_schema_vectors_live.py`, twice each. That is not belt and braces: a vector holding
DDL no server accepts would be a frozen mistake every future implementation is *required* to
reproduce, and the suite's authority is exactly what makes it dangerous when wrong.

`telemetry/` is Tier 1 and it is the **one family compared parsed rather than as bytes**. Section 1
of the contract rejects floating point outright, because a float's textual form differs between
languages - and a window document is almost entirely floats. It is not signed, not hashed and never
compared for equality, so that rule does not bind it; what makes the family checkable is narrower:
every number in a window is either a ratio of two integers or a bucket edge divided by a million,
and IEEE 754 requires division to be correctly rounded, so two languages compute the same double
even where they print it differently.

Two things it deliberately leaves out. **A clock**: the document carries none, which is what makes
it deterministic, and the two features that would need a duration are declared unmeasurable anyway.
And **buffer eviction**: `dropped_windows` is in the document and zero in every case, because a full
buffer dropping its oldest window is behaviour with no artefact - each library asserts it directly,
and the two assertions are the only thing holding those two implementations together.

`migration/` is Tier 2's second half and the only family that pins **the calls a library makes** as
well as the answer it reaches. A library that arrived at the same counts by scanning the whole table
and filtering in memory would satisfy every number and be unusable against a real one, so the
sequence is the part that says *how*. The fixture is each library's own in-memory engine, in its
`testing` package rather than in its runner: a runner that writes its own is a runner whose fixture
can be the thing that differs, and a red vector would then say "one of two tables disagreed".

Read `001` first. Its expected call list is **empty**, because the no-account mode promises no
table, no query and no cost - and the TypeScript port gathered every engine's watermark and *then*
noticed the map was unsigned. The right answer with the promise broken, which is the one shape of
defect a record of the decision cannot show.

One case here is worth reading before adding to any family: `002` pins bucket boundaries, and the
alternative implementation - a logarithm - **passes all of them**, because glibc's `log2` and V8's
are both exact at a power of two. A property no output can distinguish on the machines available is
not one a vector can hold, and pretending otherwise is worse than admitting it: both libraries check
that one statically instead, over their own source.

`canonical/` is the newest kind and the most instructive. It exists because a mutation that should
have failed did not: every object key in the model IR is fixed ASCII, so no model vector reaches the
object-key comparator, and swapping code point ordering for a naive sort passed the whole suite.
Field names do reach the IR - as array elements, through a different comparator. Two call sites, one
covered.

The lesson generalises, and it is worth applying to any vector added here: **break the code
deliberately and check that this suite notices.** A vector that passes without reaching the code it
describes takes the place of one that would have.

**Which files are bytes and which are text.** `ir.json` and `bytes.json` hold bytes and have **no
trailing newline**: compare them exactly. The `.txt` files - `version.txt`, `salt.hex` and the rest -
are text and do end with one, so strip it. That is not a rule anybody would guess, and a runner that
gets it wrong fails with a diff nobody can see.

**`expected.json` may carry a `load` block, and one case does.** It is arguments for the loader
rather than anything about the refusal: `errors/015` sets `require_signature` there, because the map
in it is unsigned and §7 says an unsigned map is *accepted* — so without the block the case is
unpassable and its natural reading is that the vector is wrong. One vector in thirty-seven needs it,
which is exactly why it is written down here: a runner built from this file and not from the tree
fails that one case with "expected MapError, got no refusal" and no clue why. Treat an absent block
as no arguments.

**`expected.json`'s `match` is a required substring of your refusal's own message**, compared
literally and case-sensitively. Diagnostics are part of the contract here, which is deliberate: a
refusal of an incompatible contract version once rendered a literal `{CONTRACT}` in one language and
the number in the other, and no vector reached it because the suite compares encodings and that path
produces only a diagnostic. Say more than `match` if you like, in any language you like; not less.

`ir.json` holds bytes, not a document to be re-parsed. Compare it as bytes. A library that parses it
and compares the parsed structures is not testing the thing that breaks - two libraries agreeing on
the *structure* while disagreeing on key order or Unicode normalisation is exactly the failure these
vectors exist to catch, and it is invisible after parsing.

## How a library uses them

1. Read `model.json` and build your own model type from it. Every library needs a loader for this;
   in Python it is `sde.testing.loader`. It is a requirement rather than a convenience - without it
   the vectors could not be shared, and unshared vectors verify nothing.
2. Assert your canonical IR equals `ir.json` **byte for byte**.
3. Assert your `model_version` equals `version.txt`.
4. Assert your groups and shapes equal `groups.json` and `shapes.json`, in order.
5. For routing vectors, load `map.json` and assert each case resolves as `cases.json` says.
6. For error vectors, assert the error is raised, and raised at the stage `expected.json` names -
   a library that raises the right error at the wrong time has a different bug, not the same one.
   The `model` stage is the loader **and** the model builder: a refusal about the shape of the
   declaration comes out of the loader, so calling it outside the assertion makes those cases
   unpassable. A `map` case is the other way round - its model must build, outside the assertion.
   A `session` case builds both **and** loads the map outside the assertion, and only the session
   construction is expected to fail: the document is valid, and what is refused is what it asks two
   adapters to do. Tier 2 only. If your runner does not know a stage, **fail rather than skip** -
   a stage nobody runs is a rule nobody checks, and skipping reads as coverage in the summary.
   Where a `session` case carries `calls.json`, compare it: the refusal is only half the claim, and
   the other half is that nothing was created, read or written before it.

7. For signature vectors, load `map.json` with the keys in `keys.json` and assert which one
   verified it. A single entry under the **empty** name means the caller passed one bare key and
   the library reports no name back; that is a different call from a one-entry mapping, and the
   difference is the whole reason this family exists. `key_id` in the map orders the attempts and
   decides nothing - vector `003` is signed with one key and says another.

## Where the expected values came from

`model/001-single-entity` is **hand-written**. Its IR was typed out by a person from the rules in
`docs/format-contract.md`, not produced by any implementation, and it is the vector that proves the
document is sufficient to implement from. If the reference implementation ever disagrees with it, the
implementation is wrong until somebody argues otherwise in writing.

`signature/` is the second family without that limitation, and for the same reason as `hashing/`:
every signature in it was produced **by openssl**, which shares no code with either library, and the
generator refuses to write a file whose signature openssl will not verify. What still comes from this
implementation is the *payload* - the canonical encoding of the map without its signature block -
because openssl cannot compute our canonical form. That part is pinned separately and harder, by
`model/001-single-entity`. No private key is committed: each pair is generated in a temporary
directory and discarded, so regenerating means new keys and new signatures, which costs nothing
because a signature is not one of the artefacts that has to stay byte-stable.

The rest were generated from the Python implementation and reviewed by hand. That is honest but
weaker: a bug in the reference implementation would have been frozen into them. So the rule is that
**001 is authoritative for the encoding**, and the generated vectors pin behaviour that 001 does not
reach - composite keys, unicode in identifiers, the full type vocabulary, routing.

Once committed, a vector is frozen. Changing one is changing the contract, which means bumping
`contract-version.txt`, and every library declaring which version it implements. There is no such
thing as fixing a vector quietly: a vector that was wrong was a contract that was wrong, and somebody
may have stored a placement map against it.

## Where these came from, part two

Eleven of the `errors/` vectors - `026` through `036` - were added on 6 September 2026 after a Tier 0
implementation was written in Go from `docs/format-contract.md` and this directory alone, to find out
whether that document is sufficient to implement from without asking us. It is, for the encoding: all
49 vectors passed on the first run. It was not for anything the vectors did not reach, and the
eleven new ones are the rules that came out of that - six of which were defects in the two libraries above,
including a map whose groups were validated in the document's own key order, so one document refused
differently depending on the JSON parser. `docs/implementing.md` has the whole list.

Two of them are worth knowing about as a pair. `errors/019` and `errors/035` carry the same two
defects in the same document with the group keys written in opposite orders, and they pin that the
refusal does not depend on which order a parser hands them over in. `019` alone passed in both of our
languages because both iterate an object in insertion order, and failed in Go - whose maps are
iterated in a randomised order - in 5 of 20 runs.

## Where these came from, part three

`errors/038` and `migration/020`-`021` were added on 7 September 2026, the day a defect in **shipped
code** was found by running the migration phases through an operator's command line rather than by a
test. The write fan-out was truncating a `timestamptz` on every write - measured on live servers as
`09:30:15.123456` from PostgreSQL and `09:30:15.123` from ClickHouse for one row written once - one
phase *before* the gate that refuses the same copy. So the gate stopped the migration finishing and
did not stop the loss.

It was fixed in both languages the same day and held by a test in each, and that is the state these
three vectors exist to end. A rule enforced in one runtime and not the other is one map with two
meanings, and per-language tests are exactly what that looks like from the inside: green, twice.

The stage is new because neither of the other two can reach the rule. A map names engines **by
name** and carries no dialect, so at load time the question has no answer; the earliest door that
can answer holds the adapters. Hence `stage: session`, Tier 2 only, and a runner that meets a stage
it does not know must fail rather than skip.

**Then, later the same day, the pair the rule was written for stopped existing** - and what these
three cases now pin is the result of that rather than the original defect. The three digits were a
choice this project had made, against a comparison with ClickHouse's plain `DateTime` (which is
second-resolution) rather than with the PostgreSQL beside it. Keeping the refusal would have meant
that no group with a time column could have a ClickHouse copy, which is the shape the product is
sold on. Both dialects render `DateTime64(6)` now, no pair of the three shipped here truncates, and
the truncating branch has no reachable case.

So `errors/038` pins the branch that is left and the one a fourth adapter meets on its first day: a
dialect with **no precision facts recorded**, refused rather than guessed at. `migration/020` is a
fan-out between two engines of one dialect; `migration/021` is PostgreSQL to ClickHouse - the
central shape - and it exists so that nobody can quietly render three digits again.

Two things about `038` are worth copying if you write a case like it. Its `calls.json` is **empty**,
because a map that can never work must not create a table or issue a query on the way to being
rejected - and its map is **signed** for that exact reason: against an unsigned map the forward-only
check does nothing at all, so an unsigned case would satisfy an empty call list whichever way round
the two checks ran, and the assertion would be decoration.

And the rewrite itself is the third thing. When a fix removes the reason a refusal existed, the
cases that pinned it start passing for the wrong reason - they become cases that cannot fail. Point
them at the branch that survived, say in the document which branch is now unreachable, and do not
delete the rule: it is the ratchet for the day somebody adds a dialect.


## Adding a vector

Add the case that a bug taught you, not the case that was easy to write. Every vector here should be
traceable to a way two implementations could plausibly disagree: number formatting, string
normalisation, key ordering, sort stability, the boundary between a type and a value.


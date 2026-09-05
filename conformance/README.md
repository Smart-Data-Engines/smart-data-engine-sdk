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
    shapes.json               every operation shape, with its identifier
  routing/<nnn>-<name>/
    model.json
    map.json                  a placement map
    cases.json                (shape, in a write transaction?, needs freshness?) -> materialisation
  errors/<nnn>-<name>/
    model.json
    expected.json             which error, and at what point it must be raised
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
```

`canonical/` is the newest kind and the most instructive. It exists because a mutation that should
have failed did not: every object key in the model IR is fixed ASCII, so no model vector reaches the
object-key comparator, and swapping code point ordering for a naive sort passed the whole suite.
Field names do reach the IR - as array elements, through a different comparator. Two call sites, one
covered.

The lesson generalises, and it is worth applying to any vector added here: **break the code
deliberately and check that this suite notices.** A vector that passes without reaching the code it
describes takes the place of one that would have.

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

## Adding a vector

Add the case that a bug taught you, not the case that was easy to write. Every vector here should be
traceable to a way two implementations could plausibly disagree: number formatting, string
normalisation, key ordering, sort stability, the boundary between a type and a value.

# SDE implementations

Which libraries exist, who keeps them working, and exactly what each one does. Requirement 17.6 asks
for this list to be public and current, because writing "we support Java" when it means something
different from what it means for Python is misleading in the direction that costs a client a
migration.

Two words, and the difference is who fixes it:

- **Supported by us.** We wrote it, we run its conformance suite in our own CI on every change to the
  format, and a defect in it is ours. If we stop maintaining one, it moves down this page rather than
  disappearing from it.
- **Community.** Somebody else wrote it. It passes the Tier 0 vectors — that is the condition for
  being on this page at all, and the one thing we do not negotiate — and everything after that,
  including whether it still passes next month, belongs to its author. We link it; we do not vouch
  for it.

Tiers are defined in [`format-contract.md` §9](format-contract.md). Tier 0 is not optional: an
implementation that does not pass the Tier 0 vectors is not an SDE library, whoever wrote it.

## Supported by us

| Library | Language | Tier | Hashing (§2a) | IR contract | Map contract | Engines |
|---|---|---|---|---|---|---|
| `smart-data-engine-sdk` | Python 3.11–3.13 | 2 | yes | 1 | 1–3 | `clickhouse`, `orderbook`, `postgres` |
| `@smart-data-engines/sde` | TypeScript / Node 18–22 | 2 | yes | 1 | 1–3 | `clickhouse`, `postgres` |

The engines column carries **dialect identifiers**, not product names: they are what a hand-written
layout and `schema_statements(dialect=...)` take, so they are the spelling a client actually types.
Requirement 17.5 is where the real cost of this table lives: four languages times two engines is
eight driver integrations, and each of them needs its own transaction-semantics tests. Two of the
eight exist per row above. `postgres` is PostgreSQL and `orderbook` is our own L2 orderbook engine,
whose schema is fixed in its own source — a group either is that shape or it cannot be placed there, and rendering DDL for it
yields no statements rather than no tables.

**Python is the reference implementation.** Where it disagrees with the contract, the contract is
right and the library is wrong — with one exception, stated in the contract itself:
`model/001-single-entity` was written by hand from the rules and is authoritative for the encoding,
so if the reference disagrees with *it*, the reference is wrong until somebody argues otherwise in
writing.

**TypeScript reached Tier 2 on 6 September 2026, and the vectors for those tiers were written
first.** Section 10 of the contract says the vectors for a tier are written before a second library
claims it, not after — so `telemetry/`, `schema/` and `migration/` exist because of this claim rather
than alongside it, and closing that gap found five defects in the reference implementation. Its
second purpose remains structural: the contract's neutrality is checked by a second implementation,
not by a fourth, and several rules in it exist because these two disagreed.

Three differences from the reference are worth knowing before you depend on it, and all three are
decisions rather than gaps.

**Its Tier 2 surface is asynchronous, and everything below Tier 2 is not.** Node's I/O is
asynchronous, so a synchronous wrapper around a driver blocks the event loop — a worse thing to do to
a client's process than a promise in a signature. The split follows the tiers exactly: nothing up to
and including telemetry touches a socket. A session is *opened* rather than constructed, because the
forward-only map check reads a table, so a caller cannot hold one whose check has not run.

**Its ClickHouse adapter has no driver dependency.** The official Node client requires Node 20 and
this package supports 18 to 22 with all three in CI; dropping 18 would narrow a published claim to
gain a dependency, and pinning a superseded client version is the conflict the zero-dependency rule
exists to avoid. ClickHouse's HTTP interface needs no client. One consequence is an improvement: the
adapter owns both of its timeouts rather than inheriting a driver's defaults, which is how the
reference implementation came to discover that its own ClickHouse connect timeout never fired.

**It has no `explain`.** Validating an analyst's SQL against a live engine (requirement 19.4) is a
Python feature today. Nothing about it is language-specific; it is simply not written here, and a
table that implied otherwise is the kind of claim requirement 17.6 exists to prevent.

**Its timestamp values retain microseconds.** Both adapters return immutable `Timestamp` values
rather than JavaScript `Date`; a `Date` remains accepted on writes. This matters when a Python
service writes the data and TypeScript reads or migrates it: the previous conversion lost three
digits and could report a corrupt copy as matching. [Exact timestamps](timestamps.md) documents the
API change and the live tests with an independent Python writer and reader.

The tier in the table above is checked against the library's own `TIER` constant by a test, because a
list that says one thing while the code says another is the failure requirement 17.6 exists to
prevent, and prose does not fail.

**`pip install smart-data-engine-sdk` installs this library**, published to PyPI on 12 September
2026. `npm install @smart-data-engines/sde` does not install anything yet: the scope is ours, held by
the `smart-data-engines` organisation from the same day, so that name cannot become somebody else's
package — but on npm a scope is a reservation and the package under it is a separate act, and the
first publish there will come from CI with provenance rather than from a laptop.

The suffix is not decoration. `smart-data-engine` is **refused** by PyPI as "too similar to an
existing project" — `smartdata-engine`, registered by somebody else with no releases — so the
distribution could not have that name even if we wanted it. The reasoning is in
[`publishing.md`](publishing.md#2-pypi--the-name-is-claimed-by-an-upload-and-by-nothing-else).

## Community

None yet.

If you are writing one, [`implementing.md`](implementing.md) is the guide, and it is meant to be
enough on its own. If it is not, that is a bug in our documents and the most useful thing you can
send us.

To get listed, open a pull request against this page with: the language and runtime versions, the
tier and — for Tier 2 — the engines it round-trips, whether it offers hashing, the contract range it
implements, a link to a CI run of the conformance suite, and who to contact when it breaks. We will
not ask you to change your API: the contract is identical, the ergonomics are idiomatic, and a
library that reads like transliterated Python is a worse library in your language and no better an
SDE one.

## Not started

Java, Rust, C# / .NET, Go, Kotlin, PHP and Ruby, in roughly that order of demand. None of them is
claimed and none is in progress. That sentence is read by a test: each language in it needs a row in
[`publishing.md`](publishing.md) saying how a name is claimed in its registry, because the nine
registries disagree about whether a name can be held before there is code, and an eighth language
added here without that row is a question nobody can answer from our documents. The cost of the *n*-th library is the cost of one implementation of
this contract rather than another version of the product, which is the whole argument for the
contract being a byte-level document — but it is still a cost, and a library nobody keeps working is
worse for a client than no library in that language.

Two implementations were written as **measurements** and are deliberately **not** on this page and
not in this repository: a Tier 0 one in **Go** and, once the Tier 1 and Tier 2 vectors existed, a
Tier 0 + Tier 1 + Tier 2 one in **Rust**. Both dates are 6 September 2026. Their purpose was to find
out whether `format-contract.md` is sufficient to implement from without asking us; the first found
six defects in the two libraries above and the second found ten. Publishing either would be a
support claim we cannot keep, and a fourth and fifth implementation to hold in sync with every
contract change. What they produced is written down in
[`implementing.md`](implementing.md#the-measurement).

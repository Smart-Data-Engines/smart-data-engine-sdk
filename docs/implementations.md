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
| `smart-data-engine` | Python 3.11–3.13 | 2 | yes | 1 | 1–2 | `clickhouse`, `orderbook`, `postgres` |
| `@smart-data-engines/sde` | TypeScript / Node 18–22 | 0 | yes | 1 | 1–2 | none |

The engines column carries **dialect identifiers**, not product names: they are what a hand-written
layout and `schema_statements(dialect=...)` take, so they are the spelling a client actually types.
`postgres` is PostgreSQL and `orderbook` is our own L2 orderbook engine, whose schema is fixed in its
own source — a group either is that shape or it cannot be placed there, and rendering DDL for it
yields no statements rather than no tables.

**Python is the reference implementation.** Where it disagrees with the contract, the contract is
right and the library is wrong — with one exception, stated in the contract itself:
`model/001-single-entity` was written by hand from the rules and is authoritative for the encoding,
so if the reference disagrees with *it*, the reference is wrong until somebody argues otherwise in
writing.

**TypeScript is Tier 0 and that is a decision, not a gap.** It has no engine adapters, so it opens no
connections and applies no schema; it parses maps, resolves routing, hashes identifiers and enforces
every parsing rule — including the two reserved table names it will never write to, because a rule
that holds in one runtime and not the other is one map with two meanings. Its second purpose is
structural: the contract's neutrality is checked by a second implementation, not by a fourth, and
several rules in the contract exist because these two disagreed.

Neither library is published to a package registry yet. `pip install smart-data-engine` and
`npm install @smart-data-engines/sde` do not install ours today, and both names are unclaimed — which
is worth knowing before you follow an installation line in any of our documents.

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
claimed and none is in progress. The cost of the *n*-th library is the cost of one implementation of
this contract rather than another version of the product, which is the whole argument for the
contract being a byte-level document — but it is still a cost, and a library nobody keeps working is
worse for a client than no library in that language.

A Tier 0 implementation was written in **Go** on 6 September 2026 and is deliberately **not** on this
page and not in this repository. It was a measurement, not a library: its purpose was to find out
whether `format-contract.md` is sufficient to implement from without asking us, and it found six
defects in the two libraries above. Publishing it would be a support claim we cannot keep, and a
fourth implementation to hold in sync with every contract change. What it produced is written down in
[`implementing.md`](implementing.md#the-measurement).

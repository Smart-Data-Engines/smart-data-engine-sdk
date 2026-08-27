# Smart Data Engine — client libraries

[![check](https://github.com/Smart-Data-Engines/smart-data-engine-sdk/actions/workflows/ci.yml/badge.svg)](https://github.com/Smart-Data-Engines/smart-data-engine-sdk/actions/workflows/ci.yml)
[![CodeQL](https://github.com/Smart-Data-Engines/smart-data-engine-sdk/actions/workflows/codeql.yml/badge.svg)](https://github.com/Smart-Data-Engines/smart-data-engine-sdk/actions/workflows/codeql.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Node 18+](https://img.shields.io/badge/node-18%2B-blue.svg)](https://nodejs.org/)

Declare your data model. We decide which database engine each part of it lives in, how it is laid out
there, and when it should move — and move it while your application keeps running.

Your code never names a table or an engine. That absence is the product: it is what lets the physical
schema change underneath you without touching a line of your code.

```python
from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID
import sde

@sde.entity
class User:
    id: UUID
    email: str

    class Meta:
        pii = ["email"]

@sde.entity
class Order:
    id: UUID
    user: sde.Ref[User]
    total: Annotated[Decimal, sde.precision(12, 2)]
    created_at: datetime

    class Meta:
        residency = "EU"

@sde.entity
class Event:
    id: UUID
    name: str
    at: datetime
```

`User` and `Order` are related, so they are placed together and get a row store. `Event` is joined to
nothing, so it is free to go somewhere built for it. That split — the transactional core in one
engine, the stream nobody joins in another — is the decision most applications get wrong once, at the
start, and never revisit, because moving a live table is a project rather than a decision.

## Status

**Early.** What works today is the first slice, end to end: declaration, canonical model and version,
colocation groups, operation shapes, placement maps with signature verification, routing, and a
PostgreSQL adapter that creates schema and reads and writes through it. There is no telemetry yet, no
planner, and no migration engine. Those are next, in that order, and migration is last on purpose.

| Library | Tier | Status |
|---|---|---|
| [`python/`](python/) | 0, and 2 for PostgreSQL | reference implementation |
| [`typescript/`](typescript/) | 0 | passes the same vectors, byte for byte |
| `java/`, `rust/`, then C#, Go, Kotlin, PHP, Ruby | — | contributions welcome; the contract now has two implementations, which is what made it safe to invite them |

## What you declare, and what you do not

You declare entities, relations, and four invariants. Everything else about storage is ours.

The four exist because no amount of watching traffic reveals them:

| Declaration | Why traffic cannot tell us |
|---|---|
| `atomic_with` | that two entities must commit together is a business rule, not a pattern |
| `residency` | where data may legally live is not visible in a query |
| `pii` | which column is personal data determines retention and what may be denormalised |
| `cost_ceiling` | your budget is not in your workload |

If a future feature needs a fifth declaration, that is a change to what the product promises rather
than a new configuration option, and it will be argued about as one.

## Open core, stated plainly

These libraries are Apache-2.0 and free. The **control plane** — the placement planner, the scoring
model, the migration orchestrator — is private, and that is the part you pay for.

Which means the boundary runs in both directions. Nothing from the planner is here, and **nothing that
touches your data is anywhere else**. That second half is why this repository exists: "we never see a
row" stops being a promise you have to accept and becomes something you can check.

Three things you can verify without asking us:

- **We are not in your data path.** [`routing.py`](python/src/sde/routing.py) is a dictionary lookup
  and three conditions. There is no code path that sends a query through us, so our outage cannot be
  your outage.
- **Telemetry carries no values.** An operation shape is assembled from the structure of a call, not
  from a query string, so there is nowhere for a value to come from. Compare that with the SQL route,
  where you receive a string full of literals and strip them out with a parser you hope is complete.
- **It works without an account.** Hand-write a placement map, point the library at it, and everything
  runs — no key, no network, no account. An unsigned map is valid. That is a supported mode, tested as
  the default in our own suite, and the honest answer to what happens if you stop paying us.

What is refused is a map that *claims* to be ours, by carrying a signature, when there is no key to
check the claim against. An unverifiable claim is worse than no claim.

## The one hard rule across four languages

Four libraries computing the same model version is not a nice property, it is the difference between a
working product and a broken one. If Python and Java disagree, the same model arrives at the control
plane as two models, gets two placements, and half a fleet writes to a different set of tables.
Nothing about that fails at compile time.

So the encoding is specified at the byte level in [`docs/format-contract.md`](docs/format-contract.md)
— UTF-8, keys NFC-normalised then sorted by code point, no insignificant whitespace, minimal escaping,
no float literals, a closed type vocabulary so that `Decimal` and `BigDecimal` land on the same bytes.
And [`conformance/`](conformance/) holds vectors that every library runs in its own test runner, so a
divergence is a red test for whoever caused it rather than an operation written to the wrong engine in
production.

`conformance/vectors/model/001-single-entity` was written by hand from the document rather than
generated, which makes it the one vector that proves the document says enough to implement from. So
was every vector under `conformance/vectors/canonical/`.

### What the second implementation found

Writing TypeScript against the contract, rather than translating Python into it, found a divergence
worth the entire exercise. JavaScript compares strings by UTF-16 code unit; the contract requires code
point order. Those agree for everything in the Basic Multilingual Plane, so no test written with Latin
or CJK identifiers can see the difference — and they disagree above U+FFFF, where an astral character
is a surrogate pair starting at 0xD800 and therefore sorts *before* U+E000 instead of after. One field
name like that and two libraries produce two versions of one model.

Then mutation testing found that the first vector written for it did not actually cover the bug. Every
object key in the model IR is fixed ASCII, so swapping the object-key comparator for a naive sort
passed the whole suite; field names reach the IR as array elements, through a different comparator. The
`canonical/` vectors exist to close that, and both call sites are now verified by deliberately breaking
them and watching the suite go red.

## Getting started

```bash
cd python && python -m venv .venv && .venv/bin/pip install -e '.[dev,signed,postgres]' && cd ..
cd typescript && npm install && cd ..
make pg-up && make check
```

`make check` runs both languages. It does not stop at the first failure across them on purpose: if
Python and TypeScript have both drifted, you want to see both, because the fix is usually in the
contract rather than in either library.

The integration slice runs against a real PostgreSQL rather than a fake. A fake would agree with
whatever this library believes about types, quoting and transactions, which is exactly the set of
beliefs worth checking.

## Contributing

[`CONTRIBUTING.md`](CONTRIBUTING.md), and the short version: read the format contract, pass the Tier 0
vectors in your own runner, be honest about which tier you reach, and write an API that is idiomatic in
your language rather than Python transliterated.

## Licence

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

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
colocation groups, operation shapes, placement maps with signature verification, routing, hashed
identifiers, telemetry, and **three engine adapters** — PostgreSQL, ClickHouse and the
[orderbook engine](https://github.com/Smart-Data-Engines/low-cost-and-low-latency-orderbook-dbengine).
The first two create their own schema and read and write through it.

Two engines is the first point at which any of this means anything. Between a row store and a column
store lies the decision most applications get wrong once, at the start, and never revisit; with one
adapter there was nothing to choose between.

The third is a different kind of engine and it changed something about this library rather than
adding to it. It stores L2 orderbook depth in a shape fixed in its own C++ source, so there is no
`CREATE TABLE` to send it and the relationship inverts: for PostgreSQL and ClickHouse you declare a
model and we choose the physical schema, and here the engine has already chosen. A group either *is*
that shape or it cannot be placed there, and `sde.ORDERBOOK_SHAPE` is the shape.

Three differences from a general-purpose store are named rather than smoothed over, because a client
planning around this engine needs to know which promises it does not make. It has no transactions.
It does not enforce a key — two writes with the same one both persist, measured, so a read by key
**refuses** when it finds two rather than answering with one of them. And its unit of work is an
update of N depth levels rather than a row, because `level` is a price's index inside an update and
not a column the write API accepts; a single-row write can therefore only produce level 0, and any
other value is refused rather than stored at 0 with the read disagreeing with the write.

Smoothing any of those over had only bad forms. Mapping a client's field names onto the engine's
would mean guessing which declared field is the price from what it is called, and reasoning from a
name is what this library refuses everywhere else.

The pair is also what makes a claim checkable that was previously only stated. `save()` the same value
through both adapters, read it back from each, and the two results have to be equal — in content *and*
in Python type. That test found two divergences the moment it existed: a naive `datetime` landed in
PostgreSQL as 12:00 and in ClickHouse as 10:00, and a `timestamptz` came back timezone-aware from one
engine and naive from the other, so the same field read from the two could not even be compared. Both
are fixed; the test is `python/tests/test_engine_agreement.py`, it needs both servers, and CI fails if
it skipped.

What does not exist yet: **no migration engine**. Migration is last on purpose; one lost row ends a
product like this, so it comes after three checkpoints and a test that deliberately drops writes in
order to prove the verification notices.

| Library | Tier | Status |
|---|---|---|
| [`python/`](python/) | 0 and 1, plus 2 for PostgreSQL, ClickHouse and the orderbook engine, plus hashing | reference implementation |
| [`typescript/`](typescript/) | 0, plus hashing | passes the same vectors, byte for byte |
| `java/`, `rust/`, then C#, Go, Kotlin, PHP, Ruby | — | contributions welcome; the contract now has two implementations, which is what made it safe to invite them |

One honest qualification on that table, because the whole point of the tiers is that "supported" means
the same thing in every language. Tier 0 is backed by shared vectors that both implementations run.
**Tier 1 and Tier 2 are not, yet** — the `telemetry/`, `schema/` and `migration/` vector sets named in
`docs/format-contract.md` §10 do not exist. Python's Tier 1 and Tier 2 are covered by its own tests
and by a slice against a real PostgreSQL, which is not the same thing: it verifies that the
implementation works, not that a second implementation would agree with it. Today that costs nothing,
since no other library claims those tiers. It costs on the day one does, so the vectors come before
the claim does.

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

One more refusal is worth knowing about, because it means the library keeps a little state. A
signature says a document is authentic and says nothing about whether it is current — a signed map
for version 3 verifies correctly forever. So a **signed** map is refused if it is older than one
already applied against your engines, and remembering which one that was needs a table:
`sde_map_state`, append-only, in your own engines, holding a map version and a timestamp and nothing
else. Equal is fine; restarting is ordinary. `session.rollback_protection` says whether it is in
force, because a guarantee whose state you cannot read is one you have to take on trust — and for an
engine whose schema is fixed in its own source there is nowhere to keep it, so the answer there is
`unavailable` rather than a pretence. An **unsigned** map is never checked: it is your document, and
in the no-account mode this costs nothing, creates nothing and queries nothing.

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

The integration slices run against real servers rather than fakes. A fake would agree with whatever
this library believes about types, quoting and transactions, which is exactly the set of beliefs
worth checking.

The orderbook slice is the exception, and the exception is split rather than waived. That engine's
Python client is not on PyPI and its shared library is built from C++, so it cannot run everywhere.
Everything the adapter *decides* — the shape check, the level refusal, the refusal on a duplicate
key, unknown-not-zero for a sequence number — happens before the client library is called and is
tested against a fake, which runs everywhere. What a fake cannot check is whether the engine still
behaves as measured, so four measurements are asserted against the engine itself:

```bash
git clone https://github.com/Smart-Data-Engines/low-cost-and-low-latency-orderbook-dbengine ../ob
cmake -S ../ob -B ../ob/build && cmake --build ../ob/build -j"$(nproc)"
OB_LIB_PATH=$PWD/../ob/build/liborderbook_shared.so PYTHONPATH=$PWD/../ob/python \
  SDE_ORDERBOOK=1 python/.venv/bin/python -m pytest python/tests/test_orderbook_slice.py
```

If the engine changes, that file fails and the fake stops describing something true — which is the
failure mode a fake normally hides.

## Contributing

[`CONTRIBUTING.md`](CONTRIBUTING.md), and the short version: read the format contract, pass the Tier 0
vectors in your own runner, be honest about which tier you reach, and write an API that is idiomatic in
your language rather than Python transliterated.

## Licence

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

# Contributing

## What is here and what is not

This repository holds the **client libraries**. They declare a model, cache a placement map, route
operations, manage schema and collect telemetry. They are Apache-2.0 and they work without an
account.

The **control plane** is a separate, private program: the placement planner, the scoring model, the
migration orchestrator. That is the part clients pay for.

The boundary is enforced in both directions, and the second direction is the one people forget:

- Nothing from the planner belongs here.
- **Nothing that touches a client's data belongs anywhere else.** The moment a code path that sees a
  client's rows moves into the private repository, the privacy guarantee stops being verifiable and
  goes back to being a promise. Open-sourcing this library is what makes "we never see a row"
  checkable, so a change that moves such code out of it defeats the reason the repository exists.

A grey area worth naming: `python/src/sde/layout.py` derives a default schema from a model. That is
here, not in the planner, because the library has to work with a hand-written map. What is *not* here
is which engine a group belongs in, which indexes earn their cost, when to partition, or when to move
anything.

## Before you open a pull request

```bash
make pg-up          # a PostgreSQL for the integration slice
make check          # lint, types, tests
```

`mypy --strict` is not decoration. Several guarantees in this design are enforced by types — a
proposal that cannot be constructed without a rollback path, a projection that cannot exist without a
stated basis. Weakening a type to make an error go away usually deletes a guarantee.

## The conformance vectors are the contract

`conformance/vectors` is shared by every language. If you change what a library computes, you are
changing the contract, and that means:

1. Bumping `conformance/contract-version.txt`.
2. Updating every library that declares that version.
3. Explaining, in the commit message, why the old contract was wrong.

There is no quiet fix. Somebody may have a stored placement map that depends on the old bytes.

If your language cannot reproduce a byte the contract requires, the first hypothesis is that
**the contract is wrong** — that it wrote down what one language happens to do. That has already
happened once, and `docs/format-contract.md` says so at the bottom.

Vectors are generated once and reviewed by hand; `conformance/tools/generate.py` refuses to run
without an explicit flag for that reason. `model/001-single-entity` was written by hand and is never
regenerated: it is the only independent check that `docs/format-contract.md` says enough to implement
from.

## Adding a library in a new language

Read `docs/format-contract.md`. It is meant to be sufficient on its own — if you had to ask us
something, that is a bug in the document and we would rather have the bug report than the question.

Two things make a library an SDE library, and both are non-negotiable:

- It passes the Tier 0 vectors in its own test runner.
- It declares which contract version and which tier it implements, honestly.

Everything else should be idiomatic for your language. An API in Java should not read like Python
transliterated; the *contract* is identical, the ergonomics are yours. The Python library is a
reference for behaviour, not a template for style.

## What we will push back on

**Anything that puts us in the data path.** The library connects to engines directly and answers every
operation from a cached map. A feature that needs a round trip to the control plane at query time
makes our outage the client's outage, and we will drop the feature rather than the property.

**Any judgement moving into the library.** Routing is a dictionary lookup and three conditions. That
is affordable in four languages; a planner is not.

**A retry that crosses engines, or a swallowed write error.** Internal problems in the library are
swallowed and logged, because a bug of ours must not take down someone's application. A write that
did not happen is not an internal problem.

**A fifth thing for the client to declare.** They declare atomicity, residency, personal data and a
cost ceiling — the four things traffic cannot reveal. Everything else about storage is our decision.
A new configuration knob is a change to what the product promises, not a convenience.

## Commits

Conventional commits, in English. Say why, not what: the diff already says what. This repository is
public and every commit in it is part of how the company is judged.

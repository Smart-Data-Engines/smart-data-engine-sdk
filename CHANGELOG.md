# Changelog

The two libraries are released separately, one tag per language: `python-v*` to PyPI and
`typescript-v*` to npm ([`docs/publishing.md`](docs/publishing.md) §5). A shared version number does
not make them agree. What does is the conformance suite and
[`conformance/contract-version.txt`](conformance/contract-version.txt).

## Unreleased

**Fixed: a row the model does not allow is refused before any engine** (both libraries,
[`docs/format-contract.md`](docs/format-contract.md) §8b). `Session.save` refuses a field its entity
does not declare, a required field left out, and a required field given null. In TypeScript
`undefined` is refused like null. `Session.save_many` / `saveMany` refuses a null in a required field,
naming the row. A field is required when it is declared without `nullable`, and a key field always is.
Until now the engines answered for the library, each its own way, measured on PostgreSQL 15 and
ClickHouse 24.8:
- PostgreSQL stored NULL in a required field;
- ClickHouse stored a value nobody wrote: `0` for an integer left out, `""` for a string;
- ClickHouse's driver refused a null with a message about a column type.

A new `write` stage in the shared error vectors, `errors/078`-`082`, holds the refusal in both
runners. In each, one accepted write comes first, and the refused one must reach no engine. With
hashed identifiers, Python's refusal of an undeclared field now reads `declares no field`, as in
TypeScript. Rows stored before this release are unchanged.

**The orderbook adapter over TCP** (`smart-data-engine-sdk`, [`docs/orderbook.md`](docs/orderbook.md)).
- **Credentials, TLS and a timeout**, and an `orderbook://` DSN.
- **A chosen sequence number is refused before sending.** The server numbers every update itself.
  The current server refuses a chosen number, and an older one discarded it, so the row read back
  disagreed with the row written.
- **`Session.save_many` writes whole updates.**
- **`Session.scan` pages one book in key order** although the engine answers in arrival order.
- **`count` and `summarize` are refused by name.**
- **Values the engine cannot store are refused with the field named**, before anything is sent.

A new CI job, `orderbook`, builds the engine at a pinned commit and runs every orderbook slice
in-process and over TCP, plain and with credentials and TLS.

**Placement map contract 6: a group without a write generation** (both libraries,
[`docs/format-contract.md`](docs/format-contract.md) §7k).
- **A group may leave `write_epoch` out** when its engine cannot fence writes, as the orderbook
  engine cannot. Such a group has only a source, and `null` is not absence. A contract-5 library
  refuses the whole document.
- **A session checks the engine against the map, both ways.** It refuses such a group on an engine
  with write fences, and a group with a generation on an engine without them. Its writes carry no
  generation column, and the column stays reserved.
- **No staging, cutover or in-place index build acts on such a group**, and each carries it
  unchanged. The local operator needs no binding for its engine and refuses one.
- New vectors: `errors/074`-`077`, `migration/188`-`199`, `signature/015`. `errors/006` now declares
  contract 7.

**The orderbook adapter in TypeScript** (`@smart-data-engines/sde/engines/orderbook`,
[`docs/orderbook.md`](docs/orderbook.md)). It speaks the engine's TCP protocol itself, with
credentials and TLS, and makes the reference's decisions with its refusals. That covers whole
updates in batches of 64, pages in key order from an engine that answers in arrival order, the
server's sequence number, and count and summarize refused by name. A TypeScript session can now
open on a map with an orderbook group. As in Python, a count's or a summary's refusal now comes from
the adapter's own reason (`countRefusal`, `summaryRefusal`), before the call is timed.

**A trading firm's application on three engines** ([`examples/trading/`](examples/trading/)). Depth
on the orderbook, orders and fills on PostgreSQL and trades on ClickHouse, from one contract-6 map.
It provisions, writes and verifies every row from Python, and its TypeScript half reads what the
Python half wrote. CI runs it on all three engines.

**The orderbook engine pin is `9f55e84`**, past the engine's #198. A quantity of exactly 2^60 - 1
used to read back as 2^64 - 1, and every later quantity in its segment as 0. Both libraries' slices
now read the edges of the quantity range back.

**`sde.engine_facts(dialect)`** (Python): what each engine can and cannot do, as data. It covers the
schema (derived, or the fixed orderbook shape), transactions, the key, write generations, the write
unit, the reads, and what an orderbook scan needs. The control plane gives it to the model that
decides placement. Each fact is tested against its adapter and engine.

## `smart-data-engine-sdk` 0.1.1

Published on 28 September 2026, on the first run of `python-v0.1.1`, with attestations.

A patch release of the Python library alone. `@smart-data-engines/sde` has no 0.1.1. Of what it
ships, only the version field has changed since 0.1.0, to a development version. One tag per
language keeps an otherwise identical package from going out under a new number (§5.1 of
[`docs/publishing.md`](docs/publishing.md)).

**Fixed.** Resume and abandonment of a PostgreSQL in-place index build
used to read the catalogue while the interrupted statement was still running. A deadline or a
killed operator ends the client, not its `CREATE INDEX CONCURRENTLY`. Recovery then either raced
that statement's commit and failed with "tuple concurrently updated", or dropped the index it was
finishing. Both now wait for the statement to end, then keep what it finished
([`docs/in-place-index.md`](docs/in-place-index.md)).

## `smart-data-engine-sdk` 0.1.0 and `@smart-data-engines/sde` 0.1.0

Published on 27 September 2026, each tag on its first run of the release workflow, with attestations
on PyPI and provenance on npm. On npm it is `latest`.

The first final release. The libraries are the release candidates below: apart from the version
itself, nothing in `python/src`, `typescript/src` or `typescript/bin` changed after `python-v0.1.0rc1`
and `typescript-v0.1.0-rc.1`.
The candidates were verified from the registries before this number was set:
- the registries' artefacts are the ones the release gate checked;
- the PyPI package runs the library's suite;
- the npm package's signatures and provenance verify;
- both compute the same model version;
- the Weather starter ran end to end from the registries.

Two things changed around the libraries:
- the release workflow checks the registry for longer than its cache (#95), and says why npm
  refused a publish if it does (#96);
- the documents describe a released SDK (#96).

## `smart-data-engine-sdk` 0.1.0rc1 and `@smart-data-engines/sde` 0.1.0-rc.1

Published on 27 September 2026. These are the first release candidates published through the
release workflow, with attestations on PyPI and provenance on npm. They cover everything since
`0.1.0.dev0`, the development release that claimed the PyPI name on 12 September 2026.

**Runtimes.** Python 3.11 to 3.14 and Node 18 to 26, every version tested in CI
([`docs/platforms.md`](docs/platforms.md)).

**Placement maps and moving data.**
- Map contract 5 carries a physical design for each group: key order, a time partition, and indexes
  from a closed vocabulary. It is checked against the engine's own catalogue
  ([`docs/physical-design.md`](docs/physical-design.md)).
- Write generations are enforced by the engine. Migration verification is bound to the project, the
  immutable map and the group.
- The local operator, `sde-operator`, handles all copy and index work:
  - it executes and recovers signed cutovers;
  - it stages fresh copies, also within one engine;
  - it builds, removes and replaces indexes in place, without a copy or a write pause;
  - it abandons a staging that cannot finish.

**Sessions.**
- Both languages have bounded batch writes, point reads, keyset scans, counts and exact numeric
  summaries.
- Session and transaction ownership.
- Verified TLS to both engines ([`docs/engine-connections.md`](docs/engine-connections.md)).
- Values keep their precision. Timestamps keep their microseconds in TypeScript reads and in
  ClickHouse parameters from Python, and ClickHouse JSON numbers stay exact.

**Telemetry.** The window document reports three things:
- each operation's shape;
- the fields each read filtered on, by name only;
- the group's size, index share, daily growth, time-filtered share and write burstiness
  (`Session.measure_storage()` / `session.measureStorage()`), as numbers only.

**The Weather starter.** `sde-weather` (Python) and `sde-weather-ts` make up a local, recoverable
customer starter ([`docs/weather-starter.md`](docs/weather-starter.md)):
- restricted runtime credentials;
- point, analytics, fleet and alerts workloads;
- operator handoffs;
- an ownership-checked reset.

**Releasing.** A release is a per-language tag, behind a gate, artefact checks and OIDC publishing
with no stored credential. The npm dist-tag is chosen from what the registry already holds.

# Changelog

The Python and TypeScript libraries are released separately, one tag per language: `python-v*` to
PyPI and `typescript-v*` to npm ([`docs/publishing.md`](docs/publishing.md) §5). The C++ library is
not released yet; it is built from this repository. A shared version number does not make them
agree. What does is the conformance suite and
[`conformance/contract-version.txt`](conformance/contract-version.txt).

## Unreleased

**New: a C++ library, Tier 0 with hashing** ([`cpp/README.md`](cpp/README.md)). It declares models
and computes their versions and shapes, reads and writes the neutral declaration, loads placement
maps of contracts 1 to 6 with their signatures and key sets, and routes operations. It has no
engine adapters yet. It is built with CMake from this repository (GCC 12 or Clang 16 at the oldest,
OpenSSL 3, utf8proc) and is on no package registry.
- It passes every vector of its tier.
- Where the other two libraries coerce a value or fail with their runtime's own error, it refuses
  with the contract's. Those inputs are listed in
  [`docs/implementing.md`](docs/implementing.md#the-third-implementation-c), to become rules in every
  library.
- Four required checks join the ruleset, nineteen in all: `cpp (gcc-12)`, `cpp (clang-16)`,
  `cpp-sanitizers` and CodeQL's `analyze (c-cpp)`.

**Fixed: the local operator beside the engine's administrator** (Python,
[`docs/runtime-roles.md`](docs/runtime-roles.md#the-operators-login-and-the-principals-beside-it)).
On ClickHouse the operator refused every operation when the server's administrator was not its own
login. It took the administrator's global grants for an undeclared grantee of the tables.
- A user with a direct global `ACCESS MANAGEMENT` is now admitted, as a PostgreSQL superuser always
  was and as `docs/local-cutover.md` promised.
- An in-place index build no longer refuses any other grantee, because it moves no authority.
- Staging and cutover still refuse anyone else covering a table, a role or a monitoring login with
  `SELECT ON *.*` among them. The refusal now names the principal and its grants.

A general test on 2 October 2026 found it: the first build was refused beside the administrator.
No test had run an operator that was not the administrator; now staging, cutover and a build do.

**Fixed: the library's own reads use the engines' indexes** (both libraries,
[`docs/format-contract.md`](docs/format-contract.md) §7a). On PostgreSQL every scan, count and
summary compares text as `("x" COLLATE "C")`, but the library created text columns in the default
collation, and every index on them with it, primary keys included. The planner uses an index only in
the predicate's own collation, so none of them served a read. Measured on 200 000 rows, a scan of one
station of a `(station, at)` key read the whole table in 27 ms; now it reads the key in 0.103 ms. On
ClickHouse a UUID equality was `toString(id) = ...` and read 25 of 25 granules; now it compares
natively and reads 1. A general test found it: a B-tree an agent decided, and the operator built
under traffic, left the scans it was decided for at 131 ms, before it and after it.
- PostgreSQL text columns are created `COLLATE "C"`. So is a text column in every index: built at
  provisioning, in place, or on a staged copy. Six `schema/` vectors carry the new bytes. Maps, model
  versions and signatures do not change.
- A table created before this release keeps its collation. An index built on it now serves the
  reads, but its primary key still cannot. Python logs `sde.schema.text_collation` for the primary key
  and for each declared index in that state, with the remedy. Either stage a fresh copy, or have an
  administrator run `ALTER COLUMN ... TYPE text COLLATE "C"`, which holds an exclusive lock on the
  table while its indexes rebuild.
- An in-place build resumed after an upgrade builds again an index of its own that an earlier library
  left without the collation.
- `python/tests/test_index_use_live.py` and `typescript/tests/index-use.live.test.ts` ask each engine's
  planner for the plan of the exact statement the library sent. Point `get` already used plain
  equality on both engines and was not affected.

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

**Changed: which fields may be null is part of the orderbook shape.** `sde.fixed_schema_mismatch`
takes a required `nullable` argument, which `sde.group_nullable(model, group)` derives. A model
declares `sequence_number` nullable and no other field (`sde.ORDERBOOK_NULLABLE`), and
`default_layout` refuses anything else. Both kinds of mismatch used to pass and then could not be
written, on an engine a group can never move off:
- a nullable `quantity`;
- a required `sequence_number`, which the server assigns.

The engine facts say so: `fixed_shape.nullable` and `fixed_shape.assigned_by_server`. A caller of
`fixed_schema_mismatch` must now pass `nullable`, and a call without it fails rather than returning
half an answer.

**Fixed: an orderbook read refuses a value outside the model's types** (both libraries). An engine
before `c1f14c0` read a stored quantity of 2^60 - 1 back as 2^64 - 1, and the adapters returned it as
an `int64`. Now the read is refused, naming the field.

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

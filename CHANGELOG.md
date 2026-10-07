# Changelog

The Python and TypeScript libraries are released separately, one tag per language: `python-v*` to
PyPI and `typescript-v*` to npm ([`docs/publishing.md`](docs/publishing.md) §5). The C++ library is
not released yet; it is built from this repository. A shared version number does not make them
agree. What does is the conformance suite and
[`conformance/contract-version.txt`](conformance/contract-version.txt).

## Unreleased

**New: the ClickHouse adapter in the C++ library** ([`cpp/README.md`](cpp/README.md)).
`sde::ClickHouseEngine`, over ClickHouse's HTTP interface through libcurl, in a target of its own,
`sde::clickhouse`, so an application that places nothing in ClickHouse carries no libcurl. It is the
reference's adapter, method for method: schema with the catalogue's check and the physical design's
findings, writes and batches, logical reads, counts and exact summaries, the forward-only
bookkeeping, the migration operations, write barriers - a constraint and a drain bound to the
table's UUID - storage sizes, and TLS against the CA the URI names, read once and pinned. Its
connection URI is parsed as the reference parses it, rule for rule and message for message,
compared on 102,000 URIs.
- An exchange is one POST on a connection of its own, never reused: a failed one is reported with
  its outcome unknown and never sent again, measured against a proxy that drops the answer to an
  accepted insert. The statement is the body, never part of a URL.
- Rows travel in `RowBinaryWithNamesAndTypes` both ways, so a date of year 1 or 9999 is the date it
  was. A value its column cannot hold is refused before anything is sent: the reference's driver
  stores `Decimal("1.239")` in a `Decimal(12, 2)` as 1.23 - PostgreSQL rounds the same save to 1.24
  - and `Decimal("12345678901.23")` as 12345678901.20, an enum name its type does not declare as 0,
  and cuts a moment's fraction a column does not keep, each without a word: recorded in
  [`docs/implementing.md`](docs/implementing.md), as rules for the contract.
- Its live tests are the reference's ClickHouse slice, each message captured from the reference
  against the same server; what the reference runs against both engines with one body runs here
  with one body for each adapter built - copies in every direction, write generations, the frozen
  comparison, the bookkeeping, and the two engines agreeing on one value. CI runs them against a
  ClickHouse server at both compiler floors and under the sanitizers.
- Fixed on the way, in the C++ library: a PostgreSQL size the catalogue refused was reported as
  `failed` rather than `refused`, because the session looked for the SQLSTATE in a message that
  never carries it.

**New: the PostgreSQL adapter in the C++ library** ([`cpp/README.md`](cpp/README.md)).
`sde::PostgresEngine`, over libpq, in a target of its own, `sde::postgres`: the core still links no
network library, and an application that places nothing in PostgreSQL carries no libpq. It is the
reference's adapter, method for method - schema with the catalogue's check of columns and types and
the physical findings, writes and batches, logical reads, counts and exact summaries, transactions
with savepoints, the forward-only bookkeeping, the migration operations, native write fences and
storage sizes - and TLS is the DSN's, verified by libpq. With it, `sde::prepare_schema`: what a
person provisioning a map runs before the application opens it, since from contract 4 a session
creates nothing.
- Its live tests are the reference's PostgreSQL slice, ported, and every message they compare was
  captured from the reference against the same server. They run against a real server in CI, and
  fail rather than skip there without one.
- Fixed in the C++ library on the way: `sde::PhysicalFinding::to_string` was declared and defined
  nowhere, so calling it failed to link; `Session::ensure_schema` did not emit
  `sde.schema.physical_mismatch`, which the reference emits there; and `sde::LOG_EVENTS` named two
  events nothing emits and lacked three the adapter does - a test now holds it to the code.

**New: migration participation and signed packets in the C++ library, Tier 2**
([`cpp/README.md`](cpp/README.md)). `sde::backfill` copies a group's rows to the copies its map fans
writes out to, chunk first and marker after, and resumes; `sde::verify` compares them with the
source and confirms every missing row with a point read. `sde::VerificationRequest` binds a
comparison to its request, project, map and group, and `sde::verify_frozen` compares under named
write barriers. `sde::load_cutover_plan`, `sde::load_staging_plan` and `sde::load_index_plan` decode
and authorise the signed packets of §7g, §7h and §7j without executing anything.
- Every `migration/` vector passes, so the library claims Tier 2: the vectors. Which engines it
  reaches is the engines cell of [`docs/implementations.md`](docs/implementations.md), separately,
  and an engine enters it when its adapter round-trips.
- The three packet loaders were compared with the reference's on 102,384 signed packets, made
  by changing every accepted packet vector at each of its paths and in random pairs and signing them
  again. Outside three named classes, recorded in [`docs/implementing.md`](docs/implementing.md),
  the outcome, fingerprint, record and message were the same in every case.
- A staging refusal names a staging. The reference's helpers it shares with the cutover packet say
  "cutover" ("cutover stage_id must be ..."), and no vector pins those messages; this library takes
  TypeScript's wording, and the reference's is a finding.

**New: the session in the C++ library** ([`cpp/README.md`](cpp/README.md)). `sde::Session` opens
against `sde::Engine`s with the reference's checks. It writes and batches, fans writes out to copies
after the commit, reads by key and by plan, and keeps a transaction to one group. It stamps and
checks write generations, holds `sde::WriteFence`s and refuses a signed map that goes backwards. An
adapter's optional abilities are data on the value (`Engine::capabilities()`). The in-memory engine
of the `migration/` vectors is `sde::testing::MemoryEngine`, in a target of its own.
- It passes the session and write stages of `errors/` and the `migration/` cases of fences,
  generations, batches, fan-out and the forward-only check. The tier stays 1 until backfill and
  verification.
- A log sink that throws can no longer fail a map load; the C++ loader called it unguarded.

**New: values, DDL and read plans in the C++ library, the first part of Tier 2**
([`cpp/README.md`](cpp/README.md)). `sde::Value` holds one host type per neutral type, with an exact
`sde::Decimal` and microsecond timestamps. `sde::schema_statements` renders a layout's DDL and
`sde::compatibility_views` the views on a target. `sde::plan_read` checks a read and normalises its
values; `sde::numeric_summary` computes an exact summary, with the mean rounded half to even.
- It passes every `schema/` and `query/` vector; the library stays at Tier 1 until it takes part in
  a migration.
- Its answers were compared with the reference's own on 240,000 random inputs. The comparison
  found text the two other libraries read in two ways: the whitespace around a decimal, an offset
  part past 59, and 24:00. Each now has one rule, held by `query/024`-`028`, which this library
  takes as well.
- `sde::QueryRefused` is a `sde::ModelPlanningError`, as in the reference, and `sde::schema_is_fixed`
  refuses a dialect it does not know rather than answering `false`.

**New: telemetry in the C++ library, Tier 1** ([`cpp/README.md`](cpp/README.md)). `sde::Recorder`
measures operations by shape and rolls windows, and `Window::as_record` writes the §6a document; every
`telemetry/` vector passes. The recorder takes no lock on an operation's path: a window is a block of
atomic counters, swapped at a roll that waits for the writes begun on the old block. Its concurrency
is tested under ThreadSanitizer in CI.

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

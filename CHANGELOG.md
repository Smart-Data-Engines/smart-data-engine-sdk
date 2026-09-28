# Changelog

The two libraries are released separately, one tag per language: `python-v*` to PyPI and
`typescript-v*` to npm ([`docs/publishing.md`](docs/publishing.md) §5). A shared version number does
not make them agree. What does is the conformance suite and
[`conformance/contract-version.txt`](conformance/contract-version.txt).

## `smart-data-engine-sdk` 0.1.1

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

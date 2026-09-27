# Changelog

The two libraries are released separately, one tag per language: `python-v*` to PyPI and
`typescript-v*` to npm ([`docs/publishing.md`](docs/publishing.md) §5). A shared version number does
not make them agree. What does is the conformance suite and
[`conformance/contract-version.txt`](conformance/contract-version.txt).

## `smart-data-engine-sdk` 0.1.0rc1 and `@smart-data-engines/sde` 0.1.0-rc.1

These are the first release candidates published through the release workflow. They cover
everything since `0.1.0.dev0`, the development release that claimed the PyPI name on 12 September
2026.

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

# Local Weather starter acceptance — 15 September 2026

[acceptance.json](acceptance.json) records the implementation/test commits, artifact SHA-256 values,
installed-client results and each mutation's named witness. This qualifies the local synthetic
starter. It does not qualify production deployment, certify an SLA or complete the private
controller/AI demonstration.

The public applications were installed into a new Python venv and a separate npm application from
a built wheel/tarball. Python used `-I`, and both reported import paths were checked against the
installed `site-packages` / `node_modules` locations. Python resolved psycopg 3.3.5,
clickhouse-connect 1.8.0 and cryptography 50.0.1. The source suite also ran on the supported
clickhouse-connect 1.7.2 floor. The npm application contained runtime dependencies only and its
installation audit reported no known vulnerabilities.

Each source direction ran Python and TypeScript for 21 rows each, with different run namespaces.
Both performed batch writes, point reads, bounded scans, counts and exact decimal summaries, then
exported actual Recorder windows. The trial checked repeat setup, status while both credential
files were unavailable, unsigned-bootstrap refusal before allocation, and explicit double reset.
Credential files were removed on reset; metadata/history remained. The supplied bootstrap was
signed by an explicitly identified **test fixture**, not by an AI model or the private controller.

The full source suites passed before two further Python cases and one TypeScript case strengthened
the mutation witnesses. Those focused suites also passed. The two counts are recorded separately;
the GitHub PR checks verify the complete suite on the final head.

Twenty-one single-gate mutations were detected. Every selected positive control passed before the
change and after exact byte restoration; each mutant failed its named behavioral test. Coverage
includes ownership marker/identity, credential hashes, ClickHouse password proof, accidental
regrant, enrollment/completed-setup fsync, signature admission, pending write ranges, source-only
confirmation, replay, active bindings and closing replaced sessions. Test selection itself was
checked: a Vitest filter initially selected no case and the harness refused to count that as a
result. Mutation runs used committed source, exact backups and `finally` restoration.

Reproduce the functional acceptance using the [runbook](../../weather-starter.md) and inspect the
underlying tests:

- `python/tests/test_demo_resources.py`: native ownership, grant/reset recovery, and three real SIGKILL boundaries;
- `python/tests/test_enrollment_durability.py`: persistent fsync refusal and identical successful retry;
- `python/tests/test_demo_starter.py` and `typescript/tests/weather.test.ts`: failure outcomes, source/copy distinction, local status, configuration and resource lifetime;
- `python/tests/test_demo_starter_live.py` and `typescript/tests/weather.live.test.ts`: both native sources with restricted runtime credentials, including an unavailable unused adapter.

The Node peer also ran with an installed Python environment that lacked pytest. Its bootstrap
fixture is independent of the test runner. The TypeScript console program resides in `bin/` and is
checked with strict `checkJs` after compilation; the existing guarantee that library `src/` has no
console output remains enforced without an exclusion.

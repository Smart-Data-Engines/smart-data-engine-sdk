# Qualify a local cutover workload

The opt-in harness in `python/tests/qualification/` drives two Python and two TypeScript application
processes through installed SDK artifacts. It uses disposable PostgreSQL/ClickHouse namespaces and
restricted runtime logins, keeps a fixed arrival schedule, collects actual Recorder windows and
checks every acknowledged synthetic value. The ordinary pytest suite contains fast acceptance-
validator tests; the multi-minute workload is an explicit run.

The profile is `weather-append-point-v1`: station plus a microsecond timestamp as the composite key,
with exact decimal, int64 and UUID fields. One point get follows every five saves. Two workers adopt
new maps periodically; two retain their session until an explicit refusal makes them refresh.
The workers retain their Session when the map fingerprint is unchanged. A retry keeps the same
synthetic key and values; arbitrary ambiguous network failures are not claimed as generic upsert
semantics. PostgreSQL insertion and ClickHouse replacement remain different native semantics.

## Run requirements

Use the development dependencies and both test engines described in the repository README. The
native role fixture needs permission to create isolated test logins/namespaces. This harness is
for those disposable test environments. It does not restart the servers or change their limits.

Build and inspect the Python wheel and npm package using the existing artifact workflow. Install
the wheel with signed/postgres/clickhouse extras into a fresh venv and the npm tarball into a fresh
consumer with its optional `pg` peer. The coordinator's `--python` names the venv interpreter;
`--npm` names the installed `@smart-data-engines/sde` directory. Keep the venv interpreter path:
resolving its symlink to `/usr/bin/python` selects the system installation instead.

From the SDK repository root, with both standard test DSNs set:

```sh
PYTHONPATH=python/tests python/.venv/bin/python -m qualification.run \
  --scratch /absolute/session-scratch/unique-run \
  --python /absolute/installed-wheel-venv/bin/python \
  --npm /absolute/npm-consumer/node_modules/@smart-data-engines/sde \
  --mode before_decision --seconds 180 --rate 10 --seed-rows 5000
```

Each run directory must be new and outside the SDK repository. Set `TMPDIR` to the session scratch
root as well. `--rate` is writes per worker per second, so 10 means 40 aggregate writes/s. The seed
rows are inserted by the operator before measured application traffic and are validated separately.

Modes are `baseline`, `success`, `before_decision` and `after_decision`. Transition modes stage a
fresh copy after ten seconds and start cutover after twenty-five seconds. The crash modes stop the
operator at `repair:intent` or `decision:success`, then kill that process with SIGKILL after one
second and resume through a fresh installed operator. The former must abort; the latter must finish
its already durable success. Source and target directions are run sequentially.

## Acceptance and evidence

The declared local profile uses 40 scheduled writes/s, 180 seconds per scenario and 5000 seeded
rows. Steady p99 write, point-read and scheduling delay limits are 250 ms; the maximum scheduled
response time and gap between successful writes are 45 seconds. Each application process has a
256 MiB RSS ceiling. The signed cutover budget is 30 seconds. These are experiment boundaries on
the recorded host, not customer throughput or availability commitments.

Steady statistics exclude staging plus two seconds and cutover/recovery plus ten seconds for
catch-up. The complete scheduled response distribution and maximum success gap are also reported,
so the exclusion cannot hide the pause or accumulated scheduling debt. Refusals must be timed,
recognized SDK errors and inside cutover/recovery through two seconds after operator return.
Dropped or incomplete telemetry, missing processes, incomplete schedules, invalid/NaN timings,
incorrect native outcomes and any differing acknowledged value fail the run with a nonzero exit.

`report.json` contains the accepted cases and final `passed` flag. While a multi-case run is still
in progress, that flag remains false. Use the process exit status and final report together.
Each case retains worker samples, telemetry, stderr, signed packets, local operator state and
`events.json` with operation timings/receipts. Engine namespaces are removed when the case ends;
the synthetic expectation remains reproducible from worker/sequence. Reports contain no row values
or runtime connection strings; local operator configuration names environment variables only.

A process RSS includes the test harness's sample arrays and runtime, so it is not an isolated SDK
memory measurement. The harness currently supports the declared append/point-read profile; it does
not establish update/delete semantics, arbitrary customer-volume qualification or production SLOs.
Controller participation has a separate private test adapter; do not describe a run using locally
signed synthetic packets as proof of controller allocation/receipt integration under load.

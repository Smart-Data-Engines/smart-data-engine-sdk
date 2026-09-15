# Weather starter: installed packages to an isolated local application

This is a local, synthetic demonstration for PostgreSQL and ClickHouse. Python performs local
setup and runs the existing SDK operator. Python and TypeScript applications use independent
runtime connections. They read `state/active-map.json`; a running controller is not required.

The starter is unreleased: the published Python development version predates this command, and
npm has no published package yet. Use reviewed build artifacts. Do not publish a release merely
to try the demo. [Publishing](publishing.md) describes the separate release procedure.

## Install artifacts and obtain trusted metadata

Use Python 3.11-3.13, Node 18-22 and a local POSIX filesystem. An operator supplies the reviewed
wheel and npm tarball. In fresh application directories:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install './smart_data_engine_sdk-0.1.0.dev0-py3-none-any.whl[signed,postgres,clickhouse]'
npm init -y
npm install ./smart-data-engines-sde-0.1.0-dev.0.tgz pg
```

The version strings above identify development artifacts, not a promise that every package with
that version contains this feature. Retain their SHA-256 checksums and the supplying source commit.
The project owner can build a wheel with `python -m build --wheel --outdir ARTIFACT_DIRECTORY python`
and a tarball with `npm pack --pack-destination ARTIFACT_DIRECTORY` in `typescript/` after CI passes.

Give [the Weather declaration](../examples/weather/model.json) to the controller operator. They
return a `bootstrap.json` with exactly these fields:

- `kind: "sde-weather-bootstrap"` and `protocol: 1`;
- `project_id`, the neutral `model`, and `public_keys` mapping key IDs to raw Ed25519 bytes in base64;
- `engines`, mapping your binding names to `postgres` or `clickhouse` (one of each at most);
- `current_map`, the signed contract-4 initial map, version 1, with source materializations only.

Receive that file through the agreed authenticated operator handoff and verify the public-key
fingerprint through your trusted channel. A signature checked against a key from an untrusted
file does not identify your operator. The starter never creates a substitute signer.

## Prepare local resources

Start local test engines as described by the SDK [README](../README.md). Their administrator
DSNs remain in local environment variables `SDE_POSTGRES_DSN` and `SDE_CLICKHOUSE_DSN`. You can
name other variables using `--postgres-admin-env` / `--clickhouse-admin-env`; pass names, not secrets.
Setup accepts explicit loopback endpoints only. Do not point it at a production database.

```sh
.venv/bin/sde-weather --directory ./weather-demo setup --bootstrap ./bootstrap.json
.venv/bin/sde-weather --directory ./weather-demo doctor
```

Setup creates random, isolated namespaces and restricted runtime users. It durably records intent
before DDL, then calls SDK `prepare_schema` and `LocalCutover.enroll`. The operator and runtime
DSNs occupy separate mode-0600 files. The application reads only `runtime-credentials.json`.
`config.json` remains compatible with `sde-operator`: its connection fields are environment
references, not credential values. The convenience `operator` command loads the private local files.

Rerun setup with exactly the same bootstrap/directory after interruption. A completed setup checks
ownership and the current signed map; it does not provision again, restore revoked grants or replace
an advanced map. Changed native identities or missing ownership history require investigation.
Never erase a manifest to make a refusal disappear.

Doctor verifies the signature/model/project, native binding identity, direct restricted runtime
grants, engine/package versions and current connection TLS. Its result is scoped to
`local-weather-demo`; it does not qualify production certificate policy or deployment readiness.
The operator file retains the administrator credentials used for setup; selecting a namespace
is not a privilege boundary. Runtime privileges are restricted. Production provisioning credentials
need their own deployment-specific qualification. Both files stay customer-side.

## Generate and read actual data

```sh
.venv/bin/sde-weather --directory ./weather-demo run --iterations 20 --batch-size 10
./node_modules/.bin/sde-weather-ts --directory ./weather-demo run --iterations 20 --batch-size 10
```

Both programs use logical batch writes, point reads, bounded pages, counts and exact decimal
summaries. They compare the result to their own deterministic input. Each invocation has a random
run namespace, so repeating it or running both languages does not reuse another writer's keys.
`--workload point` adds point reads; `--workload analytics` keeps the scan/summary-heavy mix.
Limits are 1-1000 iterations and rows per batch, at most 10000 rows per invocation, 0-1000 ms
between batches and 0-30000 ms for bounded read/recovery attempts.

Each run writes `runs/RUN_ID/report.json` and a real SDK `window.json`. Reports contain counters,
versions, status and a pending sequence range, not rows or DSNs. Only the window is the telemetry
handoff to the controller. Its call count measures SDK operations: a batch is one call. The
elapsed workload time in the local report is separate from the SDK window, which has no interval
clock. Do not label either synthetic inputs or one local run as customer performance evidence.

Before each batch, the application persists its intended sequence range. If the write response is
lost, it checks the exact keys against the source using fresh local-map sessions. If the complete
batch is visible, `verified_after_uncertain_rows` distinguishes that evidence from an acknowledged
write. An absent/partial batch remains `incomplete` with `pending`; absence does not prove rollback.
The program never blindly repeats an uncertain write. Preserve this report for local inspection.
A killed process may leave `status: running`; that is an interrupted run, not success. Start a new
run namespace only after resolving whether your workflow can leave the interrupted range behind.

A refreshed map replaces and closes the previous owned session. Partial startup and normal/error
exit close owned connections. Read retries are bounded. A bad signature, mismatched value or
unresolved write stops the workload with a nonzero status.

## Verify earlier runs after a transition

A new successful workload does not establish that the previous data survived. Keep the completed
run IDs from both languages and verify them against the current source:

```sh
.venv/bin/sde-weather --directory ./weather-demo verify-runs --run-id FIRST_RUN_ID --run-id SECOND_RUN_ID
```

This Python command checks both Python and TypeScript runs. It reads each run's entire station
namespace through fresh source pages, compares every exact generated value and refuses missing or
extra rows. The logical map and the original report hashes must remain unchanged through cleanup.
Limits are 32 runs, 10000 rows per run and 100000 total per invocation.

New reports have protocol 2 and `generator_id`, a digest of the shared generator description.
The generated values remain identical to v1, including microseconds and UUIDs; the shared fixture
pins all 10000 supported sequences for two independent run IDs. Old protocol-1 reports lack the
generator identity and this verifier refuses them. Use a new demo with the current artifact; do
not rewrite an old report to make it look verified. Interrupted/incomplete reports also refuse.
Verification returns IDs, counts and fingerprints, never the regenerated or observed row values.

## Execute the approved Weather count locally

The controller operator supplies the current approved count-query packet over the authenticated
metadata channel. It has kind `sde-weather-count-query`, protocol 1, project/query/revision/map
identity, the complete approved query record and a digest. The pure SDK `make_count_request`
constructor creates this metadata; it does not open connections. The digest detects changed bytes,
while trust in who supplied the file comes from the authenticated handoff.

```sh
.venv/bin/sde-weather --directory ./weather-demo query-count --record ./weather-count.json
```

This is the Weather demo's bounded count oracle. It accepts only
`SELECT COUNT(*) [AS alias] FROM exact_source_table`, with `FINAL` required on ClickHouse. It
refuses filters, joins, arbitrary expressions/functions, settings, comments and additional
statements. Names and the query stamp must match the current signed source-only map; execute it
before staging or after controller completion. A general analyst SQL/code API remains separate.

The command verifies all completed local runs, plans and executes the exact admitted SQL using
runtime credentials, and compares its count to the locally expected total. The query has a
server-side execution limit; PostgreSQL additionally uses a read-only transaction. A changed map,
changed run catalog, unresolved run or unexpected data refuses a successful receipt.

Only a metadata observation is printed: query/revision/digest, project/map, run IDs and the local
verification outcome. The business value stays in
`query-results/EXECUTION_ID/result.json` with mode 0600; `receipt.json` contains only the observation.
Show the result locally during the demo. Send the receipt, not the result or full query-results
directory, to the controller. This is the customer's observation, not a controller-side data read.

## Use the existing operator

An operator supplies reviewed signed staging and cutover authorizations. Execute them locally:

```sh
.venv/bin/sde-weather --directory ./weather-demo operator status
.venv/bin/sde-weather --directory ./weather-demo operator stage --plan ./staging.json
.venv/bin/sde-weather --directory ./weather-demo operator execute --plan ./cutover.json
.venv/bin/sde-weather --directory ./weather-demo operator resume
```

Follow [staging](staging.md) and [local cutover](local-cutover.md) for the authorization, receipt and
controller-completion sequence. `resume` is for an interrupted existing operation. It does not
invent a decision. Only metadata/receipts cross to the controller; no command above needs its
network endpoint. The full assisted demo supplies the file handoffs in the appropriate order.

## Reset and repeat

Stop every workload and local operator process first. Read `allocation_id` from the local
`resources.json`, then explicitly confirm that allocation:

```sh
.venv/bin/sde-weather --directory ./weather-demo reset --confirm-allocation ALLOCATION_ID
```

Reset verifies native server and object identity plus ownership markers before dropping the
demo's resources. It is resumable and idempotent; an unrelated object with the same name is refused.
It does not use `DROP OWNED` or remove dependencies from other namespaces. A reset marker stops new
workloads in this directory. Keep the local reports/manifests for diagnosis; the next demo uses a
**new directory**, so its map enrollment and native watermark cannot collide with the previous run.
Do not transfer the credential files, operator state or full directory to the controller/support.

Measured scope and test evidence: [local starter acceptance](qualification/weather-starter/README.md).

Prior-run/COUNT evidence: [completion acceptance](qualification/weather-completion/README.md).

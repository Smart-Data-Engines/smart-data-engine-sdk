# Measure SDK operations from installed artifacts

`python/tests/qualification/operations.py` measures one source engine, one language and one
finite workload per invocation. The worker imports an installed SDK wheel or npm package.
The coordinator provisions disposable namespaces and restricted runtime accounts, and checks
every expected source value before reporting success. A failed cleanup also fails the run.

This is a **closed-loop measurement of SDK, driver and engine together**. Generation and result
comparison occur outside individual call timers; the enclosing elapsed time includes that work.
The result is not isolated SDK overhead and does not measure latency under independent arrivals.
Use [the fixed-schedule qualification](cutover-qualification.md) for scheduling delay, continuous
mixed-client traffic, cutover pauses and recovery. Its controller adapter is a separate experiment.

## Run

Use the normal native test DSNs with administrative rights for the disposable role fixture.
Prepare a fresh installed wheel environment and npm consumer as described in the qualification
guide. The coordinator needs the test dependencies; the workers only need their SDK dependencies.

```sh
PYTHONPATH=python/tests python/.venv/bin/python -m qualification.operations \
  --scratch /absolute/session-scratch/new-operation-run \
  --python /absolute/installed-wheel-venv/bin/python \
  --npm /absolute/npm-consumer/node_modules/@smart-data-engines/sde \
  --source postgres --language python --mode write \
  --method save_many --batch-size 100 --rows 100000 --telemetry on
```

Select `postgres` or `clickhouse`, and `python` or `typescript`. `save` requires batch size 1;
`save_many` uses native batches with a final partial batch when necessary. The read mode seeds
`--rows` before timing and repeats get/scan/count/summarize `--samples` times each. It walks a
deterministic set of point/range keys, checks complete returned pages, exact counts and decimal
sums. Reads use the declared source and ClickHouse logical entity semantics (`FINAL`).

Rows are bounded to one million, batches to 1000 and read samples to 10000. These are harness
bounds, not SDK capacity promises. Each worker has a finite 900-second completion timeout.
Use a separate supervisory timeout and record process exits when running a series.

## Interpret results

- `latency_ms` contains nearest-rank p50/p95/p99 over successful real calls. Always retain the
  sample count: a p99 from a few dozen samples has little resolution.
- `calls` and `rows` are distinct. A batch of 100 rows is one call. For count and summarize,
  `rows` is one result row, not the number of source rows scanned.
- `measured_call_seconds` sums call times. `elapsed_seconds` covers the measured loop, including
  generation, validation and sample bookkeeping. Neither includes provisioning, warmup or the
  final independent source oracle; the latter has its own `oracle_ns`.
- Write warmup uses two calls in a separate synthetic key namespace in the same table. Read
  warmup uses twenty point reads. The run makes no cold-cache or fully warmed query-plan claim.
- `max_rss_kib` covers the whole worker, including SDK, driver, samples and validation buffers.
  `--telemetry off` permits a paired comparison with the otherwise identical workload.
- `report.json` has `passed: true` only after the worker, complete value oracle, workload-accounting
  checks and fixture cleanup succeed. Timings cannot compensate for missing or substituted work.
  `worker.json` retains samples; stdout/stderr remain local diagnostic artifacts.

Run repeated paired comparisons on the same quiet host and alternate order between repetitions.
Record engine/runtime versions, CPU/memory/storage configuration, TLS and durability policies,
artifact hashes, host contention, warmup and offered workload. Preserve unsuccessful runs. Do not
attribute a change between machines or engine versions to the SDK, and do not treat a shared-host
result as an isolated backend capacity number. Use scoped, reviewed evidence when publishing;
never bundle runtime credentials or raw engine state into the measurement attachment.


Fixture destruction uses a separate administrative connection with at least 300 seconds of
receive-inactivity allowance (or the caller's already longer value). Large ClickHouse runs can
leave many physical parts for synchronous removal; a measured 100k-row run passed its exact oracle
but its former 15-second DROP timeout correctly prevented acceptance. This cleanup allowance does
not change the application's DSN, measured calls, absence-of-replay policy or workload criteria.
Cleanup failure still makes the run fail. Preserve and inspect that result, then use a fresh case
identity; do not relabel the original failed trial as accepted.

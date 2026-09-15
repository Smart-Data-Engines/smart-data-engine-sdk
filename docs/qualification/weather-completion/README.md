# Prior-run verification and local COUNT acceptance — 15 September 2026

[acceptance.json](acceptance.json) records the tested implementation, complete suites, installed
artifact hashes, source/language cases and all mutation witnesses. The profile is the synthetic
Weather application, completed protocol-2 reports and an exact COUNT over its current source.

Both clients preserve the original generator's values. A descriptor identifies that generator;
a shared frozen digest from the previously merged implementation covers every supported sequence
for two run IDs (20000 rows), including exact microseconds, decimals and UUIDs. The new verifier
refuses legacy reports that cannot identify their generator or unresolved run state.

A fresh wheel environment and a separate npm application each ran on both source engines. Each
source case had Python and TypeScript write 21 rows apiece, verify both earlier runs, execute the
approved COUNT, retain its value only in the local mode-0600 result file, refuse an old stamp and
reset twice. Imports were checked against installed package locations. These particular artifact
trials use an explicitly identified fixture signer; they do not replace the full private
controller/AI demonstration.

Review found one real lexical defect. Python's Unicode IGNORECASE could accept a dotless-i
identifier instead of the ASCII source table. A PostgreSQL test created both distinct native
objects with the same row count and demonstrated a false successful observation of the wrong
query. Five further lexical cases were also red before `re.ASCII` restricted token folding.
Quoted identifiers retain exact spelling. All 55 COUNT cases now pass, including map/report
changes during actual native execution and refusal to export the business value in a receipt.

Fourteen targeted mutations were detected. Each original and restored control passed, and every
selected witness failed for its intended behavior. The report distinguishes control phases from
witness-case counts because several mutations exercise two independent witnesses. Mutations began
only after commit and restored exact source bytes. They cover report identity, source authority,
exact/missing/extra values, context rechecks, cleanup, Unicode source selection, FINAL, query stamp,
local count equality and result privacy.

The source suite uses the supported ClickHouse driver floor; fresh artifact installation also
exercised clickhouse-connect 1.8.0 and psycopg 3.3.5. No release tag or registry publication is part
of this acceptance. See the [runbook](../../weather-starter.md) for the customer-side commands.

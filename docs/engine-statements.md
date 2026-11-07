# What the libraries and the operator send to your engines

This page lists every statement the libraries and the local operator can send to your engines **that
changes something**, by engine: who sends it, under which login, and when. Reads are not listed:
they change nothing, and the runtime login's grants ([runtime roles](runtime-roles.md)) bound them.
`…` stands for an identifier taken from the placement map and quoted for the engine, or for a value
bound as a parameter. It is never a value spliced into the text.

`python/tests/test_engine_statements.py` reads the Python library's source. It fails on a statement
form this page does not list, and on a listed form the source no longer sends. A form the source
builds where a reader of literals cannot follow is named in the test, together with the line that
builds it. For TypeScript and C++ the test reads how each statement begins, `ALTER TABLE` or
`LOCK TABLE`, and fails on an opening the page has no form for.

Who sends it:

- **application**: your process, through a `Session`, with its runtime login. The TypeScript and C++
  libraries send only the application's and provisioning's statements. The operator, and with it
  every other row, is in the Python library.
- **provisioning**: `prepare_schema`, with the operator's login;
- **operator**: `sde-operator`, when you run it on a signed instruction, with the operator's login;
- **departure**: `sde-operator depart --execute`, or you running the text it lists
  ([leaving](departure.md)).

## PostgreSQL

| Statement | Sent by | When |
|---|---|---|
| `CREATE TABLE IF NOT EXISTS … (…, PRIMARY KEY (…))` | provisioning; application on `ensure_schema` | a table of the map is missing |
| `CREATE INDEX IF NOT EXISTS …` | provisioning; application on `ensure_schema` | an index of the map is missing |
| `CREATE TABLE IF NOT EXISTS … (… bigint NOT NULL, … text NOT NULL, … timestamptz NOT NULL DEFAULT now())` | provisioning | `sde_map_state`, for the first signed map |
| `CREATE TABLE IF NOT EXISTS … (… text NOT NULL, … text NOT NULL, … bigint NOT NULL, … timestamptz NOT NULL DEFAULT now())` | `sde.backfill` | `sde_backfill_state`, for the first backfill |
| `INSERT INTO … (…) VALUES (…)`, `INSERT INTO … (…) VALUES …` | application | `save`, `save_many` |
| `INSERT INTO … (…, …) VALUES (%s, %s)` | application | a newer signed map is loaded: its version, into `sde_map_state` |
| `INSERT INTO … (…, …, …) VALUES (%s, %s, %s)` | `sde.backfill` | its progress, into `sde_backfill_state` |
| `INSERT INTO … (…) VALUES … ON CONFLICT DO NOTHING` | `sde.backfill`; operator | a backfill, or a cutover refilling the copy |
| `ALTER TABLE … ADD COLUMN IF NOT EXISTS … bigint NOT NULL DEFAULT 0` | provisioning | fence setup: `__sde_write_epoch` |
| `ALTER TABLE … ADD CONSTRAINT … CHECK (…) NOT VALID` | provisioning; operator | fence setup, a barrier, a generation change: `__sde_f_*` only |
| `ALTER TABLE … DROP CONSTRAINT IF EXISTS …` | provisioning; operator; departure | fence setup finishing, a barrier released, a generation changed, leaving: `__sde_f_*` only |
| `LOCK TABLE … IN SHARE ROW EXCLUSIVE MODE` | provisioning; operator | a barrier waits for the writers in flight |
| `GRANT SELECT, INSERT ON … TO …`, `REVOKE SELECT, INSERT ON … FROM …` | operator | a cutover pauses and restores your runtime logins, and retires a source |
| `TRUNCATE TABLE …` | operator | a cutover empties the copy it refills, after checking its identity |
| `CREATE TABLE …`, then `COMMENT ON TABLE … IS …` | operator | a staging creates a copy with the first statement of this table, without `IF NOT EXISTS`, and marks it its own |
| `CREATE INDEX …` | operator | a staging builds its copy's indexes |
| `DROP TABLE …` | operator | an abandoned staging drops its own marked copy |
| `CREATE INDEX CONCURRENTLY …`, `DROP INDEX CONCURRENTLY …`, `DROP INDEX CONCURRENTLY IF EXISTS …` | operator | an index change in place |
| `ALTER TABLE … DROP COLUMN IF EXISTS …`, `DROP TABLE IF EXISTS …` | departure | leaving, with `--cleanup`: the generation column and the bookkeeping tables |
| `SET TRANSACTION READ ONLY` | application | explaining a query's plan, in a transaction that is rolled back |
| `SET DateStyle TO ISO` | application, C++ | on connecting, so that dates are read in one format |

## ClickHouse

| Statement | Sent by | When |
|---|---|---|
| `CREATE TABLE IF NOT EXISTS … (…) ENGINE = ReplacingMergeTree …ORDER BY (…)` | provisioning; application on `ensure_schema` | a table of the map is missing, with its data-skipping indexes |
| `CREATE TABLE IF NOT EXISTS … (… Int64, … String, … DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')) ENGINE = MergeTree ORDER BY (…)` | provisioning | `sde_map_state`, for the first signed map |
| `CREATE TABLE IF NOT EXISTS … (… String, … String, … Int64, … DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')) ENGINE = MergeTree ORDER BY (…, …)` | `sde.backfill` | `sde_backfill_state`, for the first backfill |
| `CREATE TABLE IF NOT EXISTS … (table_name String, table_uuid UUID, project_id FixedString(32), hold String) ENGINE=MergeTree ORDER BY (table_uuid, project_id, hold)` | provisioning; operator | `__sde_fence_drains`, for the first drain |
| `INSERT` built by the driver from the rows; `INSERT INTO … (…) FORMAT JSONEachRow` from TypeScript | application; `sde.backfill`; operator | `save`, `save_many`, a map version, backfill progress, a copy's refill, a drain's record |
| `ALTER TABLE … ADD COLUMN IF NOT EXISTS … Int64 DEFAULT 0` | provisioning | fence setup: `__sde_write_epoch` |
| `ALTER TABLE … ADD CONSTRAINT IF NOT EXISTS … CHECK …` | provisioning; operator | fence setup, a barrier, a generation change: `__sde_f_*` only |
| `ALTER TABLE … DROP CONSTRAINT IF EXISTS …` | provisioning; operator; departure | fence setup finishing, a barrier released, a generation changed, leaving: `__sde_f_*` only |
| `DETACH TABLE … PERMANENTLY SYNC`, then `ATTACH TABLE …` | provisioning; operator | a barrier waits for the writers in flight, recorded first in `__sde_fence_drains` |
| `GRANT SELECT, INSERT ON … TO …`, `REVOKE SELECT, INSERT ON … FROM …` | operator | a cutover pauses and restores your runtime logins and retires a source; a dropped copy's grants are taken back |
| `TRUNCATE TABLE … SYNC` | operator | a cutover empties the copy it refills, after checking its identity |
| `CREATE TABLE …`, `CREATE TABLE … COMMENT '…'` | operator | a staging creates a copy with the first statement of this table, without `IF NOT EXISTS`, marked as its own |
| `DROP TABLE …` | operator | an abandoned staging drops its own marked copy, with `SYNC` |
| `ALTER TABLE … ADD …`, `ALTER TABLE … MATERIALIZE INDEX …` | operator | an index change builds a data-skipping index |
| `ALTER TABLE … DROP INDEX … SETTINGS alter_sync = 0` | operator | an index change removes one |
| `KILL MUTATION WHERE database = currentDatabase() AND table = '…' AND mutation_id = '…'` | operator | an index change stops its own materialization of an index it removes |
| `ALTER TABLE … DROP COLUMN IF EXISTS …`, `DROP TABLE IF EXISTS …` | departure | leaving, with `--cleanup`; the tables with `SYNC` |

## Elasticsearch

Requests, in console syntax:

| Request | Sent by | When |
|---|---|---|
| `PUT /…` | provisioning; application on `ensure_schema`; operator | an index of the map is missing, `sde_map_state` and `sde_backfill_state` on first use, a staging's copy: each with its mapping |
| `PUT /…/_doc/…?refresh=wait_for`, `POST /_bulk?refresh=wait_for` | application | `save`, `save_many`, a map version |
| `POST /…/_doc?refresh=true` | `sde.backfill` | its progress, into `sde_backfill_state` |
| `POST /_bulk?refresh=true` | `sde.backfill`; operator | a backfill, or a cutover refilling the copy |
| `PUT /…/_mapping` | provisioning | fence setup: `__sde_write_epoch` |
| `PUT /_ingest/pipeline/…` | provisioning; operator | fence setup, a barrier, a generation change: the fence pipeline `__sde_f_<index>`, conditional on its version |
| `PUT /…/_settings` | provisioning; operator; departure | the index's final pipeline set to its fence or unset when leaving, a write block lifted |
| `PUT /…/_block/write`, `POST /…/_refresh` | provisioning; operator | a barrier waits for the writes in flight |
| `PUT /…/_block/read` | operator | a cutover retires a source |
| `POST /…/_delete_by_query?refresh=true&conflicts=abort` | operator | a cutover empties the copy it refills, after checking its identity |
| `DELETE /…` | operator | an abandoned staging deletes its own marked copy |
| `DELETE /…?ignore_unavailable=true` | departure | leaving, with `--cleanup`: the bookkeeping indexes |
| `DELETE /_ingest/pipeline/…` | operator; departure | an abandoned staging's copy, and leaving: the fence pipeline `__sde_f_<index>` |

The operator also asks Elasticsearch whether a runtime key holds a privilege, with a request that
changes nothing: `_security/user/_has_privileges`.

## The orderbook engine

The library writes levels and flushes them. It runs no DDL there and keeps nothing of its own: the
engine fixes its schema, and a group on it carries no write generation.

"""Three engine behaviours the in-place index design rests on, measured on the SDK's test engines."""
import json, os, uuid
import psycopg
import clickhouse_connect

out = {}
pg = psycopg.connect(os.environ["SDE_POSTGRES_DSN"], autocommit=True)
schema = "probe_" + uuid.uuid4().hex[:12]
pg.execute(f"CREATE SCHEMA {schema}"); pg.execute(f"SET search_path TO {schema}")
try:
    pg.execute("CREATE TABLE t (id bigint PRIMARY KEY, value integer)")
    pg.execute("INSERT INTO t SELECT g, g % 100 FROM generate_series(1, 1000) g")
    # A duplicate-free unique index build on duplicated values fails CONCURRENTLY and leaves the
    # index behind INVALID - the leftover a crashed or cancelled build leaves too.
    try:
        pg.execute("CREATE UNIQUE INDEX CONCURRENTLY leftover ON t (value)")
    except Exception as exc:
        out["pg_failed_build"] = type(exc).__name__
    row = pg.execute("SELECT i.indisvalid, i.indisready FROM pg_index i WHERE i.indexrelid = to_regclass('leftover')").fetchone()
    out["pg_leftover_valid_ready"] = None if row is None else list(row)
    notices = []
    pg.add_notice_handler(lambda d: notices.append(d.message_primary))
    pg.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS leftover ON t (value)")
    row = pg.execute("SELECT i.indisvalid, i.indisready, i.indisunique FROM pg_index i WHERE i.indexrelid = to_regclass('leftover')").fetchone()
    out["pg_if_not_exists_over_invalid"] = {"notices": list(notices), "valid_ready_unique_after": list(row)}
    pg.execute("DROP INDEX CONCURRENTLY IF EXISTS leftover")
    out["pg_after_drop"] = pg.execute("SELECT to_regclass('leftover')").fetchone()[0]
finally:
    pg.execute(f"DROP SCHEMA {schema} CASCADE")

dsn = os.environ["SDE_CLICKHOUSE_DSN"]  # clickhouse://default:sde@127.0.0.1:58123/sde
from urllib.parse import urlsplit
p = urlsplit(dsn)
ch = clickhouse_connect.get_client(host=p.hostname, port=p.port, username=p.username, password=p.password)
db = "probe_" + uuid.uuid4().hex[:12]
ch.command(f"CREATE DATABASE {db} ENGINE=Atomic")
try:
    ch.command(f"CREATE TABLE {db}.t (id Int64, value Int32) ENGINE = ReplacingMergeTree ORDER BY id")
    for batch in range(5):
        ch.command(f"INSERT INTO {db}.t SELECT number + {batch*1000}, number % 100 FROM numbers(1000)")
    ch.command(f"ALTER TABLE {db}.t ADD INDEX `sde_i_abc_000001` value TYPE minmax GRANULARITY 4")
    out["ch_index_after_add"] = ch.query(f"SELECT name, type_full, expr, granularity FROM system.data_skipping_indices WHERE database='{db}' AND table='t'").result_rows
    ch.command(f"ALTER TABLE {db}.t MATERIALIZE INDEX `sde_i_abc_000001`")
    rows = ch.query(f"SELECT mutation_id, command, is_done, parts_to_do, latest_fail_reason FROM system.mutations WHERE database='{db}' AND table='t'").result_rows
    out["ch_mutation_rows"] = [list(map(str, r)) for r in rows]
    ch.command(f"ALTER TABLE {db}.t ADD INDEX IF NOT EXISTS `sde_i_abc_000001` value TYPE set(10) GRANULARITY 1")
    out["ch_if_not_exists_other_shape"] = ch.query(f"SELECT name, type_full, granularity FROM system.data_skipping_indices WHERE database='{db}' AND table='t'").result_rows
finally:
    ch.command(f"DROP DATABASE {db}")
print(json.dumps(out, indent=1, default=str))

"""ClickHouse 24.8: do table grants outlive DROP TABLE, and does REVOKE accept a dropped table?

    SDE_CLICKHOUSE_DSN=clickhouse://default:sde@127.0.0.1:58123/sde python probe_grants_after_drop.py
"""
import json, os, uuid
from urllib.parse import urlsplit
import clickhouse_connect
p = urlsplit(os.environ["SDE_CLICKHOUSE_DSN"])
root = clickhouse_connect.get_client(host=p.hostname, port=p.port, username=p.username, password=p.password)
db, user = "probe_" + uuid.uuid4().hex[:12], "probe_user_" + uuid.uuid4().hex[:8]
out = {}
root.command(f"CREATE DATABASE {db} ENGINE = Atomic")
root.command(f"CREATE USER {user} IDENTIFIED WITH sha256_password BY 'x{uuid.uuid4().hex}'")
try:
    root.command(f"CREATE TABLE {db}.t (id Int64) ENGINE = MergeTree ORDER BY id")
    root.command(f"GRANT SELECT, INSERT ON {db}.t TO {user}")
    grants = lambda: [list(map(str, r)) for r in root.query(f"SELECT access_type, database, table FROM system.grants WHERE user_name = '{user}' ORDER BY access_type").result_rows]
    out["after_grant"] = grants()
    root.command(f"DROP TABLE {db}.t SYNC")
    out["after_drop_table"] = grants()
    try:
        root.command(f"REVOKE SELECT, INSERT ON {db}.t FROM {user}")
        out["revoke_on_dropped_table"] = "accepted"
    except Exception as exc:
        out["revoke_on_dropped_table"] = "refused: " + str(exc)[:160]
    out["after_revoke"] = grants()
finally:
    root.command(f"DROP USER IF EXISTS {user}")
    root.command(f"DROP DATABASE IF EXISTS {db} SYNC")
print(json.dumps(out, indent=1))

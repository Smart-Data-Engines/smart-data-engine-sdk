"""The ALTERs ClickHouse ran since a moment, with their duration and every non-zero profile event
that names locks, syncs, writes or merges: where an ALTER's time went."""
import json
import os
import sys
import urllib.parse

import clickhouse_connect

since = sys.argv[1]
u = urllib.parse.urlsplit(os.environ["SDE_CLICKHOUSE_DSN"])
cx = clickhouse_connect.get_client(
    host=u.hostname, port=u.port, username=urllib.parse.unquote(u.username or "default"),
    password=urllib.parse.unquote(u.password or ""), database=u.path.lstrip("/") or "default",
)
cx.command("SYSTEM FLUSH LOGS")
rows = cx.query(
    "SELECT event_time_microseconds, query_duration_ms, query, ProfileEvents FROM system.query_log "
    "WHERE type = 'QueryFinish' AND query_kind = 'Alter' AND event_time >= {since:DateTime} "
    "ORDER BY event_time_microseconds",
    parameters={"since": since},
).result_rows
out = []
for at, ms, query, events in rows:
    keep = {k: v for k, v in events.items() if v and any(
        s in k for s in ("Lock", "Sync", "Write", "Merge", "Elapsed", "Wait", "Microseconds"))}
    out.append({"at": str(at), "ms": int(ms), "query": query[:140], "events": keep})
    print(f"{ms:6d} ms  {query[:100]}")
    for k in sorted(keep):
        if "Microseconds" in k or "Milliseconds" in k or "Wait" in k or "Sync" in k:
            print(f"           {k} = {keep[k]}")
json.dump(out, sys.stdout if len(sys.argv) < 3 else open(sys.argv[2], "w"), indent=1)

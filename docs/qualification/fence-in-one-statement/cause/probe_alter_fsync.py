#!/usr/bin/env python3
"""Is an advance's ALTER on a ClickHouse copy slow because of its metadata fsync?

Each ALTER TABLE ... ADD/DROP CONSTRAINT writes the table's metadata file and fsyncs it once
(`FileSync` = 1 in its query_log entry). On a quiet disk that is 1.5 ms. This times the advance's
four statements - and the same four changes as one statement - with the disk quiet, and with
another process writing and fsyncing beside them, as a backfill's merges or another tenant would.

Usage: probe_alter_fsync.py <out.json> <pressure file> [rounds]
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse

import clickhouse_connect

TABLE = "sde_probe_alter_fsync"
STATEMENTS = [
    f"ALTER TABLE {TABLE} ADD CONSTRAINT IF NOT EXISTS __sde_f_lo CHECK __sde_write_epoch >= 2",
    f"ALTER TABLE {TABLE} ADD CONSTRAINT IF NOT EXISTS __sde_f_hi CHECK __sde_write_epoch <= 2",
    f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS __sde_f_lo",
    f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS __sde_f_hi",
]
ONE = (
    f"ALTER TABLE {TABLE} ADD CONSTRAINT IF NOT EXISTS __sde_f_lo CHECK __sde_write_epoch >= 2, "
    "ADD CONSTRAINT IF NOT EXISTS __sde_f_hi CHECK __sde_write_epoch <= 2, "
    "DROP CONSTRAINT IF EXISTS __sde_f_lo, DROP CONSTRAINT IF EXISTS __sde_f_hi"
)
WRITER = """
import os, sys, time
path, seconds = sys.argv[1], float(sys.argv[2])
chunk = os.urandom(8 << 20)
end = time.monotonic() + seconds
with open(path, "wb") as f:
    written = 0
    while time.monotonic() < end:
        f.write(chunk); written += len(chunk)
        f.flush(); os.fsync(f.fileno())
        if written >= 512 << 20:
            f.seek(0); written = 0
"""


def client():  # type: ignore[no-untyped-def]
    u = urllib.parse.urlsplit(os.environ["SDE_CLICKHOUSE_DSN"])
    return clickhouse_connect.get_client(
        host=u.hostname, port=u.port, username=urllib.parse.unquote(u.username or "default"),
        password=urllib.parse.unquote(u.password or ""), database=u.path.lstrip("/") or "default",
    )


def run(cx, statements: list[str]) -> list[float]:  # type: ignore[no-untyped-def]
    out = []
    for statement in statements:
        started = time.perf_counter()
        cx.command(statement)
        out.append(round((time.perf_counter() - started) * 1000, 1))
    return out


def fsyncs(cx, since: str) -> list[dict[str, object]]:  # type: ignore[no-untyped-def]
    cx.command("SYSTEM FLUSH LOGS")
    rows = cx.query(
        "SELECT query_duration_ms, ProfileEvents['FileSyncElapsedMicroseconds'], "
        "ProfileEvents['FileSync'] FROM system.query_log WHERE type='QueryFinish' "
        "AND query_kind='Alter' AND event_time >= {since:DateTime} AND query LIKE {like:String} "
        "ORDER BY event_time_microseconds",
        parameters={"since": since, "like": f"%{TABLE}%CONSTRAINT%"},
    ).result_rows
    return [{"ms": int(a), "fsync_ms": round(int(b) / 1000, 1), "fsyncs": int(c)} for a, b, c in rows]


def main() -> int:
    out_path, pressure, rounds = sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 5
    cx = client()
    version = cx.query("SELECT version()").result_rows[0][0]
    cx.command(f"DROP TABLE IF EXISTS {TABLE} SYNC")
    cx.command(
        f"CREATE TABLE {TABLE} (id Int64, name String, __sde_write_epoch Int64 DEFAULT 0) "
        "ENGINE=MergeTree ORDER BY id"
    )
    cx.command(f"INSERT INTO {TABLE} (id, name) SELECT number, toString(number) FROM numbers(2000000)")
    results: dict[str, list[dict[str, object]]] = {}
    for condition in ("quiet", "pressure"):
        writer = None
        if condition == "pressure":
            writer = subprocess.Popen([sys.executable, "-I", "-c", WRITER, pressure, "600"])
            time.sleep(3)  # the writer's dirty data and syncs under way
        for form in ("four statements", "one statement"):
            since = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 1))
            client_side = []
            for _ in range(rounds):
                client_side.append(run(cx, STATEMENTS if form == "four statements" else [ONE]))
                time.sleep(0.2)
            server = fsyncs(cx, since)
            results[f"{condition}, {form}"] = [{"client_ms": c} for c in client_side] + [{"server": server}]
            totals = [round(sum(c), 1) for c in client_side]
            print(f"{condition:9} {form:16} totals ms {totals}  server fsync ms "
                  f"{[s['fsync_ms'] for s in server]}", flush=True)
        if writer is not None:
            writer.terminate()
            writer.wait()
            os.remove(pressure)
    cx.command(f"DROP TABLE IF EXISTS {TABLE} SYNC")
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump({"clickhouse": version, "rounds": rounds, "results": results}, handle, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

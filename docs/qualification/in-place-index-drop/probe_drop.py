"""What removing an index from a live table does, on both engines, before the design relies on it.

Usage (the SDK's test engines):
    SDE_POSTGRES_DSN=... SDE_CLICKHOUSE_DSN=... python probe_drop.py > probe_drop.out.json

PostgreSQL: ``DROP INDEX CONCURRENTLY`` while a writer inserts a row every 5 ms - the longest gap
between writes; whether an old snapshot that has not touched the table holds it, and whether an
open transaction that has read the table does; what a terminated drop leaves in the catalogue, and
whether a second drop removes that. ClickHouse: ``ALTER TABLE ... DROP INDEX`` with
``alter_sync = 0`` while the writer runs, and whether a read the index served still answers.
Everything is created in a probe namespace of its own and removed at the end.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any

import clickhouse_connect
import psycopg
from psycopg import sql

ROWS = 100_000


class Writer(threading.Thread):
    """Inserts one row every 5 ms and records the longest gap between two successful inserts."""

    def __init__(self, insert: Any) -> None:
        super().__init__(daemon=True)
        self.insert, self.stop, self.gaps, self.failures = insert, threading.Event(), [], 0

    def run(self) -> None:
        last, number = time.monotonic(), 10_000_000
        while not self.stop.is_set():
            try:
                self.insert(number)
                now = time.monotonic()
                self.gaps.append(now - last)
                last = now
            except Exception:
                self.failures += 1
            number += 1
            time.sleep(0.005)

    def longest(self, since: int = 0) -> float:
        return round(max(self.gaps[since:], default=0.0) * 1000, 1)


def postgres(dsn: str) -> dict[str, Any]:
    schema = "sde_probe_drop_" + uuid.uuid4().hex[:12]
    out: dict[str, Any] = {"server_version": None}
    admin = psycopg.connect(dsn, autocommit=True)
    try:
        out["server_version"] = admin.execute("SHOW server_version").fetchone()[0]
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        admin.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        admin.execute("CREATE TABLE t (id bigint PRIMARY KEY, v int NOT NULL)")
        admin.execute("INSERT INTO t SELECT g, g %% 1000 FROM generate_series(1, %s) g", (ROWS,))
        options = "-csearch_path=" + schema

        def connection() -> psycopg.Connection[Any]:
            return psycopg.connect(dsn, autocommit=True, options=options)

        writer_cx = connection()
        writer = Writer(lambda n: writer_cx.execute("INSERT INTO t VALUES (%s, %s)", (n, n % 1000)))

        # 1. A plain concurrent drop, with the writer running.
        admin.execute("CREATE INDEX i_plain ON t (v)")
        writer.start()
        time.sleep(1.0)
        before = writer.longest()
        mark = len(writer.gaps)
        started = time.monotonic()
        admin.execute("DROP INDEX CONCURRENTLY i_plain")
        out["plain_drop"] = {
            "rows": ROWS,
            "drop_ms": round((time.monotonic() - started) * 1000, 1),
            "longest_write_gap_ms_before": before,
        }
        time.sleep(0.5)
        out["plain_drop"]["longest_write_gap_ms_during"] = writer.longest(mark)

        # 2a. An old snapshot on another connection that has not touched the table. A concurrent
        # build waits for every older snapshot; does a concurrent drop?
        admin.execute("CREATE INDEX i_snapshot ON t (v)")
        snapshot = connection()
        snapshot.autocommit = False
        snapshot.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        snapshot.execute("SELECT 1")  # takes the snapshot; holds no lock on t
        snapshot_dropper = connection()
        snapshot_result: dict[str, Any] = {}

        def drop_beside_a_snapshot() -> None:
            began = time.monotonic()
            snapshot_dropper.execute("DROP INDEX CONCURRENTLY i_snapshot")
            snapshot_result["ms"] = round((time.monotonic() - began) * 1000, 1)

        thread = threading.Thread(target=drop_beside_a_snapshot, daemon=True)
        thread.start()
        thread.join(3.0)
        out["beside_an_old_snapshot_that_did_not_read_the_table"] = {
            "finished_within_3_s": not thread.is_alive(),
            "drop_ms": snapshot_result.get("ms"),
            "index_after": "gone"
            if admin.execute("SELECT to_regclass('i_snapshot')").fetchone()[0] is None
            else "present",
        }
        snapshot.rollback()
        snapshot.close()
        thread.join(10)
        snapshot_dropper.close()

        # 2b. An open transaction on another connection that has read the table: it holds a lock
        # on the table until it ends. Does the drop wait for it?
        admin.execute("CREATE INDEX i_held ON t (v)")
        holder = connection()
        holder.autocommit = False
        holder.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        holder.execute("SELECT count(*) FROM t WHERE v = 7")  # AccessShareLock on t, kept open
        dropper = connection()
        pid = dropper.execute("SELECT pg_backend_pid()").fetchone()[0]
        result: dict[str, Any] = {}

        def drop_held() -> None:
            began = time.monotonic()
            try:
                dropper.execute("DROP INDEX CONCURRENTLY i_held")
                result["outcome"] = "dropped"
            except Exception as exc:  # terminated below
                result["outcome"] = type(exc).__name__
            result["ms"] = round((time.monotonic() - began) * 1000, 1)

        thread = threading.Thread(target=drop_held, daemon=True)
        mark = len(writer.gaps)
        thread.start()
        time.sleep(3.0)
        waiting = admin.execute(
            "SELECT state, wait_event_type, wait_event FROM pg_stat_activity WHERE pid = %s", (pid,)
        ).fetchone()
        catalogue = admin.execute(
            "SELECT i.indisvalid, i.indisready FROM pg_index i "
            "WHERE i.indexrelid = to_regclass('i_held')"
        ).fetchone()
        out["held_by_a_transaction_that_read_the_table"] = {
            "still_running_after_3_s": thread.is_alive(),
            "activity": list(waiting) if waiting else None,
            "index_while_waiting": {"valid": catalogue[0], "ready": catalogue[1]}
            if catalogue
            else "gone",
            "longest_write_gap_ms_while_waiting": writer.longest(mark),
        }

        # 3. The waiting drop terminated: what does it leave?
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
        thread.join(10)
        left = admin.execute(
            "SELECT i.indisvalid, i.indisready FROM pg_index i "
            "WHERE i.indexrelid = to_regclass('i_held')"
        ).fetchone()
        out["terminated_drop"] = {
            "dropper_outcome": result.get("outcome"),
            "index_left": {"valid": left[0], "ready": left[1]} if left else "gone",
        }
        holder.rollback()
        holder.close()

        # 4. A second concurrent drop over what the terminated one left.
        started = time.monotonic()
        admin.execute("DROP INDEX CONCURRENTLY i_held")
        out["second_drop"] = {
            "drop_ms": round((time.monotonic() - started) * 1000, 1),
            "index_after": "gone"
            if admin.execute("SELECT to_regclass('i_held')").fetchone()[0] is None
            else "present",
        }
        writer.stop.set()
        writer.join(5)
        out["writer"] = {"inserts": len(writer.gaps), "failures": writer.failures}
        writer_cx.close()
        dropper.close()
    finally:
        admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        admin.close()
    return out


def clickhouse(dsn: str) -> dict[str, Any]:
    database = "sde_probe_drop_" + uuid.uuid4().hex[:12]
    root = clickhouse_connect.get_client(dsn=dsn)
    out: dict[str, Any] = {"server_version": root.server_version}
    try:
        root.command(f"CREATE DATABASE {database} ENGINE = Atomic")
        client = clickhouse_connect.get_client(dsn=dsn, database=database)
        client.command(
            "CREATE TABLE t (id Int64, v Int32, INDEX i_v v TYPE minmax GRANULARITY 1) "
            "ENGINE = ReplacingMergeTree ORDER BY id"
        )
        client.command(f"INSERT INTO t SELECT number, number % 1000 FROM numbers({ROWS})")
        writer_client = clickhouse_connect.get_client(dsn=dsn, database=database)
        writer = Writer(lambda n: writer_client.command(f"INSERT INTO t VALUES ({n}, {n % 1000})"))
        writer.start()
        time.sleep(1.0)
        before = writer.longest()
        mark = len(writer.gaps)
        answer = client.query("SELECT count() FROM t FINAL WHERE v >= 990").result_rows[0][0]
        started = time.monotonic()
        client.command("ALTER TABLE t DROP INDEX i_v SETTINGS alter_sync = 0")
        dropped_ms = round((time.monotonic() - started) * 1000, 1)
        listed = client.query(
            "SELECT count() FROM system.data_skipping_indices "
            "WHERE database = currentDatabase() AND table = 't' AND name = 'i_v'"
        ).result_rows[0][0]
        after = client.query("SELECT count() FROM t FINAL WHERE v >= 990").result_rows[0][0]
        time.sleep(0.5)
        writer.stop.set()
        writer.join(5)
        out["drop"] = {
            "rows": ROWS,
            "alter_ms": dropped_ms,
            "listed_in_the_catalogue_after": listed,
            "a_read_the_index_served_answers": after >= answer,
            "longest_write_gap_ms_before": before,
            "longest_write_gap_ms_during": writer.longest(mark),
        }
        out["writer"] = {"inserts": len(writer.gaps), "failures": writer.failures}
    finally:
        root.command(f"DROP DATABASE IF EXISTS {database} SYNC")
    return out


if __name__ == "__main__":
    print(
        json.dumps(
            {
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "postgres": postgres(os.environ["SDE_POSTGRES_DSN"]),
                "clickhouse": clickhouse(os.environ["SDE_CLICKHOUSE_DSN"]),
            },
            indent=2,
        )
    )

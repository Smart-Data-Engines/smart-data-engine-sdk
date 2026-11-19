#!/usr/bin/env python3
"""Why does `ALTER TABLE ... ADD/DROP CONSTRAINT` take 0.6-1.3 s on a ClickHouse copy in an
advance's pause, with no lock waited for?

Hypothesis: an ALTER of a MergeTree table stops the table's running merges and waits for them to
stop, and after a backfill of millions of rows the merges are long ones; each of the advance's
statements pays it again, since the merges start over after each.

Each case times the advance's statements on one table of ROWS rows inserted as BLOCKS parts, and
records what `system.merges` showed for the table just before each statement:
- `merging`: right after the insert, while the table merges its parts;
- `merged`: after the merges are done (none running);
- `stopped`: merges stopped with SYSTEM STOP MERGES before the statements, started after;
- `one-statement`: while merging, the four constraint changes as one ALTER of four commands.

Usage: probe_alter_merges.py <out.json> [rounds]
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.parse

import clickhouse_connect

ROWS = 4_000_000
BLOCKS = 40
TABLE = "sde_probe_alter_merges"


def client():  # type: ignore[no-untyped-def]
    u = urllib.parse.urlsplit(os.environ["SDE_CLICKHOUSE_DSN"])
    return clickhouse_connect.get_client(
        host=u.hostname, port=u.port, username=urllib.parse.unquote(u.username or "default"),
        password=urllib.parse.unquote(u.password or ""), database=u.path.lstrip("/") or "default",
    )


def merges(cx) -> list[dict[str, object]]:  # type: ignore[no-untyped-def]
    rows = cx.query(
        "SELECT elapsed, progress, num_parts, total_size_bytes_compressed FROM system.merges "
        "WHERE database=currentDatabase() AND table={t:String}",
        parameters={"t": TABLE},
    ).result_rows
    return [
        {"elapsed": float(e), "progress": float(p), "parts": int(n), "bytes": int(b)}
        for e, p, n, b in rows
    ]


def active_parts(cx) -> int:  # type: ignore[no-untyped-def]
    return int(cx.query(
        "SELECT count() FROM system.parts WHERE database=currentDatabase() AND table={t:String} "
        "AND active", parameters={"t": TABLE},
    ).result_rows[0][0])


def fill(cx) -> None:  # type: ignore[no-untyped-def]
    cx.command(f"DROP TABLE IF EXISTS {TABLE} SYNC")
    cx.command(
        f"CREATE TABLE {TABLE} (id Int64, name String, amount Float64, "
        "__sde_write_epoch Int64 DEFAULT 0) ENGINE=MergeTree ORDER BY id"
    )
    per = ROWS // BLOCKS
    for block in range(BLOCKS):
        start = block * per
        cx.command(
            f"INSERT INTO {TABLE} (id, name, amount, __sde_write_epoch) SELECT number, "
            f"concat('row ', toString(number)), number / 7, 1 FROM numbers({start}, {per})",
            settings={"max_insert_block_size": per, "min_insert_block_size_rows": per},
        )


STATEMENTS = [
    f"ALTER TABLE {TABLE} ADD CONSTRAINT IF NOT EXISTS __sde_f_lo_2 CHECK __sde_write_epoch >= 2",
    f"ALTER TABLE {TABLE} ADD CONSTRAINT IF NOT EXISTS __sde_f_hi_2 CHECK __sde_write_epoch <= 2",
    f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS __sde_f_lo_2",
    f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS __sde_f_hi_2",
]
ONE = (
    f"ALTER TABLE {TABLE} ADD CONSTRAINT IF NOT EXISTS __sde_f_lo_2 CHECK __sde_write_epoch >= 2, "
    "ADD CONSTRAINT IF NOT EXISTS __sde_f_hi_2 CHECK __sde_write_epoch <= 2, "
    "DROP CONSTRAINT IF EXISTS __sde_f_lo_2, DROP CONSTRAINT IF EXISTS __sde_f_hi_2"
)


def timed(cx, statements: list[str]) -> list[dict[str, object]]:  # type: ignore[no-untyped-def]
    out = []
    for statement in statements:
        before = merges(cx)
        started = time.perf_counter()
        cx.command(statement)
        out.append({
            "statement": statement.split(TABLE, 1)[1].strip()[:60],
            "seconds": round(time.perf_counter() - started, 4),
            "merges_before": before,
        })
    return out


def wait_merging(cx, limit: float = 30.0) -> float:  # type: ignore[no-untyped-def]
    """Until a merge of the table is listed, so that the statements meet one."""
    started = time.perf_counter()
    while not merges(cx) and time.perf_counter() - started < limit:
        time.sleep(0.05)
    return time.perf_counter() - started


def wait_merged(cx, limit: float = 300.0) -> float:  # type: ignore[no-untyped-def]
    """Until no merge of the table has been listed for 3 s: right after an insert the merges may
    not have started yet, and an empty list then says nothing."""
    started = time.perf_counter()
    quiet = 0
    while time.perf_counter() - started < limit:
        quiet = 0 if merges(cx) else quiet + 1
        if quiet >= 6:
            break
        time.sleep(0.5)
    return time.perf_counter() - started


def main() -> int:
    out_path, rounds = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 2
    cx = client()
    version = cx.query("SELECT version()").result_rows[0][0]
    results: list[dict[str, object]] = []
    for round_ in range(rounds):
        for case in ("merging", "optimize", "merged", "stopped", "one-statement"):
            fill(cx)
            parts = active_parts(cx)
            waited = 0.0
            optimizer = None
            if case in ("merging", "one-statement"):
                waited = wait_merging(cx)
            if case == "optimize":
                # A merge of everything, started on another connection, as a long merge after a
                # backfill would be running.
                other = client()
                optimizer = threading.Thread(
                    target=lambda: other.command(f"OPTIMIZE TABLE {TABLE} FINAL"), daemon=True
                )
                optimizer.start()
                waited = wait_merging(cx)
            if case == "merged":
                waited = wait_merged(cx)
            if case == "stopped":
                cx.command(f"SYSTEM STOP MERGES {TABLE}")
                waited = wait_merged(cx, 60.0)  # stopping cancels; wait until none is listed
            steps = timed(cx, [ONE] if case == "one-statement" else STATEMENTS)
            if case == "stopped":
                cx.command(f"SYSTEM START MERGES {TABLE}")
            if optimizer is not None:
                optimizer.join(timeout=300)
            total = sum(float(s["seconds"]) for s in steps)
            results.append({
                "round": round_, "case": case, "parts_after_insert": parts,
                "waited_before": round(waited, 2), "total_seconds": round(total, 4), "steps": steps,
            })
            print(f"{round_} {case:14} parts={parts:3} waited={waited:6.2f}s "
                  f"total={total:7.3f}s  " + " ".join(f"{s['seconds']:.3f}" for s in steps),
                  flush=True)
    cx.command(f"DROP TABLE IF EXISTS {TABLE} SYNC")
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump({"clickhouse": version, "rows": ROWS, "blocks": BLOCKS, "results": results},
                  handle, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

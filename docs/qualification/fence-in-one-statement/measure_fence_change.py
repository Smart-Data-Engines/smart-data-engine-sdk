"""A write fence's epoch change and release on ClickHouse, timed, with the disk quiet and busy.

Each round freezes the table under a new barrier, advances its generation by one and releases the
barrier, through the library's own `WriteFence` on a table of `--rows` rows. `advance` and `release`
are timed apart: they are the steps of an advance's pause that change the table's constraints.
`busy` runs a process beside them that writes 8 MiB at a time and syncs it, as a backfill's merges
or another tenant of the disk would. The library is whichever this interpreter imports, and the
output names its file and the commit of the tree it is in.

    SDE_CLICKHOUSE_DSN=... <tree>/python/.venv/bin/python measure_fence_change.py \
        --rows 2000000 --rounds 10 --pressure /path/on/the/engine's/disk --out result.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import sde
from sde.engines.clickhouse import ClickHouseEngine

PROJECT = "1" * 32
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


def commit() -> str:
    tree = Path(sde.__file__).resolve().parents[3]
    done = subprocess.run(
        ["git", "-C", str(tree), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    return done.stdout.strip() or "unknown"


def rounds(engine: Any, table: str, count: int, first: int) -> list[dict[str, float]]:
    fence = engine.write_fence(table, project_id=PROJECT)
    out = []
    for epoch in range(first + 1, first + 1 + count):
        hold = uuid4().hex
        fence.freeze(hold)
        started = time.perf_counter()
        fence.advance(epoch)
        advanced = time.perf_counter()
        fence.release(hold)
        released = time.perf_counter()
        out.append({
            "advance_ms": round((advanced - started) * 1000, 1),
            "release_ms": round((released - advanced) * 1000, 1),
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=2_000_000)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--pressure", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    engine = ClickHouseEngine(os.environ["SDE_CLICKHOUSE_DSN"])
    engine.connect()
    table = "sde_fence_timing_" + uuid4().hex[:12]
    engine._cx.command(
        f"CREATE TABLE {table} (id Int64, name String) ENGINE=MergeTree ORDER BY id"
    )
    engine._cx.command(
        f"INSERT INTO {table} (id, name) SELECT number, toString(number) "
        f"FROM numbers({args.rows})"
    )
    # One part and no merge running, before any round: the first version of this script began
    # while ClickHouse still merged the parts just inserted, and their writes beside the busy
    # disk's put second-long spikes into whichever side's first rounds met them.
    engine._cx.command(f"OPTIMIZE TABLE {table} FINAL")
    while engine._cx.query(
        "SELECT count() FROM system.merges WHERE database=currentDatabase() AND table={t:String}",
        parameters={"t": table},
    ).result_rows[0][0]:
        time.sleep(0.2)
    result: dict[str, Any] = {
        "machine": platform.node(), "python": platform.python_version(),
        "library": Path(sde.__file__).resolve().relative_to(
            Path(sde.__file__).resolve().parents[3]
        ).as_posix(),
        "commit": commit(),
        "clickhouse": engine._cx.query("SELECT version()").result_rows[0][0],
        "rows": args.rows, "rounds": args.rounds, "load_before": list(os.getloadavg()),
    }
    try:
        engine.write_fence(table, project_id=PROJECT).prepare(1)
        result["quiet"] = rounds(engine, table, args.rounds, 1)
        writer = subprocess.Popen([sys.executable, "-I", "-c", WRITER, str(args.pressure), "900"])
        try:
            time.sleep(3)
            result["busy"] = rounds(engine, table, args.rounds, 1 + args.rounds)
        finally:
            writer.terminate()
            writer.wait()
            args.pressure.unlink(missing_ok=True)
    finally:
        engine._cx.command(f"DROP TABLE IF EXISTS {table} SYNC")
        engine.close()
    for condition in ("quiet", "busy"):
        for step in ("advance_ms", "release_ms"):
            values = [row[step] for row in result[condition]]
            print(f"{condition:5} {step:10} median {statistics.median(values):7.1f} "
                  f"range {min(values):7.1f}-{max(values):7.1f}", flush=True)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

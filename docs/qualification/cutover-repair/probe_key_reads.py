"""What a table read in key order costs ClickHouse by chunks, and by one statement.

A cutover's pass walks its frozen source in key order. ``key_range`` with a limit is one statement
a chunk, and under ``FINAL`` each of them merges the table's parts again from its starting key.
This times that statement at several starting keys of an N-row table, the same range bounded at
both ends, and then the whole table read both ways: by chunks, and by ``key_stream``.

    cd python
    .venv/bin/python ../docs/qualification/cutover-repair/probe_key_reads.py --rows 900000
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from typing import Any

from sde.engines.clickhouse import ClickHouseEngine
from sde.generation import EPOCH_COLUMN


def timed(function: Any, runs: int = 5) -> dict[str, float]:
    taken = []
    for _ in range(runs):
        started = time.perf_counter()
        function()
        taken.append((time.perf_counter() - started) * 1000)
    taken.sort()
    return {"min_ms": round(taken[0], 1), "median_ms": round(taken[len(taken) // 2], 1)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=900_000)
    parser.add_argument("--chunk-rows", type=int, default=1000)
    args = parser.parse_args()
    table = "probe_" + uuid.uuid4().hex[:12]
    report: dict[str, Any] = {"rows": args.rows, "chunk_rows": args.chunk_rows}
    with ClickHouseEngine(os.environ["SDE_CLICKHOUSE_DSN"]) as engine:
        engine._cx.command(
            f"CREATE TABLE `{table}` (id Int64, value Int32, `{EPOCH_COLUMN}` Int64 DEFAULT 0) "
            f"ENGINE = ReplacingMergeTree ORDER BY (id)"
        )
        try:
            for start in range(0, args.rows, 20000):
                engine.copy_in(
                    table,
                    [
                        {"id": n, "value": n % 1000, EPOCH_COLUMN: 1}
                        for n in range(start, min(args.rows, start + 20000))
                    ],
                )
            report["version"] = engine._cx.query("SELECT version()").result_rows[0][0]
            report["parts"] = engine._cx.query(
                "SELECT count() FROM system.parts WHERE database = currentDatabase() "
                f"AND table = '{table}' AND active"
            ).result_rows[0][0]
            chunk = args.chunk_rows
            report["one_chunk_after_a_key_with_a_limit"] = {
                str(start): timed(
                    lambda start=start: engine.key_range(table, ["id"], after=(start,), limit=chunk)
                )
                for start in (0, args.rows // 3, 2 * args.rows // 3, args.rows - 10 * chunk)
            }
            report["one_chunk_bounded_at_both_ends"] = {
                str(start): timed(
                    lambda start=start: engine.key_range(
                        table, ["id"], after=(start,), upto=(start + chunk,)
                    )
                )
                for start in (0, 2 * args.rows // 3)
            }
            started = time.perf_counter()
            read = sum(len(rows) for rows in engine.key_stream(table, ["id"], chunk_rows=chunk))
            report["whole_table_by_one_stream"] = {
                "rows": read, "seconds": round(time.perf_counter() - started, 2)
            }
            started = time.perf_counter()
            read, after = 0, None
            while True:
                rows = engine.key_range(table, ["id"], after=after, limit=chunk)
                if not rows:
                    break
                read += len(rows)
                after = (rows[-1]["id"],)
            report["whole_table_by_chunks"] = {
                "rows": read, "seconds": round(time.perf_counter() - started, 2)
            }
        finally:
            engine._cx.command(f"DROP TABLE IF EXISTS `{table}`")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Whether ClickHouse ends an answer before the table does, and says nothing.

A cutover's pass reads its frozen source in key order by one statement (``key_stream``). A login
whose settings cap a result and tell the server to break rather than throw
(``max_result_rows`` with ``result_overflow_mode = 'break'``) is answered the first part of it and
no error. This gives the adapter's own connection those settings, as a settings profile gives them
to a login, and reads an N-row table three ways: by ``key_stream``, by ``key_range`` a chunk at a
time, and by ``count``.

    cd python
    .venv/bin/python ../docs/qualification/cutover-repair/probe_cut_answer.py --rows 200000
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from typing import Any

from sde.engines.clickhouse import ClickHouseEngine
from sde.generation import EPOCH_COLUMN


def read_three_ways(engine: ClickHouseEngine, table: str, chunk: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        out["by_one_stream"] = sum(
            len(rows) for rows in engine.key_stream(table, ["id"], chunk_rows=chunk)
        )
    except Exception as exc:  # The point is whether it raises: say which, and go on.
        out["by_one_stream"] = f"raised {type(exc).__name__}"
    read, statements, after = 0, 0, None
    while True:
        rows = engine.key_range(table, ["id"], after=after, limit=chunk)
        statements += 1
        if not rows:
            break
        read += len(rows)
        after = (rows[-1]["id"],)
    out["by_chunks"] = {"rows": read, "statements": statements}
    out["count"] = engine.count(table)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=200_000)
    parser.add_argument("--chunk-rows", type=int, default=1000)
    parser.add_argument("--caps", type=int, nargs="+", default=[50_000, 100])
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
            report["no_cap"] = read_three_ways(engine, table, args.chunk_rows)
            engine._cx.set_client_setting("result_overflow_mode", "break")
            for cap in args.caps:
                engine._cx.set_client_setting("max_result_rows", cap)
                report[f"max_result_rows_{cap}_break"] = read_three_ways(
                    engine, table, args.chunk_rows
                )
        finally:
            engine._cx.set_client_setting("max_result_rows", 0)
            engine._cx.command(f"DROP TABLE IF EXISTS `{table}`")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

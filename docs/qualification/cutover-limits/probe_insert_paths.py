"""Why a move into PostgreSQL paused longest: six ways to write one chunk, timed on a fenced table.

``measure_phases.py`` put most of a move's pause into PostgreSQL in the repair, and the profile put
it in the INSERT that ``copy_in`` sent. This writes the same 50 000 narrow rows, in chunks of
1000, into a table shaped like a repaired copy - a primary key and the write fence's CHECK
constraints - six ways, with psycopg directly and nothing of the SDK's, so it separates the cost of
``ON CONFLICT DO NOTHING`` from the cost of the statement that carries the rows:

- ``values_conflict``: ``INSERT ... VALUES (...), ... ON CONFLICT DO NOTHING``, ``copy_in`` before;
- ``values_plain``: the same statement without the conflict clause;
- ``unnest_conflict`` / ``unnest_plain``: the rows as one array per column, with and without it;
- ``copy_then_conflict``: ``COPY`` into a temporary table, then ``INSERT ... SELECT`` with the
  conflict clause;
- ``copy``: ``COPY`` into the table, what ``copy_in`` sends first since this record.

Each conflict form is run once more over its first chunk, which must add nothing. Two passes:

    SDE_POSTGRES_DSN=... python probe_insert_paths.py --out probe_insert_paths.out.json
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg

ROWS, CHUNK = 50000, 1000
COLUMNS = ["__sde_write_epoch", "id", "value"]
TARGET = "probe_insert_paths"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    cx = psycopg.connect(os.environ["SDE_POSTGRES_DSN"], autocommit=True)
    cx.execute(f"DROP TABLE IF EXISTS {TARGET}")
    cx.execute(
        f"CREATE TABLE {TARGET} (id bigint PRIMARY KEY, value bigint NOT NULL, "
        "__sde_write_epoch bigint NOT NULL DEFAULT 0, "
        "CONSTRAINT __sde_f_owner_probe CHECK (true), "
        "CONSTRAINT __sde_f_min_7 CHECK (__sde_write_epoch >= 7), "
        "CONSTRAINT __sde_f_max_7 CHECK (__sde_write_epoch <= 7))"
    )
    chunks = [
        [[7, i, i % 100000] for i in range(start, start + CHUNK)]
        for start in range(0, ROWS, CHUNK)
    ]
    names = ", ".join(COLUMNS)
    arrays = ", ".join("%s::bigint[]" for _ in COLUMNS)

    def values(rows: list[list[int]], clause: str) -> None:
        marks = ", ".join("(%s, %s, %s)" for _ in rows)
        cx.execute(
            f"INSERT INTO {TARGET} ({names}) VALUES {marks}{clause}",
            [value for row in rows for value in row],
        )

    def unnest(rows: list[list[int]], clause: str) -> None:
        cx.execute(
            f"INSERT INTO {TARGET} ({names}) SELECT * FROM unnest({arrays}){clause}",
            [list(column) for column in zip(*rows, strict=True)],
        )

    def copy_then_conflict(rows: list[list[int]]) -> None:
        with cx.transaction(), cx.cursor() as cur:
            cur.execute(
                f"CREATE TEMP TABLE IF NOT EXISTS probe_stage (LIKE {TARGET}) "
                "ON COMMIT DELETE ROWS"
            )
            with cur.copy(f"COPY probe_stage ({names}) FROM STDIN") as copy:
                for row in rows:
                    copy.write_row(row)
            cur.execute(
                f"INSERT INTO {TARGET} ({names}) SELECT {names} FROM probe_stage "
                "ON CONFLICT DO NOTHING"
            )

    def plain_copy(rows: list[list[int]]) -> None:
        with cx.cursor() as cur, cur.copy(f"COPY {TARGET} ({names}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row(row)

    conflict = " ON CONFLICT DO NOTHING"
    paths: list[tuple[str, Callable[[list[list[int]]], None], bool]] = [
        ("values_conflict", lambda rows: values(rows, conflict), True),
        ("values_plain", lambda rows: values(rows, ""), False),
        ("unnest_conflict", lambda rows: unnest(rows, conflict), True),
        ("unnest_plain", lambda rows: unnest(rows, ""), False),
        ("copy_then_conflict", copy_then_conflict, True),
        ("copy", plain_copy, False),
    ]
    results: list[dict[str, Any]] = []
    for run in (1, 2):
        for name, write, idempotent in paths:
            cx.execute(f"TRUNCATE {TARGET}")
            started = time.perf_counter()
            for rows in chunks:
                write(rows)
            elapsed = time.perf_counter() - started
            count = cx.execute(f"SELECT count(*) FROM {TARGET}").fetchone()
            if idempotent:
                write(chunks[0])
                assert cx.execute(f"SELECT count(*) FROM {TARGET}").fetchone() == count
            result = {"path": name, "run": run, "rows": ROWS, "chunk_rows": CHUNK,
                      "ms": round(elapsed * 1000), "us_per_row": round(elapsed * 1e6 / ROWS, 1),
                      "count": count[0] if count else None, "load_before": os.getloadavg()[0]}
            results.append(result)
            print(json.dumps(result), flush=True)
    server = cx.execute("SHOW server_version").fetchone()
    cx.execute(f"DROP TABLE {TARGET}")
    report = {"server_version": server[0] if server else None, "psycopg": psycopg.__version__,
              "psycopg_impl": psycopg.pq.__impl__, "results": results}
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

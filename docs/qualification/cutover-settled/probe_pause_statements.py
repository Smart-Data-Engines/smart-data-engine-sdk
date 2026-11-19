"""What a settled cutover's pause spends, statement by statement, with and without an index on the
generation in PostgreSQL.

``measure_settled.py`` times the pause by the operator's steps, and the pause after a settle still
grows with the group. This runs the same move once a variant, and times every statement the
process sends while the cutover runs - every ``execute`` of a PostgreSQL cursor and every query,
command and insert of a ClickHouse client, a call made inside another counted once - by its shape:
the statement with its identifiers and values taken out. A PostgreSQL ``COPY`` is not among them:
its rows are sent after its call returns, and the settled pause copies only the rows a fan-out
missed. The variants:

- ``plain``: the move of ``measure_settled.py``, way ``settled-written``;
- ``indexed``: the same, with a partial index on the PostgreSQL table of the group - the source's or
  the copy's - created after the settle: ``(key) WHERE __sde_write_epoch >= G``. Every read the
  pause makes by generation asks for G or the cutover's generation above it, so the index answers
  all of them. Creating it is timed; keeping it up to date is not.

Run from ``python/tests`` with both engine DSNs set, as the measurement:

    ../.venv/bin/python ../../docs/qualification/cutover-settled/probe_pause_statements.py \
        --sources postgres clickhouse --rows 2000000 --out statements.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from measure_settled import (
    OTHER,
    PROJECT,
    advance_of,
    cutover_from,
    initial,
    phases_of,
    seed,
    written_since,
)

import sde
from sde.generation import EPOCH_COLUMN

VARIANTS = ("plain", "indexed")


def shape(statement: Any) -> str:
    """A statement without its identifiers and values: what it asks for, not of which table."""
    text = statement if isinstance(statement, str) else str(statement)
    text = re.sub(r'"[^"]*"|`[^`]*`', "<name>", text)
    text = re.sub(r"'[^']*'", "<value>", text)
    text = re.sub(r"\b\d+\b", "<n>", text)
    text = re.sub(r"%\(\w+\)s|%s|\{\w+:\w+\}", "<p>", text)
    return " ".join(text.split())[:140]


@contextmanager
def statements() -> Iterator[list[tuple[str, str, float]]]:
    """Every statement this process sends meanwhile, as (engine, shape, seconds)."""
    import clickhouse_connect.driver.client as ch
    import psycopg

    sent: list[tuple[str, str, float]] = []
    kept: list[tuple[Any, str, Any]] = []
    depth = [0]

    def timed(owner: Any, name: str, engine: str, statement: Callable[..., Any]) -> None:
        original = getattr(owner, name)
        kept.append((owner, name, original))

        def call(self: Any, *args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            depth[0] += 1
            try:
                return original(self, *args, **kwargs)
            finally:
                depth[0] -= 1
                if depth[0] == 0:
                    elapsed = time.monotonic() - started
                    sent.append((engine, shape(statement(*args, **kwargs)), elapsed))

        setattr(owner, name, call)

    timed(psycopg.Cursor, "execute", "postgres", lambda query, *a, **k: query)
    timed(ch.Client, "query", "clickhouse", lambda query=None, *a, **k: query)
    timed(ch.Client, "command", "clickhouse", lambda cmd, *a, **k: cmd)
    timed(ch.Client, "insert", "clickhouse", lambda table=None, *a, **k: f"INSERT INTO {table}")
    try:
        yield sent
    finally:
        for owner, name, original in reversed(kept):
            setattr(owner, name, original)


def by_shape(sent: list[tuple[str, str, float]]) -> list[dict[str, Any]]:
    totals: dict[tuple[str, str], list[float]] = defaultdict(list)
    for engine, text, seconds in sent:
        totals[(engine, text)].append(seconds)
    rows = [
        {
            "engine": engine,
            "statement": text,
            "count": len(times),
            "total_ms": round(sum(times) * 1000, 1),
            "max_ms": round(max(times) * 1000, 1),
        }
        for (engine, text), times in totals.items()
    ]
    return sorted(rows, key=lambda row: -row["total_ms"])


def probe(source: str, rows: int, variant: str, since: int) -> dict[str, Any]:
    target = OTHER[source]
    with (
        TemporaryDirectory() as tmp,
        initial(source, Path(tmp), target=target) as (
            operator,
            stage,
            _old,
            roles,
            model,
            public,
            signed,
            _m,
            _s,
            _t,
        ),
    ):
        seed(roles[source].operator, "initial_events", 2, rows - 1)
        operator.stage(stage)
        (progress,) = operator.backfill()
        assert progress.complete, progress.as_record()
        before = stage.as_record()["prepared"]
        plan = advance_of(before, signed, model, public)
        assert operator.advance(plan).as_record()["outcome"] == "advanced"
        before = plan.as_record()["after"]
        (settled,) = operator.settle()
        assert settled["settled"], settled
        epoch = before["groups"]["Event"]["write_epoch"]
        copy_table = before["groups"]["Event"]["derived"][0]["layout"]["tables"]["Event"]
        written = max(10, rows * since // 1000)
        written_since(roles[source].operator, "initial_events", rows + 1, written, epoch)
        written_since(roles[target].operator, copy_table, rows + 1, written - written // 10, epoch)
        index_ms = None
        if variant == "indexed":
            table = "initial_events" if source == "postgres" else copy_table
            started = time.monotonic()
            roles["postgres"].operator._cx.execute(
                f'CREATE INDEX "sde_probe_since" ON "{table}" ("id") '
                f'WHERE "{EPOCH_COLUMN}" >= {int(epoch)}'
            )
            roles["postgres"].operator._cx.execute(f'ANALYZE "{table}"')
            index_ms = round((time.monotonic() - started) * 1000)
        marks: list[tuple[str, float]] = []
        operator._after_step = lambda step: marks.append((step, time.monotonic()))
        cutover = cutover_from(before, signed, model, public)
        with statements() as sent:
            receipt = operator.execute(cutover).as_record()
        assert receipt["outcome"] == "success", receipt
        return {
            "source": source,
            "target": target,
            "rows": rows,
            "variant": variant,
            "rows_written_since_the_settle": written,
            "index_ms": index_ms,
            "pause_ms": receipt["elapsed_ms"],
            "phases": phases_of(marks),
            "statements_ms": round(sum(seconds for _, _, seconds in sent) * 1000),
            "statements": by_shape(sent),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, nargs="+", required=True)
    parser.add_argument("--sources", nargs="+", default=["postgres", "clickhouse"])
    parser.add_argument("--variants", nargs="+", default=list(VARIANTS), choices=VARIANTS)
    parser.add_argument("--since", type=int, default=1, help="rows in a thousand, written since")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    results = []
    for source in args.sources:
        for rows in args.rows:
            for run in range(1, args.repeat + 1):
                for variant in args.variants:
                    load = os.getloadavg()
                    result = probe(source, rows, variant, args.since)
                    result["run"] = run
                    result["load_before"] = [round(value, 2) for value in load]
                    results.append(result)
                    print(json.dumps({k: v for k, v in result.items() if k != "statements"}))
                    args.out.write_text(json.dumps({"partial": results}, indent=2) + "\n")
    report = {
        "machine": {
            "node": platform.node(),
            "processor": platform.processor(),
            "cpus": os.cpu_count(),
            "python": platform.python_version(),
        },
        "sde_version": sde.__version__,
        "commit": os.environ.get("SDE_MEASURED_COMMIT"),
        "project": PROJECT,
        "results": results,
    }
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

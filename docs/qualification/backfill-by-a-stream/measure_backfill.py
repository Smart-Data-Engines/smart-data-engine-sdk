"""How long the backfill before a cutover takes, by the size of the group.

``sde-operator backfill`` fills the copy a staging created, while the application keeps writing.
This seeds N rows into the source, stages the copy with ``LocalCutover.stage`` and times
``LocalCutover.backfill`` to its end, on the SDK's own code paths. Nothing is cut over.

It imports the live-test fixtures, so it runs from ``python/tests`` with both engine DSNs set. To
measure another tree of the library with the same script, put that tree's ``python/src`` first on
``PYTHONPATH``; each result names the commit of the ``sde`` it ran:

    cd python/tests
    ../.venv/bin/python ../../docs/qualification/backfill-by-a-stream/measure_backfill.py \
        --sources clickhouse --rows 200000 900000 --label stream --out backfill.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

sys.path.insert(0, str(Path.cwd()))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cutover-limits"))

from measure_moves import OTHER, seed
from test_staging_operator_live import initial

import sde


def fill(source: str, rows: int, chunk_rows: int) -> dict[str, Any]:
    target = OTHER[source]
    with TemporaryDirectory() as tmp, initial(source, Path(tmp), target=target) as (
        operator, stage, _old, roles, _model, _public, _signed, _m, _s, _t,
    ):
        seed(roles[source].operator, "initial_events", 2, rows - 1)
        operator.stage(stage)
        operator.chunk_rows = chunk_rows
        copy_table = stage.prepared.groups["Event"].derived[0].layout.tables["Event"]
        load = os.getloadavg()[0]
        started = time.monotonic()
        (progress,) = operator.backfill()
        seconds = time.monotonic() - started
        (entity,) = progress.entities
        return {
            "source": source,
            "target": target,
            "rows": rows,
            "chunk_rows": chunk_rows,
            "backfill_s": round(seconds, 2),
            "us_a_row": round(seconds * 1e6 / rows, 1),
            "complete": progress.complete,
            "rows_copied": entity.rows_copied,
            "chunks": entity.chunks,
            "rows_in_the_copy": roles[target].operator.count(copy_table),
            "load_before": round(load, 2),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, nargs="+", required=True)
    parser.add_argument("--sources", nargs="+", default=["clickhouse"])
    parser.add_argument("--chunk-rows", type=int, default=1000)
    parser.add_argument("--label", required=True, help="which tree this is, in words")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    tree = Path(sde.__file__).resolve().parents[3]
    commit = subprocess.run(
        ["git", "-C", str(tree), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "-C", str(tree), "status", "--porcelain", "--", "python/src"],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
    )
    report: dict[str, Any] = {
        "label": args.label,
        "commit": commit,
        "python_src_modified": dirty,
        "runs": [],
        "machine": {"node": platform.node(), "processor": platform.processor(),
                    "cpus": os.cpu_count(), "python": platform.python_version()},
    }
    for source in args.sources:
        for rows in args.rows:
            result = fill(source, rows, args.chunk_rows)
            report["runs"].append(result)
            print(json.dumps(result), flush=True)
            args.out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

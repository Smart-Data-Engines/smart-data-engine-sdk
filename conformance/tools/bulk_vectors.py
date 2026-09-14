"""Emit hand-specified application-batch witnesses. Existing vectors are never regenerated.

Run with --write only when adding or intentionally changing these cases. Expected call counts,
rows, failures and transaction outcomes below do not run Session or a native adapter.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))
import sde  # noqa: E402
from sde.testing.loader import model_from_neutral  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if not args.write:
        parser.error("--write is required; these are versioned witnesses")
    base = next((ROOT / "conformance/vectors/migration").glob("013-*"))
    model_doc = json.loads((base / "model.json").read_text())
    map_doc = json.loads((base / "map.json").read_text())
    model = model_from_neutral(model_doc)
    shape = next(
        s for s in sde.enumerate_shapes(model) if s.entity == "Reading" and s.kind == "bulk_write"
    )
    rows = [{"id": 1, "celsius": 20}, {"celsius": 21, "id": 2}]
    source, target = "pg-main", "pg-copy"

    def call(engine, n=2):
        return {"engine": engine, "call": "insert_many", "table": "reading", "rows": n}

    tx = {"engine": source, "call": "transaction"}
    write = {"op": "save_many", "rows": rows}

    def case(
        number,
        slug,
        operations,
        calls,
        src_rows,
        dst_rows,
        *,
        count=0,
        row_count=0,
        failed=0,
        copies=0,
        copy_failed=0,
        errors=None,
        spec_change=None,
    ):
        spec = {
            source: {"dialect": "postgres", "tables": {}},
            target: {"dialect": "postgres", "tables": {}},
        }
        if spec_change:
            name, key, value = spec_change
            spec[name][key] = value
        tables = {
            source: {} if src_rows is None else {"reading": src_rows},
            target: {} if dst_rows is None else {"reading": dst_rows},
        }
        metrics = {
            "shapes": []
            if not count
            else [
                {
                    "shape_id": shape.id,
                    "entity": "Reading",
                    "group": "Reading",
                    "kind": "bulk_write",
                    "calls": count,
                    "rows": row_count,
                    "errors": failed,
                }
            ],
            "copies": []
            if not copies
            else [
                {
                    "group": "Reading",
                    "materialization": "Reading@pg2",
                    "writes": copies,
                    "failures": copy_failed,
                }
            ],
        }
        body = {
            "operations": operations,
            "calls": calls,
            "tables": tables,
            "errors": errors or [],
            "metrics": metrics,
            "metrics_hex": sde.canonical_bytes(metrics).hex(),
        }
        folder = ROOT / "conformance/vectors/migration" / f"{number:03d}-bulk-{slug}"
        folder.mkdir(exist_ok=True)
        for name, value in [
            ("model.json", model_doc),
            ("map.json", map_doc),
            ("engines.json", spec),
            ("bulk.json", body),
        ]:
            (folder / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        (folder / "note.txt").write_text(
            f"Application bulk witness: {slug}. One native batch, "
            "with explicit source/copy calls and metric bytes.\n"
        )

    case(
        122,
        "source-before-copy",
        [write],
        [call(source), call(target)],
        rows,
        rows,
        count=1,
        row_count=2,
        copies=1,
    )
    case(
        123,
        "nested-outer-commit",
        [{"op": "transaction", "body": [{"op": "transaction", "body": [write]}, write]}],
        [tx, tx, call(source), call(source), call(target), call(target)],
        rows + rows,
        rows + rows,
        count=2,
        row_count=4,
        copies=2,
    )
    case(
        124,
        "outer-rollback",
        [{"op": "transaction", "body": [{"op": "transaction", "body": [write]}], "rollback": True}],
        [tx, tx, call(source)],
        None,
        None,
        count=1,
        row_count=2,
    )
    case(
        125,
        "inner-rollback",
        [
            {
                "op": "transaction",
                "body": [{"op": "transaction", "body": [write], "rollback": True}, write],
            }
        ],
        [tx, tx, call(source), call(source), call(target)],
        rows,
        rows,
        count=2,
        row_count=4,
        copies=1,
    )
    case(
        126,
        "failed-source",
        [{**write, "error": "EngineError"}],
        [call(source)],
        None,
        None,
        count=1,
        row_count=2,
        failed=1,
        errors=["EngineError"],
        spec_change=(source, "fail_inserts", {"reading": 1}),
    )
    case(
        127,
        "failed-copy",
        [write],
        [call(source), call(target)],
        rows,
        None,
        count=1,
        row_count=2,
        copies=1,
        copy_failed=1,
        spec_change=(target, "fail_inserts", {"reading": 1}),
    )
    for n, side in [(128, source), (129, target)]:
        case(
            n,
            "capability-" + side,
            [{**write, "error": "BulkWriteRefused"}],
            [],
            None,
            None,
            errors=["BulkWriteRefused"],
            spec_change=(side, "bulk_writable", False),
        )
    case(130, "empty", [{"op": "save_many", "rows": []}], [], None, None)
    bad = copy.deepcopy(write)
    bad["rows"][1].pop("celsius")
    bad["error"] = "BulkWriteRefused"
    case(131, "unequal-fields", [bad], [], None, None, errors=["BulkWriteRefused"])
    case(
        132,
        "row-bound",
        [{"op": "save_many", "rows": [rows[0]], "repeat": 1001, "error": "BulkWriteRefused"}],
        [],
        None,
        None,
        errors=["BulkWriteRefused"],
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Generate the write-stage error cases - a row the model does not allow - without SDK imports.

A model says which fields a row must carry: every field declared without ``nullable``, and every
key field. Until 2 October 2026 neither library checked a row against that before an engine saw it.
PostgreSQL then stored NULL in a required field, and ClickHouse stored a value nobody wrote: ``0``
for an integer the row left out, ``""`` for a string (measured on PostgreSQL 15 and ClickHouse 24.8,
SDK ``b30c9fd``). The same model meant two things, and moving a group changed which.

Each case opens a session on a valid map, makes one accepted write - the control that shows the
engine records what it receives - and then the refused one. ``calls.json`` holds the accepted write
and nothing of the refused one: a refusal is made before any engine is called.

Every expectation is written here rather than computed by a library.

    python conformance/tools/write_vectors.py --i-am-changing-the-contract
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ERRORS = ROOT / "conformance/vectors/errors"

MODEL: dict[str, Any] = {
    "entities": [
        {
            "name": "Thing",
            "fields": [
                {"name": "amount", "type": "int32"},
                {"name": "id", "type": "string"},
                {"name": "label", "type": "string"},
                {"name": "note", "type": "string", "nullable": True},
            ],
            "key": ["id"],
        }
    ]
}
# The contract's hash of MODEL (section 2). Not computed here, because that would take a library or
# a second implementation of the IR; it needs no trust either, since both loaders refuse a map whose
# model_version is not their own hash of the model, so a wrong value fails every case below.
MODEL_VERSION = "f24ceedc0d7efe83"

MAP: dict[str, Any] = {
    "contract": 3,
    "model_version": MODEL_VERSION,
    "map_version": 1,
    "groups": {
        "Thing": {
            "source": {
                "id": "Thing@pg",
                "engine": "pg-main",
                "layout": {
                    "tables": {"Thing": "thing"},
                    "columns": {
                        "Thing": {
                            "amount": "integer",
                            "id": "text",
                            "label": "text",
                            "note": "text",
                        }
                    },
                },
            }
        }
    },
}

ENGINES = {"pg-main": {"dialect": "postgres"}}

FULL = {"amount": 1, "id": "t-1", "label": "first", "note": None}
"""The accepted write: every field, and ``null`` where the model allows it."""


def dump(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def save(values: dict[str, Any]) -> dict[str, Any]:
    return {"operation": "save", "entity": "Thing", "values": values}


def save_many(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"operation": "save_many", "entity": "Thing", "rows": rows}


ACCEPTED_ONE = [{"engine": "pg-main", "call": "insert", "table": "thing"}]

CASES: list[tuple[str, str, dict[str, Any], dict[str, Any], str, list[dict[str, Any]], str]] = [
    (
        "078-a-save-that-leaves-out-a-required-field",
        "ModelPlanningError",
        save(FULL),
        save({"id": "t-2", "label": "second", "note": None}),
        r"Thing\.amount is required and this row leaves it out",
        ACCEPTED_ONE,
        "The case that wrote a value nobody chose. ClickHouse filled the missing integer with 0 "
        "and returned it on every read; PostgreSQL stored NULL in a column the model says never "
        "holds one. Nothing raised in either, so the refusal has to come from the library, before "
        "the engine, and the same in every engine.",
    ),
    (
        "079-a-save-that-gives-a-required-field-null",
        "ModelPlanningError",
        save(FULL),
        save({"amount": None, "id": "t-2", "label": "second", "note": None}),
        r"Thing\.amount is required and this row gives it null",
        ACCEPTED_ONE,
        "PostgreSQL stored this null; ClickHouse's driver refused it with a message about a column "
        "type. One model, two outcomes, and neither was the library's decision. `note` is null in "
        "the same row and that is allowed: only the required field is refused.",
    ),
    (
        "080-a-save-that-gives-its-key-null",
        "ModelPlanningError",
        save(FULL),
        save({"amount": 2, "id": None, "label": "second", "note": None}),
        r"Thing\.id is required and this row gives it null",
        ACCEPTED_ONE,
        "A key field is required whether or not it is declared nullable: a row without its key "
        "cannot be read back by it.",
    ),
    (
        "081-a-save-with-a-field-the-entity-does-not-declare",
        "ModelPlanningError",
        save(FULL),
        save({"amount": 2, "colour": "red", "id": "t-2", "label": "second", "note": None}),
        r"Thing declares no field colour",
        ACCEPTED_ONE,
        "Refused by name before an engine is called. It used to reach the engine, which refused it "
        "in its own words - or, with hashed identifiers, was refused here in one language and sent "
        "on in the other.",
    ),
    (
        "082-a-batch-row-that-gives-a-required-field-null",
        "BulkWriteRefused",
        save_many([FULL, {"amount": 2, "id": "t-2", "label": "second", "note": None}]),
        save_many(
            [
                {"amount": 3, "id": "t-3", "label": "third", "note": None},
                {"amount": 4, "id": "t-4", "label": None, "note": "kept"},
            ]
        ),
        r"row 1: Thing\.label is required and this row gives it null",
        [{"engine": "pg-main", "call": "insert_many", "table": "thing", "rows": 2}],
        "A batch already refused a row that left a required field out; it let a null through. The "
        "whole batch is refused, row 0 included, because a batch is checked before anything is "
        "sent. The row is named by its position and the field by name - never a value.",
    ),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-am-changing-the-contract", action="store_true", required=True)
    parser.parse_args()
    for name, error, accepted, refused, match, calls, why in CASES:
        directory = ERRORS / name
        directory.mkdir(exist_ok=True)
        expected = {
            "error": error,
            "stage": "write",
            "match": match,
            "write": {"accepted": accepted, "refused": refused},
            "why": why,
        }
        for filename, value in (
            ("model.json", MODEL),
            ("map.json", MAP),
            ("engines.json", ENGINES),
            ("calls.json", calls),
            ("expected.json", expected),
        ):
            dump(directory / filename, value)
    print(f"Wrote {len(CASES)} write-stage refusals without importing an SDK")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Generate the case that pins the form a value reaches an engine in, without SDK imports.

Format contract section 8b, point 4: a row's value is admitted in the forms a filter of its type
takes, and it is passed on in the form a filter gets, so every adapter receives one representation
per type. Until 7 October 2026 a value went to the adapter as the application gave it, and the same
text meant different things per engine: a timestamp with an offset was converted by PostgreSQL and
refused by ClickHouse, and in a timezone-free column PostgreSQL dropped the offset.

The engine is the library's in-memory one, and ``tables.json`` is what it holds, written here by
hand rather than computed by a library: an instant as ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` in UTC, a
decimal in plain notation at its column's scale, a UUID in lower case - the forms the ``query/``
vectors give the same values. An integer given to a decimal field stays the integer it was.

    python conformance/tools/value_vectors.py --i-am-changing-the-contract
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
NAME = "200-a-value-reaches-the-engine-in-the-form-a-filter-gets"

MODEL: dict[str, Any] = {
    "entities": [
        {
            "name": "Value",
            "fields": [
                {"name": "amount", "type": "decimal(12,2)", "nullable": True},
                {"name": "at", "type": "timestamptz", "nullable": True},
                {"name": "day", "type": "date", "nullable": True},
                {"name": "id", "type": "int64"},
                {"name": "token", "type": "uuid", "nullable": True},
                {"name": "wall", "type": "timestamp", "nullable": True},
            ],
            "key": ["id"],
        }
    ]
}
# The contract's hash of MODEL (section 2), computed once by a library and written here: both
# loaders refuse a map whose model_version is not their own hash of the model, so a wrong value
# fails the case rather than passing it.
MODEL_VERSION = "64ed95b2081f50de"

MAP: dict[str, Any] = {
    "contract": 3,
    "model_version": MODEL_VERSION,
    "map_version": 1,
    "groups": {
        "Value": {
            "source": {
                "id": "Value@pg",
                "engine": "pg-main",
                "layout": {
                    "tables": {"Value": "value"},
                    "columns": {
                        "Value": {
                            "amount": "numeric(12,2)",
                            "at": "timestamptz",
                            "day": "date",
                            "id": "bigint",
                            "token": "uuid",
                            "wall": "timestamp",
                        }
                    },
                },
            }
        }
    },
}

# Each save, and the row the engine holds after it.
SAVES: list[tuple[dict[str, Any], dict[str, Any]]] = [
    ({"id": 1, "amount": "1.230"}, {"id": 1, "amount": "1.23"}),
    ({"id": 2, "amount": "1E+3"}, {"id": 2, "amount": "1000.00"}),
    ({"id": 3, "amount": "-0.00"}, {"id": 3, "amount": "0.00"}),
    ({"id": 4, "amount": " .5 "}, {"id": 4, "amount": "0.50"}),
    ({"id": 5, "amount": 7}, {"id": 5, "amount": 7}),
    (
        {"id": 6, "at": "2026-11-09T11:30:15.123456+02:00"},
        {"id": 6, "at": "2026-11-09T09:30:15.123456Z"},
    ),
    (
        {"id": 7, "wall": "2026-11-09T11:30:15.123456+02:00"},
        {"id": 7, "wall": "2026-11-09T09:30:15.123456Z"},
    ),
    ({"id": 8, "wall": "2026-11-09 09:30:15"}, {"id": 8, "wall": "2026-11-09T09:30:15.000000Z"}),
    (
        {"id": 9, "token": "0E984725-C51C-4BF4-9960-E1C80E27ABA0"},
        {"id": 9, "token": "0e984725-c51c-4bf4-9960-e1c80e27aba0"},
    ),
    ({"id": 10, "day": "2026-10-07"}, {"id": 10, "day": "2026-10-07"}),
]

WHY = (
    "What reaches the engine, rather than what the application wrote. A decimal is passed at its "
    "column's scale - 1.230, 1E+3, -0.00 and .5 with spaces around it are 1.23, 1000.00, 0.00 and "
    "0.50 - and an integer given to a decimal field stays that integer. A timestamp with an offset "
    "is its instant in UTC, and in a timezone-free field its UTC wall time: PostgreSQL used to "
    "drop the offset there and ClickHouse to refuse it, so the same text meant two things. A UUID "
    "is lower case and a date is its text. The rows are written by hand, not computed by a library."
)


def dump(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-am-changing-the-contract", action="store_true", required=True)
    parser.parse_args()
    directory = ROOT / "conformance/vectors/migration" / NAME
    directory.mkdir(exist_ok=True)
    held = sorted((row for _, row in SAVES), key=lambda row: json.dumps(row, sort_keys=True))
    files: dict[str, Any] = {
        "model.json": MODEL,
        "map.json": MAP,
        "engines.json": {"pg-main": {"dialect": "postgres"}},
        "operations.json": [
            {"op": "save", "entity": "Value", "values": given} for given, _ in SAVES
        ],
        "tables.json": {"pg-main": {"value": held}},
        "copies.json": [],
        "calls.json": [{"engine": "pg-main", "call": "insert", "table": "value"} for _ in SAVES],
        "why.json": {"why": WHY},
    }
    for filename, value in files.items():
        dump(directory / filename, value)
    print(f"Wrote migration/{NAME} without importing an SDK")


if __name__ == "__main__":
    main()

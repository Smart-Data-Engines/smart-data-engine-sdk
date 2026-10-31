#!/usr/bin/env python3
"""Generate the session-stage refusals of a temporal column that does not keep its field's zone.

A scan reads a value by its field's type and a read by key by its column's. So a hand-written layout
that gives a `timestamptz` field a column without a zone - or a `timestamp` field one with a zone -
read one row as two types: aware in a scan and naive by key, or the other way round (finding 27
of the C++ port, measured on ClickHouse 24.8 on 7 October 2026). Since 31 October 2026 a session
refuses such a layout when it opens, because only an engine says which dialect's spelling a layout
is in (format contract 7a, the order in 8a).

Each case's map is valid, so it loads; the session over it is refused with `MapError` before any
engine is called, and `calls.json` is empty. Every expectation is written here rather than computed
by a library.

    python conformance/tools/zone_vectors.py --i-am-changing-the-contract
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
            "name": "Reading",
            "fields": [
                {"name": "at", "type": "timestamptz", "nullable": True},
                {"name": "id", "type": "int64"},
                {"name": "wall", "type": "timestamp", "nullable": True},
            ],
            "key": ["id"],
        }
    ],
    "relations": [],
    "atomic": [],
}
# The contract's hash of MODEL (section 2), not computed here: every loader refuses a map whose
# model_version is not its own hash of the model, so a wrong value fails every case below.
MODEL_VERSION = "33f717b446fc783c"

ENGINES = {
    "clickhouse": ("ch-main", "Reading@ch"),
    "postgres": ("pg-main", "Reading@pg"),
    "elasticsearch": ("es-main", "Reading@es"),
}
KEYS = {"clickhouse": "Int64", "postgres": "bigint", "elasticsearch": "long"}


def placement(dialect: str, at: str, wall: str) -> dict[str, Any]:
    engine, material = ENGINES[dialect]
    columns = {"at": at, "id": KEYS[dialect], "wall": wall}
    return {
        "contract": 3,
        "model_version": MODEL_VERSION,
        "map_version": 1,
        "groups": {
            "Reading": {
                "source": {
                    "id": material,
                    "engine": engine,
                    "layout": {"tables": {"Reading": "reading"}, "columns": {"Reading": columns}},
                }
            }
        },
    }


def prefix(dialect: str, field: str, kind: str, carry: bool) -> str:
    material = ENGINES[dialect][1]
    must = "carry" if carry else "not carry"
    return (
        f"group 'Reading': '{material}': Reading.{field} is {kind}, so its {dialect} column must "
        f"{must} a zone"
    )


CASES: list[tuple[str, str, str, str, str, str]] = [
    (
        "125-a-timestamptz-in-a-clickhouse-column-without-a-zone",
        "clickhouse",
        "DateTime64(6)",
        "DateTime64(6)",
        prefix("clickhouse", "at", "timestamptz", True),
        "The finding itself: an instant stored in a column without a zone. A scan gave it back "
        "aware and a read by key naive, so a client comparing the two got a TypeError, on the day "
        "the group moved and not before.",
    ),
    (
        "126-a-timestamp-in-a-clickhouse-column-with-a-zone",
        "clickhouse",
        "DateTime64(6, 'UTC')",
        "DateTime64(6, 'UTC')",
        prefix("clickhouse", "wall", "timestamp", False),
        "The other direction: a wall-clock time in a column with a zone, read by key as an "
        "instant. `at` is right here, so the refusal is about `wall` alone.",
    ),
    (
        "127-a-timestamptz-in-a-postgresql-timestamp",
        "postgres",
        "TIMESTAMP(6) WITHOUT TIME ZONE",
        "timestamp(6)",
        prefix("postgres", "at", "timestamptz", True),
        "PostgreSQL has the same two types and the same defect. Its type names are read in any "
        "ASCII case, as the server reads them.",
    ),
    (
        "128-a-timestamptz-in-a-nullable-clickhouse-column-without-a-zone",
        "clickhouse",
        "Nullable(DateTime64(3))",
        "Nullable(DateTime64(3))",
        prefix("clickhouse", "at", "timestamptz", True),
        "A nullable column is the type inside `Nullable(...)`, and its zone is that type's.",
    ),
    (
        "129-a-timestamptz-in-an-elasticsearch-long-timestamp",
        "elasticsearch",
        "long/timestamp",
        "long/timestamp",
        prefix("elasticsearch", "at", "timestamptz", True),
        "Elasticsearch keeps microseconds in a `long` and the neutral type after the slash, which "
        "is what a reader reads the value back by: the same defect in the second half.",
    ),
    (
        "130-two-columns-without-their-zones-the-first-field-is-named",
        "clickhouse",
        "DateTime64(6)",
        "DateTime64(6, 'UTC')",
        prefix("clickhouse", "at", "timestamptz", True),
        "Both columns are wrong, and the refusal names `at`: fields in name order, so one document "
        "refuses the same way in every language.",
    ),
]


def dump(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-am-changing-the-contract", action="store_true", required=True)
    parser.parse_args()
    for name, dialect, at, wall, match, why in CASES:
        directory = ERRORS / name
        directory.mkdir(exist_ok=True)
        engine = ENGINES[dialect][0]
        expected = {"error": "MapError", "stage": "session", "match": match, "why": why}
        for filename, value in (
            ("model.json", MODEL),
            ("map.json", placement(dialect, at, wall)),
            ("engines.json", {engine: {"dialect": dialect}}),
            ("calls.json", []),
            ("expected.json", expected),
        ):
            dump(directory / filename, value)
    print(f"Wrote {len(CASES)} session-stage refusals without importing an SDK")


if __name__ == "__main__":
    main()

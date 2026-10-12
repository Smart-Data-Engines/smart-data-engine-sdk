#!/usr/bin/env python3
"""Generate the write-stage error cases - a row the model does not allow - without SDK imports.

A model says which fields a row must carry: every field declared without ``nullable``, and every
key field. Until 2 October 2026 neither library checked a row against that before an engine saw it.
PostgreSQL then stored NULL in a required field, and ClickHouse stored a value nobody wrote: ``0``
for an integer the row left out, ``""`` for a string (measured on PostgreSQL 15 and ClickHouse 24.8,
SDK ``b30c9fd``). The same model meant two things, and moving a group changed which.

From 7 October 2026 a row is also refused when it gives a field a value its type does not hold
(section 8b, point 4). Each driver and engine had converted such a value in its own way, measured
through ``Session.save`` on the same two engines: a decimal with too many fractional digits was
rounded by PostgreSQL and truncated by ClickHouse, an integer outside int32 wrapped to the opposite
sign in ClickHouse from TypeScript, a date that does not exist moved two days.

Each case opens a session on a valid map, makes one accepted write - the control that shows the
engine records what it receives - and then the refused one. ``calls.json`` holds the accepted write
and nothing of the refused one: a refusal is made before any engine is called.

Every expectation is written here rather than computed by a library.

    python conformance/tools/write_vectors.py --i-am-changing-the-contract
"""

from __future__ import annotations

import argparse
import copy
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

# Case 080's model: the same entity with its key declared nullable, which the loader accepts. A
# key field is required whatever it declares, and only a nullable key can show that the rule is the
# key's and not merely the field's.
NULLABLE_KEY: dict[str, Any] = copy.deepcopy(MODEL)
NULLABLE_KEY["entities"][0]["fields"][1]["nullable"] = True
NULLABLE_KEY_VERSION = "480714072caaed67"  # its hash, under the same reasoning as MODEL_VERSION

ENGINES = {"pg-main": {"dialect": "postgres"}}

FULL = {"amount": 1, "id": "t-1", "label": "first", "note": None}
"""The accepted write: every field, and ``null`` where the model allows it."""


# The typed cases' model: one field of each kind a typed refusal names, and a nullable timestamp the
# accepted write can leave out.
ENTRY: dict[str, Any] = {
    "entities": [
        {
            "name": "Entry",
            "fields": [
                {"name": "amount", "type": "decimal(12,2)"},
                {"name": "at", "type": "timestamptz", "nullable": True},
                {"name": "count", "type": "int32"},
                {"name": "day", "type": "date"},
                {"name": "id", "type": "string"},
            ],
            "key": ["id"],
        }
    ]
}
ENTRY_VERSION = "1d78c15aa7623338"  # its hash, under the same reasoning as MODEL_VERSION

ENTRY_MAP: dict[str, Any] = {
    "contract": 3,
    "model_version": ENTRY_VERSION,
    "map_version": 1,
    "groups": {
        "Entry": {
            "source": {
                "id": "Entry@pg",
                "engine": "pg-main",
                "layout": {
                    "tables": {"Entry": "entry"},
                    "columns": {
                        "Entry": {
                            "amount": "numeric(12,2)",
                            "at": "timestamptz",
                            "count": "integer",
                            "day": "date",
                            "id": "text",
                        }
                    },
                },
            }
        }
    },
}

ENTRY_FULL = {"amount": "1.230", "at": None, "count": 1, "day": "2026-10-07", "id": "e-1"}
"""The typed cases' accepted write. ``1.230`` has three fractional digits and fits two: a trailing
zero says nothing about the value, and a rule that counted it would refuse what every engine stores
exactly."""


def entry(**values: Any) -> dict[str, Any]:
    return {"operation": "save", "entity": "Entry", "values": {**ENTRY_FULL, "id": "e-2", **values}}


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
        "A key field is required whether or not it is declared nullable, and this model declares "
        "it nullable, which the loader accepts: a row without its key cannot be read back by it.",
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


ACCEPTED_ENTRY = [{"engine": "pg-main", "call": "insert", "table": "entry"}]

TYPED_CASES: list[
    tuple[str, str, dict[str, Any], dict[str, Any], str, list[dict[str, Any]], str]
] = [
    (
        "113-a-save-whose-decimal-has-more-fractional-digits-than-its-scale",
        "ModelPlanningError",
        {"operation": "save", "entity": "Entry", "values": ENTRY_FULL},
        entry(amount="1.239"),
        r"Entry\.amount is decimal\(12,2\) and this row gives it more than 2 fractional digits",
        ACCEPTED_ENTRY,
        "PostgreSQL stored 1.24 and ClickHouse 1.23, from both libraries, and neither raised: one "
        "save, two values, and which one depended on where the group lived. Refused before either.",
    ),
    (
        "114-a-save-whose-decimal-has-more-integer-digits-than-its-precision-holds",
        "ModelPlanningError",
        {"operation": "save", "entity": "Entry", "values": ENTRY_FULL},
        entry(amount="12345678901.23"),
        r"Entry\.amount is decimal\(12,2\) and this row gives it more than 10 integer digits",
        ACCEPTED_ENTRY,
        "Thirteen digits into twelve. PostgreSQL refused it as an overflow, TypeScript's "
        "ClickHouse path refused it in ClickHouse's words, and the reference's ClickHouse driver "
        "stored 12345678901.20 - a digit changed and the column holding more than its precision.",
    ),
    (
        "115-a-save-whose-integer-is-outside-int32",
        "ModelPlanningError",
        {"operation": "save", "entity": "Entry", "values": ENTRY_FULL},
        entry(count=2147483648),
        r"Entry\.count is int32 and this row gives it an integer outside int32",
        ACCEPTED_ENTRY,
        "2^31 is one past int32. ClickHouse, sent it as JSON from TypeScript, stored "
        "-2147483648: the opposite sign, and a successful write.",
    ),
    (
        "116-a-save-that-gives-an-integer-field-a-boolean",
        "ModelPlanningError",
        {"operation": "save", "entity": "Entry", "values": ENTRY_FULL},
        entry(count=True),
        r"Entry\.count is int32 and this row gives it a value that is not an integer",
        ACCEPTED_ENTRY,
        "PostgreSQL refused a boolean in an integer column and ClickHouse stored 1, from both "
        "libraries. A boolean is not an integer here even where the host language says it is one.",
    ),
    (
        "117-a-save-whose-date-does-not-exist",
        "ModelPlanningError",
        {"operation": "save", "entity": "Entry", "values": ENTRY_FULL},
        entry(day="2026-02-30"),
        r"Entry\.day is date and this row gives it a value that is not a date",
        ACCEPTED_ENTRY,
        "PostgreSQL refused 30 February and ClickHouse stored 2 March. A date is checked as a date "
        "filter is: YYYY-MM-DD text of a day that exists.",
    ),
    (
        "118-a-save-whose-decimal-is-not-a-number",
        "ModelPlanningError",
        {"operation": "save", "entity": "Entry", "values": ENTRY_FULL},
        entry(amount="NaN"),
        r"Entry\.amount is decimal\(12,2\) and this row gives it "
        r"a value that is not an exact decimal",
        ACCEPTED_ENTRY,
        "PostgreSQL stored NaN in a numeric(12,2) column and ClickHouse refused it; JavaScript's "
        "NaN reached ClickHouse as 0.00. A decimal is an exact, finite number, written as a "
        "decimal filter is.",
    ),
    (
        "119-a-batch-row-whose-timestamp-is-not-one",
        "BulkWriteRefused",
        {"operation": "save_many", "entity": "Entry", "rows": [ENTRY_FULL]},
        {
            "operation": "save_many",
            "entity": "Entry",
            "rows": [
                {**ENTRY_FULL, "id": "e-3", "at": "2026-10-07T10:00:00Z"},
                {**ENTRY_FULL, "id": "e-4", "at": "2026-10-07T25:00:00Z"},
            ],
        },
        r"row 1: Entry\.at is timestamptz and this row gives it a value that is not a timestamp",
        [{"engine": "pg-main", "call": "insert_many", "table": "entry", "rows": 1}],
        "ClickHouse stored a timestamp made from any text - 1900-01-01 from words, 1970 from a "
        "number. The whole batch is refused, the row that fits included, and the row is named by "
        "its position.",
    ),
    (
        "120-a-row-that-misfits-twice-names-the-first-field",
        "ModelPlanningError",
        {"operation": "save", "entity": "Entry", "values": ENTRY_FULL},
        entry(amount="1.239", count=True, day="2026-02-30"),
        r"Entry\.amount is decimal\(12,2\) and this row gives it more than 2 fractional digits",
        ACCEPTED_ENTRY,
        "Three fields misfit. The first in code-point order is named, as every refusal of section "
        "8b names its first offending field, so two libraries give the same message.",
    ),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-am-changing-the-contract", action="store_true", required=True)
    parser.parse_args()
    for name, error, accepted, refused, match, calls, why in CASES + TYPED_CASES:
        directory = ERRORS / name
        directory.mkdir(exist_ok=True)
        model, placement = MODEL, MAP
        if name.startswith("080-"):
            model = NULLABLE_KEY
            placement = {**copy.deepcopy(MAP), "model_version": NULLABLE_KEY_VERSION}
        if (name, error, accepted, refused, match, calls, why) in TYPED_CASES:
            model, placement = ENTRY, ENTRY_MAP
        expected = {
            "error": error,
            "stage": "write",
            "match": match,
            "write": {"accepted": accepted, "refused": refused},
            "why": why,
        }
        for filename, value in (
            ("model.json", model),
            ("map.json", placement),
            ("engines.json", ENGINES),
            ("calls.json", calls),
            ("expected.json", expected),
        ):
            dump(directory / filename, value)
    print(f"Wrote {len(CASES) + len(TYPED_CASES)} write-stage refusals without importing an SDK")


if __name__ == "__main__":
    main()

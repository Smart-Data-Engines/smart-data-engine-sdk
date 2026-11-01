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


def save(values: dict[str, Any], entity: str = "Thing") -> dict[str, Any]:
    return {"operation": "save", "entity": entity, "values": values}


def save_many(rows: list[dict[str, Any]], entity: str = "Thing") -> dict[str, Any]:
    return {"operation": "save_many", "entity": entity, "rows": rows}


def get(key: dict[str, Any], entity: str) -> dict[str, Any]:
    """A point read, only ever as the refused step: of an entity the model does not declare."""
    return {"operation": "get", "entity": entity, "key": key}


ACCEPTED_ONE = [{"engine": "pg-main", "call": "insert", "table": "thing"}]

CASES: list[tuple[str, str, dict[str, Any], dict[str, Any], str, list[dict[str, Any]], str]] = [
    (
        "078-a-save-that-leaves-out-a-required-field",
        "ModelPlanningError",
        save(FULL),
        save({"id": "t-2", "label": "second", "note": None}),
        "Thing.amount is required and this row leaves it out",
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
        "Thing.amount is required and this row gives it null",
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
        "Thing.id is required and this row gives it null",
        ACCEPTED_ONE,
        "A key field is required whether or not it is declared nullable, and this model declares "
        "it nullable, which the loader accepts: a row without its key cannot be read back by it.",
    ),
    (
        "081-a-save-with-a-field-the-entity-does-not-declare",
        "ModelPlanningError",
        save(FULL),
        save({"amount": 2, "colour": "red", "id": "t-2", "label": "second", "note": None}),
        "Thing declares no field colour",
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
        "row 1: Thing.label is required and this row gives it null",
        [{"engine": "pg-main", "call": "insert_many", "table": "thing", "rows": 2}],
        "A batch already refused a row that left a required field out; it let a null through. The "
        "whole batch is refused, row 0 included, because a batch is checked before anything is "
        "sent. The row is named by its position and the field by name - never a value.",
    ),
    # An entity the model does not declare (format contract 8d), refused before any shape is
    # looked up. Until 31 October 2026 the three libraries refused each call in their own way.
    (
        "122-a-save-of-an-entity-the-model-does-not-declare",
        "ModelPlanningError",
        save(FULL),
        save({"amount": 2, "id": "g-1", "label": "ghost", "note": None}, entity="Ghost"),
        "this model has no entity 'Ghost'",
        ACCEPTED_ONE,
        "Python and C++ said the model admits no write on Ghost, and TypeScript that it has no "
        "write of Ghost: each read a name typed wrong as an operation the model forgot to shape. "
        "With hashed identifiers C++ and Python raised a DeclarationError instead. The entity is "
        "what is missing, and the refusal names it.",
    ),
    (
        "123-a-batch-of-an-entity-the-model-does-not-declare",
        "ModelPlanningError",
        save(FULL),
        save_many([{"amount": 2, "id": "g-1", "label": "ghost", "note": None}], entity="Ghost"),
        "this model has no entity 'Ghost'",
        ACCEPTED_ONE,
        "The batch's refusal is the entity's, not a batch's: the class is ModelPlanningError "
        "exactly, never its BulkWriteRefused, since no row was looked at.",
    ),
    (
        "124-a-point-read-of-an-entity-the-model-does-not-declare",
        "ModelPlanningError",
        save(FULL),
        get({"id": "g-1"}, entity="Ghost"),
        "this model has no entity 'Ghost'",
        ACCEPTED_ONE,
        "Python raised a bare KeyError from the model's lookup, C++ a DeclarationError, and "
        "TypeScript already the words above. A point read reaches no engine either.",
    ),
]


ACCEPTED_ENTRY = [{"engine": "pg-main", "call": "insert", "table": "entry"}]

# The key cases' model: a key of two typed fields, each of a kind a typed refusal names, so a point
# read can give either a value its type does not hold (format contract 8b, a point read's key).
LEDGER: dict[str, Any] = {
    "entities": [
        {
            "name": "Ledger",
            "fields": [
                {"name": "code", "type": "decimal(12,2)"},
                {"name": "id", "type": "int64"},
                {"name": "note", "type": "string", "nullable": True},
            ],
            "key": ["code", "id"],
        }
    ]
}
LEDGER_VERSION = "d4229e11a64d523a"  # its hash, under the same reasoning as MODEL_VERSION

LEDGER_MAP: dict[str, Any] = {
    "contract": 3,
    "model_version": LEDGER_VERSION,
    "map_version": 1,
    "groups": {
        "Ledger": {
            "source": {
                "id": "Ledger@pg",
                "engine": "pg-main",
                "layout": {
                    "tables": {"Ledger": "ledger"},
                    "columns": {
                        "Ledger": {"code": "numeric(12,2)", "id": "bigint", "note": "text"}
                    },
                },
            }
        }
    },
}

LEDGER_SAVE = {
    "operation": "save",
    "entity": "Ledger",
    "values": {"code": "1.23", "id": 7, "note": "x"},
}
"""The key cases' accepted write, which the refused point read would find under its own key."""

ACCEPTED_LEDGER = [{"engine": "pg-main", "call": "insert", "table": "ledger"}]


def ledger_get(**key: Any) -> dict[str, Any]:
    return get({"code": "1.23", "id": 7, **key}, "Ledger")


KEY_CASES: list[tuple[str, dict[str, Any], str, str]] = [
    (
        "131-a-point-read-whose-int64-key-is-text",
        ledger_get(id="7"),
        "Ledger.id is int64 and this key gives it a value that is not an integer",
        "PostgreSQL and ClickHouse both found the row for the text 7, from both libraries, and the "
        "orderbook reference read a time given as text with int() where TypeScript answered no "
        "row. A key is held to its field's type before any engine, as a written value is.",
    ),
    (
        "132-a-point-read-whose-decimal-key-has-more-fractional-digits-than-its-scale",
        ledger_get(code="1.234"),
        "Ledger.code is decimal(12,2) and this key gives it more than 2 fractional digits",
        "PostgreSQL answered no row, and ClickHouse found the row stored as 1.23, reading the "
        "parameter at its column's scale: one call, two answers, by where the group lived.",
    ),
    (
        "133-a-point-read-that-gives-an-int64-key-a-boolean",
        ledger_get(id=True),
        "Ledger.id is int64 and this key gives it a value that is not an integer",
        "PostgreSQL raised an engine error and ClickHouse answered no row, from both libraries.",
    ),
    (
        "134-a-point-read-whose-key-value-is-null",
        ledger_get(id=None),
        "Ledger.id is required and this key gives it null",
        "A key field is never null, so no row could answer. Both engines said no row; the library "
        "says why, in a written row's words.",
    ),
    (
        "135-a-key-that-misfits-twice-names-the-first-field",
        ledger_get(code="1.234", id="7"),
        "Ledger.code is decimal(12,2) and this key gives it more than 2 fractional digits",
        "Two fields misfit. The first in code-point order is named, as a row's refusal names it.",
    ),
    (
        "136-a-null-in-a-key-is-named-before-a-misfit",
        ledger_get(code="1.234", id=None),
        "Ledger.id is required and this key gives it null",
        "A null is refused before any value's type is checked, as a row's is (8b, points 3 and "
        "4), so the null is named although code comes first in code-point order.",
    ),
]

TYPED_CASES: list[
    tuple[str, str, dict[str, Any], dict[str, Any], str, list[dict[str, Any]], str]
] = [
    (
        "113-a-save-whose-decimal-has-more-fractional-digits-than-its-scale",
        "ModelPlanningError",
        {"operation": "save", "entity": "Entry", "values": ENTRY_FULL},
        entry(amount="1.239"),
        "Entry.amount is decimal(12,2) and this row gives it more than 2 fractional digits",
        ACCEPTED_ENTRY,
        "PostgreSQL stored 1.24 and ClickHouse 1.23, from both libraries, and neither raised: one "
        "save, two values, and which one depended on where the group lived. Refused before either.",
    ),
    (
        "114-a-save-whose-decimal-has-more-integer-digits-than-its-precision-holds",
        "ModelPlanningError",
        {"operation": "save", "entity": "Entry", "values": ENTRY_FULL},
        entry(amount="12345678901.23"),
        "Entry.amount is decimal(12,2) and this row gives it more than 10 integer digits",
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
        "Entry.count is int32 and this row gives it an integer outside int32",
        ACCEPTED_ENTRY,
        "2^31 is one past int32. ClickHouse, sent it as JSON from TypeScript, stored "
        "-2147483648: the opposite sign, and a successful write.",
    ),
    (
        "116-a-save-that-gives-an-integer-field-a-boolean",
        "ModelPlanningError",
        {"operation": "save", "entity": "Entry", "values": ENTRY_FULL},
        entry(count=True),
        "Entry.count is int32 and this row gives it a value that is not an integer",
        ACCEPTED_ENTRY,
        "PostgreSQL refused a boolean in an integer column and ClickHouse stored 1, from both "
        "libraries. A boolean is not an integer here even where the host language says it is one.",
    ),
    (
        "117-a-save-whose-date-does-not-exist",
        "ModelPlanningError",
        {"operation": "save", "entity": "Entry", "values": ENTRY_FULL},
        entry(day="2026-02-30"),
        "Entry.day is date and this row gives it a value that is not a date",
        ACCEPTED_ENTRY,
        "PostgreSQL refused 30 February and ClickHouse stored 2 March. A date is checked as a date "
        "filter is: YYYY-MM-DD text of a day that exists.",
    ),
    (
        "118-a-save-whose-decimal-is-not-a-number",
        "ModelPlanningError",
        {"operation": "save", "entity": "Entry", "values": ENTRY_FULL},
        entry(amount="NaN"),
        "Entry.amount is decimal(12,2) and this row gives it "
        "a value that is not an exact decimal",
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
        "row 1: Entry.at is timestamptz and this row gives it a value that is not a timestamp",
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
        "Entry.amount is decimal(12,2) and this row gives it more than 2 fractional digits",
        ACCEPTED_ENTRY,
        "Three fields misfit. The first in code-point order is named, as every refusal of section "
        "8b names its first offending field, so two libraries give the same message.",
    ),
]


# Text every engine holds: U+0000 is a Unicode scalar value, PostgreSQL's text cannot hold it, and
# ClickHouse stores it. Numbered after errors/131-136, which a point read's key holds.
NUL_CASES: list[tuple[str, str, dict[str, Any], dict[str, Any], str, list[dict[str, Any]], str]] = [
    (
        "137-a-save-whose-text-holds-u0000",
        "ModelPlanningError",
        save(FULL),
        save({**FULL, "id": "t-2", "label": "fir\u0000st"}),
        "Thing.label is string and this row gives it text holding U+0000",
        ACCEPTED_ONE,
        "Measured from the reference on 1 November 2026: PostgreSQL's driver refused this save, "
        "and ClickHouse stored the text with its U+0000. A copy of such a group from ClickHouse to "
        "PostgreSQL stopped at the chunk holding it, with the driver's error, after 2 of 5 rows. "
        "Refused before either engine.",
    ),
    (
        "138-a-batch-row-whose-nullable-text-holds-u0000",
        "BulkWriteRefused",
        save_many([FULL]),
        save_many([{**FULL, "id": "t-3"}, {**FULL, "id": "t-4", "note": "\u0000"}]),
        "row 1: Thing.note is string and this row gives it text holding U+0000",
        [{"engine": "pg-main", "call": "insert_many", "table": "thing", "rows": 1}],
        "A nullable field holds null, and U+0000 is not null. The whole batch is refused, the row "
        "that fits included, and the row is named by its position.",
    ),
    (
        "139-a-save-whose-key-holds-u0000",
        "ModelPlanningError",
        save(FULL),
        save({**FULL, "id": "t\u00005"}),
        "Thing.id is string and this row gives it text holding U+0000",
        ACCEPTED_ONE,
        "A key's text is held to the same rule: the row it addresses is the row one engine refuses "
        "and another keeps.",
    ),
    (
        "143-a-point-read-whose-key-holds-u0000",
        "ModelPlanningError",
        save(FULL),
        get({"id": "t\u00001"}, "Thing"),
        "Thing.id is string and this key gives it text holding U+0000",
        ACCEPTED_ONE,
        "A point read's key is held to a written row's rules (section 8b, \"A point read's key\"), "
        "so a key holding U+0000 is refused in a key's words, before any engine is called.",
    ),
]


# A date and a timestamp outside the range of an engine the write goes to. The range is the
# server's own, as ClickHouse 24.8 answers it (section 8b): 24.8 moved a value outside to the nearer
# end without an error, and 26.9 holds the whole range. Numbered after errors/131-139.
CH_RANGE = {
    "first_day": "1900-01-01",
    "last_day": "2299-12-31",
    "first_instant": "1900-01-01 00:00:00.000000",
    "last_instant": "2299-12-31 23:59:59.999999",
}
CH_COLUMNS = {
    "amount": "Decimal(12, 2)",
    "at": "Nullable(DateTime64(6, 'UTC'))",
    "count": "Int32",
    "day": "Date32",
    "id": "String",
}
RANGE_MAP: dict[str, Any] = copy.deepcopy(ENTRY_MAP)
RANGE_MAP["groups"]["Entry"]["source"] = {
    "id": "Entry@ch",
    "engine": "ch-main",
    "layout": {"tables": {"Entry": "entry"}, "columns": {"Entry": CH_COLUMNS}},
}
RANGE_ENGINES = {"ch-main": {"dialect": "clickhouse", "temporal_range": CH_RANGE}}
COPY_MAP: dict[str, Any] = copy.deepcopy(ENTRY_MAP)
COPY_MAP["groups"]["Entry"]["derived"] = [
    {
        "id": "Entry@ch",
        "engine": "ch-copy",
        "layout": {"tables": {"Entry": "entry_copy"}, "columns": {"Entry": CH_COLUMNS}},
        "lag_budget_ms": 30000,
    }
]
COPY_MAP["groups"]["Entry"]["also_write"] = ["Entry@ch"]
COPY_ENGINES = {
    "pg-main": {"dialect": "postgres"},
    "ch-copy": {"dialect": "clickhouse", "temporal_range": CH_RANGE},
}
EDGES = {**ENTRY_FULL, "at": "2299-12-31T23:59:59.999999Z", "day": "1900-01-01"}
"""The accepted write of the range cases: both ends, which the range holds."""

RANGE_CASES: list[
    tuple[
        str, str, dict[str, Any], dict[str, Any], str, list[dict[str, Any]], str,
        dict[str, Any], dict[str, Any],
    ]
] = [
    (
        "140-a-save-whose-date-is-outside-its-engines-range",
        "EngineError",
        {"operation": "save", "entity": "Entry", "values": EDGES},
        entry(day="1800-01-01"),
        "ch-main holds a date from 1900-01-01 to 2299-12-31, and this row gives Entry.day one "
        "outside it; nothing was sent",
        [{"engine": "ch-main", "call": "insert", "table": "entry"}],
        "Measured on 1 November 2026: ClickHouse 24.8 stored 1800-01-01 as 1900-01-01, without an "
        "error, and 26.9 stored it. The range is the server's, read from it, and a row outside it "
        "is refused before the engine. The accepted save gives both ends of the range.",
        RANGE_MAP,
        RANGE_ENGINES,
    ),
    (
        "141-a-batch-row-whose-timestamp-is-outside-its-engines-range",
        "EngineError",
        {"operation": "save_many", "entity": "Entry", "rows": [EDGES]},
        {
            "operation": "save_many",
            "entity": "Entry",
            "rows": [
                {**ENTRY_FULL, "id": "e-3"},
                {**ENTRY_FULL, "id": "e-4", "at": "2500-06-01T12:00:00Z"},
            ],
        },
        "row 1: ch-main holds a timestamp from 1900-01-01 to 2299-12-31, and this row gives "
        "Entry.at one outside it; nothing was sent",
        [{"engine": "ch-main", "call": "insert_many", "table": "entry", "rows": 1}],
        "ClickHouse 24.8 stored 2500-06-01 12:00 as 2299-12-31 12:00. The whole batch is refused, "
        "the row that fits included, and the row is named by its position.",
        RANGE_MAP,
        RANGE_ENGINES,
    ),
    (
        "142-a-save-whose-copy-cannot-hold-its-date",
        "EngineError",
        {"operation": "save", "entity": "Entry", "values": ENTRY_FULL},
        entry(day="1800-01-01"),
        "ch-copy holds a date from 1900-01-01 to 2299-12-31, and this row gives Entry.day one "
        "outside it; nothing was sent",
        [
            {"engine": "pg-main", "call": "insert", "table": "entry"},
            {"engine": "ch-copy", "call": "insert", "table": "entry_copy"},
        ],
        "PostgreSQL holds 1800-01-01 and the group's copy on ClickHouse 24.8 would not. Every "
        "engine the write goes to is asked before any is written, so the source does not take a "
        "row its copy then refuses.",
        COPY_MAP,
        COPY_ENGINES,
    ),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-am-changing-the-contract", action="store_true", required=True)
    parser.parse_args()
    for name, error, accepted, refused, match, calls, why in CASES + TYPED_CASES + NUL_CASES:
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
    for name, refused, match, why in KEY_CASES:
        directory = ERRORS / name
        directory.mkdir(exist_ok=True)
        expected = {
            "error": "ModelPlanningError",
            "stage": "write",
            "match": match,
            "write": {"accepted": LEDGER_SAVE, "refused": refused},
            "why": why,
        }
        for filename, value in (
            ("model.json", LEDGER),
            ("map.json", LEDGER_MAP),
            ("engines.json", ENGINES),
            ("calls.json", ACCEPTED_LEDGER),
            ("expected.json", expected),
        ):
            dump(directory / filename, value)
    for name, error, accepted, refused, match, calls, why, placement, engines in RANGE_CASES:
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
            ("model.json", ENTRY),
            ("map.json", placement),
            ("engines.json", engines),
            ("calls.json", calls),
            ("expected.json", expected),
        ):
            dump(directory / filename, value)
    total = len(CASES) + len(TYPED_CASES) + len(KEY_CASES) + len(NUL_CASES) + len(RANGE_CASES)
    print(f"Wrote {total} write-stage refusals without importing an SDK")


if __name__ == "__main__":
    main()

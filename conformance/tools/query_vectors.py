"""Generate logical-read and exact-summary witnesses without rewriting any older family."""

from __future__ import annotations

import argparse
import json
import struct
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python/src"))
from sde import canonical_bytes  # noqa: E402
from sde.query import QueryRefused, Range, ReadColumn, numeric_summary, plan_read  # noqa: E402


def decode(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"$int"}:
        return int(value["$int"])
    if isinstance(value, dict) and set(value) == {"$bytes"}:
        return bytes.fromhex(value["$bytes"])
    if isinstance(value, list):
        return [decode(item) for item in value]
    if isinstance(value, dict):
        return {name: decode(item) for name, item in value.items()}
    return value


def normalized(column: ReadColumn, value: Any) -> Any:
    if value is None or column.type == "bool":
        return value
    if column.type.startswith("float"):
        return struct.pack(">d", value).hex()
    if column.type.startswith("decimal("):
        return format(value, "f")
    if isinstance(value, datetime):
        aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return aware.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if column.type == "bytes":
        return bytes(value).hex()
    return str(value)


def outcome(case: dict[str, Any]) -> dict[str, Any]:
    try:
        if case["kind"] == "summary":
            result = numeric_summary(
                case["record"], ReadColumn("value", case["type"]), case.get("mean_scale", 6)
            )
            return {
                name: None if value is None else str(value) for name, value in vars(result).items()
            }
        columns = tuple(ReadColumn(name, kind) for name, kind in case["columns"].items())
        options = decode(case.get("options", {}))
        if options.get("bounds") is not None:
            options["bounds"] = Range(**options["bounds"])
        plan = plan_read(columns, case["key"], **options)
        return {
            "filters": [
                {
                    "field": item.column.name,
                    "type": item.column.type,
                    "op": item.operation,
                    "value": normalized(item.column, item.value),
                }
                for item in plan.filters
            ],
            "order": [item.name for item in plan.order],
            "descending": plan.descending,
            "after": None
            if plan.after is None
            else [
                normalized(column, value)
                for column, value in zip(plan.order, plan.after, strict=True)
            ],
            "limit": plan.limit,
        }
    except QueryRefused:
        return {"error": "QueryRefused"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if not args.write:
        parser.error("--write is required")
    base = {
        "kind": "plan",
        "columns": {
            "id": "int64",
            "label": "string",
            "at": "timestamptz",
            "amount": "decimal(12,2)",
        },
        "key": ["id"],
    }
    cases = [
        ("default-key-page", base),
        (
            "nullable-descending",
            {
                **base,
                "options": {
                    "order_by": "label",
                    "descending": True,
                    "after": {"label": None, "id": 7},
                    "limit": 1,
                },
            },
        ),
        (
            "time-range-equality",
            {
                **base,
                "options": {
                    "where": {"label": "e\u0301"},
                    "bounds": {
                        "field": "at",
                        "low": "2026-09-14T12:00:00.123456+02:00",
                        "high": "2026-09-14T10:00:00.123457Z",
                    },
                    "order_by": "at",
                    "limit": 2,
                },
            },
        ),
        (
            "fine-decimal-bound",
            {**base, "options": {"bounds": {"field": "amount", "low": "123456.005"}}},
        ),
        (
            "exact-int64-position",
            {**base, "options": {"after": {"id": {"$int": "9007199254740993"}}}},
        ),
        (
            "three-part-position",
            {
                "kind": "plan",
                "columns": {"a": "int64", "b": "int64", "c": "int64"},
                "key": ["a", "b", "c"],
                "options": {"after": {"c": 3, "a": 1, "b": 2}},
            },
        ),
        (
            "canonical-uuid",
            {
                "kind": "plan",
                "columns": {"id": "uuid"},
                "key": ["id"],
                "options": {"after": {"id": "FFFFFFFF-FFFF-FFFF-0000-000000000000"}},
            },
        ),
        (
            "timestamp-naive-utc",
            {
                "kind": "plan",
                "columns": {"at": "timestamp"},
                "key": ["at"],
                "options": {"after": {"at": "1965-09-14T12:00:00.123456+02:00"}},
            },
        ),
        (
            "binary-key",
            {
                "kind": "plan",
                "columns": {"id": "bytes"},
                "key": ["id"],
                "options": {"after": {"id": {"$bytes": "0001ff80"}}},
            },
        ),
        ("empty-range-refused", {**base, "options": {"bounds": {"field": "at"}}}),
        (
            "partial-position-refused",
            {**base, "options": {"order_by": "label", "after": {"id": 1}}},
        ),
        ("bool-limit-refused", {**base, "options": {"limit": True}}),
        ("float-order-refused", {"kind": "plan", "columns": {"id": "float64"}, "key": ["id"]}),
        (
            "float-count-unordered",
            {
                "kind": "plan",
                "columns": {"id": "float64"},
                "key": ["id"],
                "options": {"paginate": False},
            },
        ),
        ("unknown-field-refused", {**base, "options": {"where": {"missing": "private"}}}),
    ]
    for name, total, present, scale in [
        ("int64-sum-wide", "18446744073709551614", "2", 6),
        ("tie-to-even-zero", "1", "2", 0),
        ("tie-to-even-two", "3", "2", 0),
        ("negative-rounded-zero", "-1", "2", 0),
        ("third-rounded", "2", "3", 6),
    ]:
        cases.append(
            (
                name,
                {
                    "kind": "summary",
                    "type": "int64",
                    "mean_scale": scale,
                    "record": {
                        "sde_count": present,
                        "sde_present": present,
                        "sde_min": "0",
                        "sde_max": total,
                        "sde_total": total,
                    },
                },
            )
        )
    cases += [
        (
            "decimal-scale-restored",
            {
                "kind": "summary",
                "type": "decimal(12,2)",
                "mean_scale": 3,
                "record": {
                    "sde_count": "2",
                    "sde_present": "2",
                    "sde_min": "3.25",
                    "sde_max": "5.25",
                    "sde_total": "8.5",
                },
            },
        ),
        (
            "all-null-summary",
            {
                "kind": "summary",
                "type": "int64",
                "record": {
                    "sde_count": "3",
                    "sde_present": "0",
                    "sde_min": "0",
                    "sde_max": "0",
                    "sde_total": "0",
                },
            },
        ),
        ("wide-precision-refused", {"kind": "summary", "type": "decimal(57,18)", "record": {}}),
    ]
    for index, (name, case) in enumerate(cases, 1):
        expected = outcome(case)
        folder = ROOT / "conformance/vectors/query" / f"{index:03d}-{name}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "case.json").write_text(json.dumps(case, ensure_ascii=False, indent=2) + "\n")
        (folder / "expected.json").write_text(
            json.dumps(expected, ensure_ascii=False, indent=2) + "\n"
        )
        (folder / "expected.hex").write_text(canonical_bytes(expected).hex() + "\n")


if __name__ == "__main__":
    main()

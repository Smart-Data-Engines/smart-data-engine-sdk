"""Both libraries must produce the same logical plans and exact summary values."""

from __future__ import annotations

import json
import struct
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import sde
from sde.query import Range, ReadColumn, numeric_summary, plan_read

ROOT = Path(__file__).parents[2] / "conformance/vectors/query"


def decode(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"$int"}:
        return int(value["$int"])
    if isinstance(value, dict) and set(value) == {"$bytes"}:
        return bytes.fromhex(value["$bytes"])
    if isinstance(value, dict):
        return {name: decode(item) for name, item in value.items()}
    if isinstance(value, list):
        return [decode(item) for item in value]
    return value


def normalized(column: ReadColumn, value: Any) -> Any:
    if value is None or column.type == "bool":
        return value
    if column.type.startswith("float"):
        return struct.pack(">d", value).hex()
    if column.type.startswith("decimal("):
        return format(value, "f")
    if isinstance(value, datetime):
        instant = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return instant.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if column.type == "bytes":
        return bytes(value).hex()
    return str(value)


@pytest.mark.parametrize("folder", sorted(ROOT.iterdir()), ids=lambda path: path.name)
def test_query_vector(folder: Path) -> None:
    case = json.loads((folder / "case.json").read_text())
    expected = json.loads((folder / "expected.json").read_text())
    try:
        if case["kind"] == "summary":
            summary = numeric_summary(
                case["record"], ReadColumn("value", case["type"]), case.get("mean_scale", 6)
            )
            got = {
                name: None if value is None else str(value) for name, value in vars(summary).items()
            }
        else:
            columns = tuple(ReadColumn(name, kind) for name, kind in case["columns"].items())
            options = decode(case.get("options", {}))
            if options.get("bounds") is not None:
                options["bounds"] = Range(**options["bounds"])
            result = plan_read(columns, case["key"], **options)
            got = {
                "filters": [
                    {
                        "field": item.column.name,
                        "type": item.column.type,
                        "op": item.operation,
                        "value": normalized(item.column, item.value),
                    }
                    for item in result.filters
                ],
                "order": [item.name for item in result.order],
                "descending": result.descending,
                "after": None
                if result.after is None
                else [
                    normalized(column, value)
                    for column, value in zip(result.order, result.after, strict=True)
                ],
                "limit": result.limit,
            }
    except sde.QueryRefused:
        got = {"error": "QueryRefused"}
    assert got == expected
    assert sde.canonical_bytes(got).hex() == (folder / "expected.hex").read_text().strip()

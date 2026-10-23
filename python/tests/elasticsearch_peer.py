"""The Python side of the TypeScript Elasticsearch slice: write or read rows of an existing index.

    elasticsearch_peer.py write INDEX < rows.json
    elasticsearch_peer.py read INDEX < keys.json > rows.json
    elasticsearch_peer.py fence-prepare INDEX < {"project_id": ..., "epoch": N} > state.json
    elasticsearch_peer.py fence-state INDEX < {"project_id": ...} > state.json

Rows travel as text where JSON cannot carry a value exactly - an integer as digits, an instant in
ISO form, a decimal as its text - and each side decodes them by the index's own column types.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sde.engines.elasticsearch import ElasticsearchEngine


def decode(kind: str, value: Any) -> Any:
    if value is None:
        return None
    if kind in ("integer", "long"):
        return int(value)
    if kind in ("long/timestamp", "long/timestamptz"):
        return datetime.fromisoformat(value)
    if kind.startswith("long/decimal("):
        return Decimal(value)
    if kind == "keyword/uuid":
        return UUID(value)
    if kind == "date":
        return date.fromisoformat(value)
    return value


def encode(kind: str, value: Any) -> Any:
    if value is None:
        return None
    if kind in ("integer", "long"):
        return str(value)
    if kind in ("long/timestamp", "long/timestamptz", "date"):
        return value.isoformat()
    if kind.startswith("long/decimal(") or kind == "keyword/uuid":
        return str(value)
    return value


def main() -> int:
    action, table = sys.argv[1], sys.argv[2]
    engine = ElasticsearchEngine(os.environ["SDE_ELASTICSEARCH_DSN"])
    engine.connect()
    try:
        if action.startswith("fence-"):
            request = json.load(sys.stdin)
            fence = engine.write_fence(table, project_id=request["project_id"])
            state = fence.prepare(request["epoch"]) if action == "fence-prepare" else fence.state()
            json.dump(state.as_record(), sys.stdout)
            return 0
        columns = engine._index(table).columns
        payload = json.load(sys.stdin)
        if action == "write":
            rows = [{name: decode(columns[name], value) for name, value in row.items()}
                    for row in payload]
            engine.insert_many(table, rows)
            return 0
        found = []
        for key in payload:
            wanted = {name: decode(columns[name], value) for name, value in key.items()}
            row = engine.get(table, wanted)
            found.append(None if row is None else {
                name: encode(columns[name], value) for name, value in row.items()
            })
        json.dump(found, sys.stdout)
        return 0
    finally:
        engine.close()


if __name__ == "__main__":
    raise SystemExit(main())

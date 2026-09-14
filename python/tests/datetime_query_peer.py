"""Read TypeScript-written temporal keys through the actual Python ClickHouse query adapter."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime

from sde.engines.clickhouse import ClickHouseEngine

request = json.load(sys.stdin)
times = [datetime.fromisoformat(value) for value in request["times"]]
with ClickHouseEngine(os.environ["SDE_TEMPORAL_PEER_DSN"]) as engine:
    points = [
        engine.get("temporal_queries", {"at": at, "id": index})
        for index, at in enumerate(times, start=1)
    ]
    ranged = engine.range("temporal_queries", "at", low=times[0], high=times[2])
    keyed = engine.key_range(
        "temporal_queries", ["at", "id"], after=[times[0], 1], upto=[times[2], 3]
    )
    print(
        json.dumps(
            {
                "points": [None if row is None else row["id"] for row in points],
                "range": [row["id"] for row in ranged],
                "keyset": [row["id"] for row in keyed],
            }
        )
    )

"""A separate Python SDK process for the TypeScript live interoperability tests.

The writer and final reader must not share TypeScript's timestamp codec: two identically lossy
readers used to certify a corrupt migration. These are real SDK operations, not a second codec.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime

from sde.engines.clickhouse import ClickHouseEngine
from sde.engines.postgres import PostgresEngine


def main() -> None:
    action, dialect, table = sys.argv[1:]
    engine = (
        PostgresEngine(os.environ["SDE_POSTGRES_DSN"])
        if dialect == "postgres"
        else ClickHouseEngine(os.environ["SDE_CLICKHOUSE_DSN"])
    )
    engine.connect()
    try:
        if action == "write":
            for row in json.load(sys.stdin):
                engine.insert(
                    table,
                    {
                        "id": int(row["id"]),
                        "at": datetime.fromisoformat(row["at"]),
                        "naive": datetime.fromisoformat(row["naive"]).replace(tzinfo=None),
                    },
                )
        elif action == "read":
            result = []
            for row in engine.key_range(table, ["at", "id"]):
                result.append(
                    {
                        "id": str(row["id"]),
                        "at": row["at"].astimezone(UTC).isoformat(timespec="microseconds"),
                        "naive": row["naive"].isoformat(timespec="microseconds"),
                    }
                )
            print(json.dumps(result))
        else:
            raise ValueError(f"unknown peer action: {action}")
    finally:
        engine.close()


if __name__ == "__main__":
    main()

"""A separate SDK process for the live native fencing interoperability tests."""

from __future__ import annotations

import json
import os
import sys
from urllib.parse import urlsplit, urlunsplit

from sde.engines.clickhouse import ClickHouseEngine
from sde.engines.postgres import PostgresEngine
from sde.schema import QUOTE


def main() -> None:
    action, dialect, namespace, table = sys.argv[1:]
    if dialect == "postgres":
        engine = PostgresEngine(os.environ["SDE_POSTGRES_DSN"])
        engine.connect()
        engine._cx.execute(f"SET search_path TO {QUOTE['postgres'](namespace)}")
    else:
        parts = urlsplit(os.environ["SDE_CLICKHOUSE_DSN"])
        dsn = urlunsplit((parts.scheme, parts.netloc, "/" + namespace, parts.query, parts.fragment))
        engine = ClickHouseEngine(dsn)  # type: ignore[assignment]
        engine.connect()
    try:
        fence = engine.write_fence(table, project_id="1" * 32)
        if action == "prepare":
            state = fence.prepare(1)
        elif action == "advance":
            fence.freeze("2" * 32)
            fence.advance(2)
            state = fence.release("2" * 32)
        elif action == "read":
            state = fence.state()
        else:
            raise ValueError(action)
        print(json.dumps(state.as_record()))
    finally:
        engine.close()


if __name__ == "__main__":
    main()

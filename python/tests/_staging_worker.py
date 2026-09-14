"""Disposable staging worker for real process-crash recovery tests."""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

import sde
from sde.engines.clickhouse import ClickHouseEngine
from sde.engines.postgres import PostgresEngine
from sde.local_cutover import LocalCutover
from sde.testing.loader import model_from_neutral


def ready() -> None:
    print("READY", flush=True)
    os.kill(os.getpid(), signal.SIGSTOP)


def main() -> None:
    request: dict[str, Any] = json.load(sys.stdin)
    operators: dict[str, Any] = {}
    runtime: dict[str, list[Any]] = {}
    for name in ("postgres", "clickhouse"):
        kind = PostgresEngine if name == "postgres" else ClickHouseEngine
        operators[name] = kind(request["operators"][name])
        operators[name].connect()
        runtime[name] = [kind(request["runtime"][name])]
        runtime[name][0].connect()
    model = model_from_neutral(request["model"])
    key = bytes.fromhex(request["public_key"])
    plan = sde.load_staging_plan(
        request["plan"], model=model, project_id=request["project_id"], public_key=key
    )
    checkpoint = request["checkpoint"]
    if checkpoint == "native:created":
        from sde.engines._staging import NativeStaging

        original = NativeStaging.create_table

        def stop_after_creation(self: Any, **kwargs: Any) -> Any:
            result = original(self, **kwargs)
            ready()
            return result

        NativeStaging.create_table = stop_after_creation
    if checkpoint == "native:pg-before-comment":
        connection = operators["postgres"]._cx

        class PauseConnection:
            def __getattr__(self, name: str) -> Any:
                return getattr(connection, name)

            def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
                result = connection.execute(statement, *args, **kwargs)
                if isinstance(statement, str) and statement.startswith("CREATE TABLE "):
                    ready()
                return result

        operators["postgres"]._conn = PauseConnection()
    executor = LocalCutover(
        Path(request["directory"]),
        model=model,
        project_id=request["project_id"],
        public_key=key,
        operators=operators,
        runtime=runtime,
        chunk_rows=1,
    )

    def stop(step: str) -> None:
        if step == checkpoint:
            ready()

    executor._after_step = stop
    executor.stage(plan)
    raise AssertionError("the requested crash checkpoint did not execute")


if __name__ == "__main__":
    main()

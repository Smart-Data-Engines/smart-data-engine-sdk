"""Disposable native worker for hard-kill and project-lock integration tests."""

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
    plan = sde.load_cutover_plan(
        request["plan"], model=model, project_id=request["project_id"], public_key=key
    )
    checkpoint = request["checkpoint"]
    if checkpoint == "native:detach":
        from sde.engines._write_fences import ClickHouseFences

        original = ClickHouseFences._command

        def stop_after_detach(self: Any, sql: str) -> None:
            original(self, sql)
            if sql.startswith("DETACH TABLE"):
                ready()

        ClickHouseFences._command = stop_after_detach
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
    executor.execute(plan)
    raise AssertionError("the requested crash checkpoint did not execute")


if __name__ == "__main__":
    main()

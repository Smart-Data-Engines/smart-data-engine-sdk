"""Disposable in-place index build worker for real process-crash recovery tests."""

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
    plan = sde.load_index_plan(
        request["plan"], model=model, project_id=request["project_id"], public_key=key
    )
    executor = LocalCutover(
        Path(request["directory"]),
        model=model,
        project_id=request["project_id"],
        public_key=key,
        operators=operators,
        runtime=runtime,
    )
    checkpoint = request["checkpoint"]
    if checkpoint == "native":
        # The parent watches the engine's own catalogue for the build and kills this process
        # while the engine is still working on it.
        print("STARTED", flush=True)
    else:

        def stop(step: str) -> None:
            if step == checkpoint:
                ready()

        executor._after_step = stop
    executor.index(plan)
    raise AssertionError("the worker finished instead of being killed")


if __name__ == "__main__":
    main()

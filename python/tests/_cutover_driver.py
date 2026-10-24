"""Run the Python local executor for a separate TypeScript application process."""

from __future__ import annotations

import json
import sys
from typing import Any

import sde
from sde.local_cutover import LocalCutover
from sde.testing.loader import model_from_neutral
from sde_operator.__main__ import adapter

request: dict[str, Any] = json.load(sys.stdin)
model = model_from_neutral(request["model"])
public = bytes.fromhex(request["public_key"])
plan = sde.load_cutover_plan(
    request["plan"], model=model, project_id=request["project_id"], public_key=public
)
operators: dict[str, Any] = {}
runtime: dict[str, Any] = {}
try:
    # Each binding's dialect, by default its name: ``postgres`` and ``clickhouse``.
    names = sorted(request["operators"])
    dialects: dict[str, str] = request.get("dialects", {name: name for name in names})
    for name in names:
        kind = adapter(dialects[name])
        operators[name] = kind(request["operators"][name])
        operators[name].connect()
        runtime[name] = kind(request["runtime"][name])
        runtime[name].connect()
    executor = LocalCutover(
        request["directory"],
        model=model,
        project_id=request["project_id"],
        public_key=public,
        operators=operators,
        runtime={name: [engine] for name, engine in runtime.items()},
        chunk_rows=1,
    )
    executor.enroll(request["plan"]["before"])
    print(json.dumps(executor.execute(plan).as_record()), flush=True)
finally:
    for engine in [*operators.values(), *runtime.values()]:
        engine.close()

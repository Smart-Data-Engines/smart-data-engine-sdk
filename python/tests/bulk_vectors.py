"""Shared batch behavior and exact value-free metric bytes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import sde


class Rollback(Exception):
    pass


def drive_bulk_vector(case: Path, model: Any, placement: Any, engines: Any) -> None:
    want = json.loads((case / "bulk.json").read_text())
    recorder = sde.Recorder(model.version)
    session = sde.Session(model, placement, engines, recorder=recorder)
    errors: list[str] = []

    def run(steps: list[dict[str, Any]]) -> None:
        for step in steps:
            try:
                if step["op"] == "transaction":
                    try:
                        with session.transaction("Reading"):
                            run(step["body"])
                            if step.get("rollback"):
                                raise Rollback
                    except Rollback:
                        pass
                else:
                    rows = step["rows"] * step.get("repeat", 1)
                    session.save_many(step.get("entity", "Reading"), rows)
            except sde.SdeError as exc:
                assert type(exc).__name__ == step.get("error")
                errors.append(type(exc).__name__)
            else:
                assert "error" not in step

    run(want["operations"])
    engine = next(iter(engines.values()))
    assert engine.recorded.calls == want["calls"]
    assert {name: db.tables for name, db in engines.items()} == want["tables"]
    assert errors == want["errors"]
    window = recorder.roll()
    metrics = {
        "shapes": []
        if window is None
        else [
            {
                key: getattr(stats, key)
                for key in ("shape_id", "entity", "group", "kind", "calls", "rows", "errors")
            }
            for stats in window.shapes
        ],
        "copies": []
        if window is None
        else [
            {key: getattr(stats, key) for key in ("group", "materialization", "writes", "failures")}
            for stats in window.fanned
        ],
    }
    assert metrics == want["metrics"]
    assert sde.canonical_bytes(metrics).hex() == want["metrics_hex"]

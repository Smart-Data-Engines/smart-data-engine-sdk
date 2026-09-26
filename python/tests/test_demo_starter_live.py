"""A real restricted customer application uses both SDK data engines and resets only itself."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from _weather_fixture import supplied

import sde
from sde_demo import project, resources, runtime
from sde_demo.diagnostics import doctor


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_local_weather_setup_run_doctor_retry_and_reset(
    source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = {
        dialect: os.environ.get(variable, "")
        for dialect, variable in (
            ("postgres", "SDE_POSTGRES_DSN"),
            ("clickhouse", "SDE_CLICKHOUSE_DSN"),
        )
    }
    if not all(admin.values()):
        pytest.skip("both native engines are required for the Weather starter")
    root = tmp_path / "weather"
    bundle, _ = supplied(source)
    try:
        assert project.setup(root, bundle, admin) == {"status": "ready", "map_version": 1}
        report = doctor(root)
        assert (
            report["signature"] == "verified"
            and report["production_qualification"] == "not_performed"
        )
        assert set(report["engines"]) == set(admin)
        for result in report["engines"].values():
            assert result["version"] and result["runtime_privileges"] == "qualified"
        # The size a window will carry is readable on the source's engine - on ClickHouse through
        # the system.parts column grant setup gives - and the other engine holds no source.
        assert {name: item["storage_telemetry"] for name, item in report["engines"].items()} == {
            name: "available" if name == source else "no_source" for name in admin
        }
        original_engine = runtime.engine

        def active_only(dialect: str, dsn: str) -> object:
            if dialect != source:
                raise sde.EngineError("unused engine is unavailable")
            return original_engine(dialect, dsn)

        monkeypatch.setattr(runtime, "engine", active_only)
        first = runtime.run(root, iterations=2, batch_size=3, interval_ms=0)
        second = runtime.run(root, iterations=1, batch_size=2, interval_ms=0)
        assert first["status"] == second["status"] == "complete"
        assert first["verified_rows"] == 6 and second["verified_rows"] == 2
        assert first["run_id"] != second["run_id"]
        window = project.read(root / "runs" / first["run_id"] / "window.json")
        encoded = json.dumps(window)
        assert "weather-" + first["run_id"] not in encoded
        assert "runtime-credentials" not in encoded
        assert first["model_version"] in encoded
        # Measured at the start and the end of the run: a size, the index share (the declared
        # layout has no secondary index) and how the writes arrived; a day is not projected from a
        # run of seconds. The time-filtered share is the calls whose filter named `at`, exactly as
        # the same window's `filtered_on` reports them.
        body = window["groups"]["WeatherReading"]
        assert body["total_bytes"] > 0 and body["index_to_table_ratio"] == 0.0
        assert body["write_burstiness"] >= 1.0
        timed = sum(
            entry["calls"]
            for shape in body["shapes"]
            for entry in shape.get("filtered_on", ())
            if entry.get("range") == "at" or "at" in entry["equal"]
        )
        assert body["time_filtered_share"] == timed / body["calls"]
        assert body["missing"] == ["daily_growth_bytes"]
        # Fleet analytics reads both earlier runs' rows in one time window, across stations, and
        # expects the exact sum; the window says each read bounded `at` and fixed no station.
        fleet = runtime.run(root, iterations=2, batch_size=4, interval_ms=0, workload="fleet")
        assert fleet["status"] == "complete" and fleet["verified_rows"] == 8
        shapes = project.read(root / "runs" / fleet["run_id"] / "window.json")["groups"][
            "WeatherReading"
        ]["shapes"]
        filters = {entry["kind"]: entry.get("filtered_on") for entry in shapes}
        assert filters["range_read"] == [{"equal": [], "range": "at", "calls": 2}]
        assert filters["aggregate"] == [{"equal": [], "range": "at", "calls": 4}]
        fleet_body = project.read(root / "runs" / fleet["run_id"] / "window.json")["groups"][
            "WeatherReading"
        ]
        assert fleet_body["time_filtered_share"] > 0.0  # every fleet read bounded `at`
        # Alerts read one station's readings at or above a humidity: the first iteration has no
        # alert yet (an empty page and a summary of nothing), the second has five.
        alerts = runtime.run(root, iterations=2, batch_size=40, interval_ms=0, workload="alerts")
        assert alerts["status"] == "complete" and alerts["verified_rows"] == 80
        shapes = project.read(root / "runs" / alerts["run_id"] / "window.json")["groups"][
            "WeatherReading"
        ]["shapes"]
        filters = {entry["kind"]: entry.get("filtered_on") for entry in shapes}
        assert filters["range_read"] == [{"equal": ["station"], "range": "humidity", "calls": 2}]
        assert filters["aggregate"] == [{"equal": ["station"], "range": "humidity", "calls": 4}]
        before = (root / "state" / "active-map.json").read_bytes()
        assert project.setup(root, bundle, admin) == {"status": "ready", "map_version": 1}
        assert (root / "state" / "active-map.json").read_bytes() == before
    finally:
        if (root / "resources.json").exists():
            result = resources.reset(root, admin)
            assert result["status"] == "reset"
            assert resources.reset(root, admin) == result


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_setup_completion_retry_never_runs_provisioning_again(
    source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = {
        dialect: os.environ.get(variable, "")
        for dialect, variable in (
            ("postgres", "SDE_POSTGRES_DSN"),
            ("clickhouse", "SDE_CLICKHOUSE_DSN"),
        )
    }
    if not all(admin.values()):
        pytest.skip("both native engines are required for the Weather starter")
    root = tmp_path / "weather"
    bundle, sign = supplied(source)
    try:
        project.setup(root, bundle, admin)
        current = dict(bundle["current_map"])
        current["map_version"] = 2
        # Only test the completed setup branch here; the full operator transition has its own
        # native/installed qualification and will supply this map in the customer demo.
        project.write(root / "state" / "active-map.json", sign(current))
        monkeypatch.setattr(
            sde, "prepare_schema", lambda *args, **kwargs: pytest.fail("reprovisioned")
        )
        monkeypatch.setattr(
            resources, "grant_tables", lambda *args, **kwargs: pytest.fail("regranted")
        )
        assert project.setup(root, bundle, admin) == {"status": "ready", "map_version": 2}
    finally:
        if (root / "resources.json").exists():
            resources.reset(root, admin)

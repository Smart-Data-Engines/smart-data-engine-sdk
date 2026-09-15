"""Completed-run verification is exact, bounded, source-only and independent of operator secrets."""

from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from test_demo_starter import local

import sde
from sde_demo import project, verification
from sde_demo.model import GENERATOR_ID, model, reading

FIRST = "1" * 32
SECOND = "2" * 32
SECRET = "private-unexpected-value-canary"


def completed(
    root: Path,
    identity: str = FIRST,
    *,
    rows: int = 3,
    language: str = "python",
    uncertain: int = 0,
) -> dict[str, Any]:
    settings = project.config(root)
    report = {
        "protocol": 2,
        "generator_id": GENERATOR_ID,
        "run_id": identity,
        "language": language,
        "project_id": settings["project_id"],
        "model_version": model().version,
        "status": "complete",
        "pending": None,
        "acknowledged_rows": rows - uncertain,
        "verified_after_uncertain_rows": uncertain,
        "verified_rows": rows,
    }
    project.write(root / "runs" / identity / "report.json", report)
    return report


class Source:
    """Return deterministic source pages; a stale copy deliberately has its own data."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> None:
        self.rows = deepcopy(rows)
        self.copy = deepcopy(rows)
        self.closed = False
        self.scans: list[dict[str, Any]] = []
        self.bindings: set[str] = set()
        self.after_scan = lambda: None
        self.on_close = lambda: None
        source = self

        class Client:
            def __enter__(self) -> Client:
                return self

            def __exit__(self, *args: Any) -> None:
                self.close()

            def close(self) -> None:
                source.closed = True
                source.on_close()

            def scan(self, entity: str, **options: Any) -> sde.ScanPage:
                assert entity == "WeatherReading"
                source.scans.append(options)
                selected = source.rows if options.get("fresh") else source.copy
                rows = sorted(
                    (row for row in selected if row["station"] == options["where"]["station"]),
                    key=lambda row: (row["station"], row["at"]),
                )
                after = options.get("after")
                if after is not None:
                    rows = [
                        row
                        for row in rows
                        if (row["station"], row["at"]) > (after["station"], after["at"])
                    ]
                page = rows[: options["limit"]]
                cursor = (
                    {key: page[-1][key] for key in ("station", "at")}
                    if len(rows) > options["limit"]
                    else None
                )
                source.after_scan()
                return sde.ScanPage(tuple(deepcopy(page)), cursor)

        def connect(_model: Any, _placement: Any, factories: Any, **options: Any) -> Client:
            self.bindings = set(factories)
            return Client()

        monkeypatch.setattr(sde.Session, "connect", connect)


def test_shared_completed_loader_is_local_immutable_and_keeps_exact_counts(tmp_path: Path) -> None:
    local(tmp_path)
    completed(tmp_path, FIRST, rows=3, uncertain=1)
    completed(tmp_path, SECOND, rows=4, language="typescript")
    loaded = verification.load_completed_runs(tmp_path, [SECOND, FIRST])
    assert [item.run_id for item in loaded] == [SECOND, FIRST]
    assert [item.language for item in loaded] == ["typescript", "python"]
    assert sum(item.expected_rows for item in loaded) == 7
    assert (
        loaded[0].report_sha256
        == hashlib.sha256((tmp_path / "runs" / SECOND / "report.json").read_bytes()).hexdigest()
    )
    with pytest.raises(FrozenInstanceError):
        loaded[0].expected_rows = 0  # type: ignore[misc]
    assert not (tmp_path / "operator-credentials.json").exists()


@pytest.mark.parametrize("language", ["python", "typescript"])
def test_verifies_all_pages_and_ignores_other_runs_without_operator_credentials(
    language: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local(tmp_path)
    completed(tmp_path, rows=1003, language=language, uncertain=3)
    source = Source(
        monkeypatch,
        [reading(FIRST, 0, index) for index in range(1, 1004)] + [reading(SECOND, 0, 1)],
    )
    before = {path: path.read_bytes() for path in tmp_path.rglob("*.json")}
    result = verification.verify_runs(tmp_path, [FIRST])
    assert result["status"] == "verified" and result["verified_rows"] == 1003
    assert result["runs"][0]["language"] == language
    assert source.bindings == {"postgres"}
    assert source.closed
    assert len(source.scans) == 2 and source.scans[1]["after"] is not None
    assert all(
        options["fresh"] is True and options.get("bounds") is None for options in source.scans
    )
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*.json")}
    encoded = json.dumps(result)
    assert reading(FIRST, 0, 1)["station"] not in encoded
    assert str(reading(FIRST, 0, 1)["id"]) not in encoded
    assert "runtime-secret-marker" not in encoded


@pytest.mark.parametrize(
    "change", ["missing", "wrong_value", "extra_early", "extra_late", "extra_page"]
)
def test_exact_source_mismatch_refuses_and_always_closes(
    change: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local(tmp_path)
    size = 1000 if change == "extra_page" else 3
    completed(tmp_path, rows=size)
    expected = [reading(FIRST, 0, index) for index in range(1, size + 1)]
    source = Source(monkeypatch, expected)
    if change == "missing":
        source.rows.pop()
    elif change == "wrong_value":
        source.rows[0]["celsius"] = SECRET
    elif change == "extra_early":
        source.rows.insert(0, reading(FIRST, 0, 0))
    else:
        source.rows.append(reading(FIRST, 0, size + 1))
    with pytest.raises(project.DemoRefused) as error:
        verification.verify_runs(tmp_path, [FIRST])
    assert source.closed
    assert SECRET not in str(error.value)
    # If fresh=True disappears, the unchanged stale copy would wrongly pass every case.
    assert all(call["fresh"] is True for call in source.scans)


@pytest.mark.parametrize("has_extra", [False, True])
def test_empty_completed_namespace_is_still_scanned(
    has_extra: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local(tmp_path)
    completed(tmp_path, rows=0)
    source = Source(monkeypatch, [reading(FIRST, 0, 1)] if has_extra else [])
    if has_extra:
        with pytest.raises(project.DemoRefused, match="additional"):
            verification.verify_runs(tmp_path, [FIRST])
    else:
        assert verification.verify_runs(tmp_path, [FIRST])["verified_rows"] == 0
    assert source.scans and source.closed


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("protocol", 1),
        ("protocol", 2.0),
        ("protocol", True),
        ("generator_id", None),
        ("generator_id", "unrecognized-generator"),
        ("run_id", SECOND),
        ("project_id", "other-project"),
        ("model_version", "different-model"),
        ("language", "unknown-language"),
        ("status", "running"),
        ("status", "incomplete"),
        ("pending", {"first": 1, "count": 3}),
        ("cleanup_failed", True),
        ("failure", "EngineError"),
        ("acknowledged_rows", True),
        ("verified_after_uncertain_rows", False),
        ("verified_rows", 3.0),
        ("acknowledged_rows", "3"),
        ("verified_rows", -1),
        ("verified_after_uncertain_rows", -1),
        ("acknowledged_rows", 2),
        ("verified_rows", None),
    ],
)
def test_invalid_or_incomplete_reports_refuse_before_opening_engines(
    field: str, value: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local(tmp_path)
    report = completed(tmp_path)
    report[field] = value
    project.write(tmp_path / "runs" / FIRST / "report.json", report)
    monkeypatch.setattr(
        sde.Session, "connect", lambda *args, **kwargs: pytest.fail("opened engines")
    )
    with pytest.raises(project.DemoRefused):
        verification.verify_runs(tmp_path, [FIRST])


@pytest.mark.parametrize("field", ["generator_id", "pending", "acknowledged_rows"])
def test_absent_required_descriptor_never_guesses_a_default(field: str, tmp_path: Path) -> None:
    local(tmp_path)
    report = completed(tmp_path)
    report.pop(field)
    project.write(tmp_path / "runs" / FIRST / "report.json", report)
    with pytest.raises(project.DemoRefused):
        verification.load_completed_runs(tmp_path, [FIRST])


@pytest.mark.parametrize(
    "identities", [[], FIRST, [FIRST, FIRST], ["../outside"], [str(i).zfill(32) for i in range(33)]]
)
def test_run_selection_is_bounded_and_does_not_silently_deduplicate(
    identities: Any, tmp_path: Path
) -> None:
    local(tmp_path)
    with pytest.raises(project.DemoRefused):
        verification.load_completed_runs(tmp_path, identities)


def test_row_limits_are_enforced_before_data_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local(tmp_path)
    monkeypatch.setattr(
        sde.Session, "connect", lambda *args, **kwargs: pytest.fail("opened engines")
    )
    completed(tmp_path, rows=10001)
    with pytest.raises(project.DemoRefused, match="10000 expected rows per run"):
        verification.verify_runs(tmp_path, [FIRST])
    identities = [format(index, "032x") for index in range(11)]
    for identity in identities:
        completed(tmp_path, identity, rows=10000)
    assert (
        sum(
            item.expected_rows
            for item in verification.load_completed_runs(tmp_path, identities[:10])
        )
        == 100000
    )
    with pytest.raises(project.DemoRefused, match="100000 expected rows per invocation"):
        verification.verify_runs(tmp_path, identities)


def test_map_change_after_scan_or_during_cleanup_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, sign = local(tmp_path)
    completed(tmp_path)
    source = Source(monkeypatch, [reading(FIRST, 0, index) for index in range(1, 4)])
    changed = deepcopy(bundle["current_map"])
    changed["map_version"] = 2
    source.on_close = lambda: project.write(tmp_path / "state" / "active-map.json", sign(changed))
    with pytest.raises(project.DemoRefused, match="active map changed"):
        verification.verify_runs(tmp_path, [FIRST])
    assert source.closed


def test_equivalent_map_formatting_is_not_a_changed_instruction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, _ = local(tmp_path)
    completed(tmp_path)
    source = Source(monkeypatch, [reading(FIRST, 0, index) for index in range(1, 4)])
    source.after_scan = lambda: (tmp_path / "state" / "active-map.json").write_text(
        json.dumps(bundle["current_map"], indent=3) + "\n"
    )
    assert verification.verify_runs(tmp_path, [FIRST])["status"] == "verified"
    assert source.closed


def test_report_change_during_verification_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local(tmp_path)
    report = completed(tmp_path)
    source = Source(monkeypatch, [reading(FIRST, 0, index) for index in range(1, 4)])
    source.after_scan = lambda: project.write(
        tmp_path / "runs" / FIRST / "report.json", {**report, "status": "incomplete"}
    )
    with pytest.raises(project.DemoRefused, match="report changed"):
        verification.verify_runs(tmp_path, [FIRST])
    assert source.closed


def test_read_and_close_errors_are_sanitized_and_cannot_report_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local(tmp_path)
    completed(tmp_path)
    source = Source(monkeypatch, [reading(FIRST, 0, index) for index in range(1, 4)])

    def fail() -> None:
        raise sde.EngineError(SECRET + " runtime-secret-marker")

    source.after_scan = fail
    source.on_close = fail
    with pytest.raises(project.DemoRefused) as error:
        verification.verify_runs(tmp_path, [FIRST])
    assert source.closed
    assert SECRET not in str(error.value) and "runtime-secret-marker" not in str(error.value)


def test_partial_owned_startup_closes_every_created_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, sign = local(tmp_path)
    completed(tmp_path)
    changed = deepcopy(bundle["current_map"])
    group = changed["groups"]["WeatherReading"]
    layout = sde.default_layout(model(), sde.colocation_groups(model())[0], dialect="clickhouse")
    group["derived"] = [
        {
            "id": "copy",
            "engine": "clickhouse",
            "lag_budget_ms": 30000,
            "layout": {
                "tables": dict(layout.tables),
                "columns": {name: dict(columns) for name, columns in layout.columns.items()},
            },
        }
    ]
    group["also_write"] = ["copy"]
    changed["map_version"] = 2
    project.write(tmp_path / "state" / "active-map.json", sign(changed))
    opened: list[str] = []
    closed: list[str] = []

    class Adapter:
        def __init__(self, dialect: str) -> None:
            self.dialect = dialect

        def connect(self) -> None:
            opened.append(self.dialect)
            if self.dialect == "postgres":
                raise sde.EngineError(SECRET)

        def close(self) -> None:
            closed.append(self.dialect)

    monkeypatch.setattr(verification, "engine", lambda dialect, dsn: Adapter(dialect))
    with pytest.raises(project.DemoRefused) as error:
        verification.verify_runs(tmp_path, [FIRST])
    assert opened == ["clickhouse", "postgres"]
    assert closed == ["postgres", "clickhouse"]
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_native_verifies_prior_runs_and_refuses_an_extra_source_row(
    dialect: str, tmp_path: Path
) -> None:
    from _weather_fixture import supplied

    from sde_demo import resources, runtime

    admin = {
        name: os.environ.get(variable, "")
        for name, variable in (
            ("postgres", "SDE_POSTGRES_DSN"),
            ("clickhouse", "SDE_CLICKHOUSE_DSN"),
        )
    }
    if not all(admin.values()):
        pytest.skip("both local engines are required for native Weather verification")
    root = tmp_path / "native-weather"
    bundle, _ = supplied(dialect)
    try:
        project.setup(root, bundle, admin)
        first = runtime.run(root, iterations=2, batch_size=3, interval_ms=0)
        second = runtime.run(root, iterations=1, batch_size=2, interval_ms=0)
        # Absence of the operator secret must not stop read-only runtime verification.
        operator_path = root / "operator-credentials.json"
        held_path = root / "held-operator-credentials.json"
        operator_path.rename(held_path)
        try:
            result = verification.verify_runs(root, [first["run_id"], second["run_id"]])
        finally:
            held_path.rename(operator_path)
        assert result["status"] == "verified" and result["verified_rows"] == 8
        settings = project.config(root)
        logical = model()
        placement = sde.load_local_map(
            root / "state",
            model=logical,
            project_id=settings["project_id"],
            public_key=project.public_keys(settings["public_keys"]),
        )
        dsns = project.credentials(root, "runtime", settings["engines"])
        name = placement.groups["WeatherReading"].source.engine
        with sde.Session.connect(
            logical,
            placement,
            {name: lambda: project.engine(dialect, dsns[name])},
            project_id=settings["project_id"],
        ) as client:
            client.save("WeatherReading", reading(first["run_id"], 0, 7))
        with pytest.raises(project.DemoRefused, match="additional"):
            verification.verify_runs(root, [first["run_id"]])
        assert verification.verify_runs(root, [second["run_id"]])["verified_rows"] == 2
    finally:
        if (root / "resources.json").exists():
            resources.reset(root, admin)

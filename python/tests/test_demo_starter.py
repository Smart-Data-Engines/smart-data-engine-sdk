"""Bootstrap trust, uncertain-write handling and session lifetime of the public starter."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

import pytest
from _weather_fixture import supplied

import sde
from sde_demo import project, runtime
from sde_demo.model import model, reading


def local(root: Path) -> tuple[dict[str, Any], Any]:
    bundle, sign = supplied()
    config, _ = project.bootstrap(bundle)
    project.write(root / "bootstrap.json", bundle)
    project.write(root / "config.json", config)
    project.write(
        root / "setup-complete.json", {"protocol": 1, "config_digest": sde.digest16(config)}
    )
    project.write(root / "state" / "active-map.json", bundle["current_map"])
    project.write(
        root / "runtime-credentials.json",
        {name: "runtime-secret-marker" for name in config["engines"]},
    )
    digest = hashlib.sha256((root / "runtime-credentials.json").read_bytes()).hexdigest()
    project.write(
        root / "resources.json", {"status": "ready", "credential_hashes": {"runtime": digest}}
    )
    # No operator credential file: running the application must not need it.
    return bundle, sign


@pytest.mark.parametrize("change", ["signature", "project", "version", "model", "binding", "extra"])
def test_bootstrap_refuses_changed_input_before_allocation(
    change: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, sign = supplied()
    if change == "signature":
        bundle["current_map"].pop("signature")
    elif change == "project":
        bundle["project_id"] = "f" * 32
    elif change == "version":
        bundle["current_map"]["map_version"] = 2
        bundle["current_map"] = sign(bundle["current_map"])
    elif change == "model":
        bundle["model"]["entities"][0]["residency"] = "US"
    elif change == "binding":
        bundle["engines"] = {"wrong": "postgres"}
    else:
        bundle["secret"] = "not a bootstrap field"
    from sde_demo import resources

    monkeypatch.setattr(
        resources, "allocate", lambda *args: pytest.fail("allocation preceded trust validation")
    )
    with pytest.raises((project.DemoRefused, sde.SdeError)):
        project.setup(tmp_path / "refused", bundle, {})
    assert not (tmp_path / "refused").exists()


def test_generator_is_exact_under_an_unrelated_decimal_context() -> None:
    expected = reading("a" * 32, 0, 999)
    with localcontext() as context:
        context.prec = 2
        assert reading("a" * 32, 0, 999) == expected
    assert expected["celsius"] == Decimal("25.24")
    assert reading("a" * 32, 0, 999) != reading("b" * 32, 0, 999)


@pytest.mark.parametrize("change", ["hash", "mode", "status", "missing_hash"])
def test_runtime_credentials_require_the_ready_pinned_file(tmp_path: Path, change: str) -> None:
    local(tmp_path)
    path = tmp_path / "runtime-credentials.json"
    if change == "hash":
        project.write(path, {name: "changed-secret" for name in ("postgres", "clickhouse")})
    elif change == "mode":
        path.chmod(0o644)
    else:
        metadata = project.read(tmp_path / "resources.json")
        if change == "status":
            metadata["status"] = "resetting"
        else:
            metadata.pop("credential_hashes")
        project.write(tmp_path / "resources.json", metadata)
    with pytest.raises(project.DemoRefused):
        project.credentials(tmp_path, "runtime", {"postgres": {}, "clickhouse": {}})


class Workload:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, mode: str = "ok") -> None:
        self.rows: list[dict[str, Any]] = []
        self.copy_rows: list[dict[str, Any]] = []
        self.sessions: list[Any] = []
        self.saves = 0
        self.bindings: list[set[str]] = []
        self.mode = mode
        self.after_save = lambda: None
        harness = self

        class Client:
            def __init__(self) -> None:
                self.closed = False
                harness.sessions.append(self)

            def close(self) -> None:
                self.closed = True

            def save_many(self, entity: str, rows: list[dict[str, Any]]) -> None:
                harness.saves += 1
                if harness.mode == "copy_only":
                    harness.copy_rows.extend(rows)
                elif harness.mode != "absent":
                    harness.rows.extend(rows[:1] if harness.mode == "partial" else rows)
                harness.after_save()
                if harness.mode != "ok":
                    raise sde.EngineError("driver could echo runtime-secret-marker and row values")

            def get(
                self, entity: str, key: dict[str, Any], **options: Any
            ) -> dict[str, Any] | None:
                visible = harness.rows if options.get("fresh") else harness.rows + harness.copy_rows
                return next(
                    (row for row in visible if all(row[k] == v for k, v in key.items())), None
                )

            def scan(self, entity: str, **options: Any) -> sde.ScanPage:
                return sde.ScanPage(
                    tuple((harness.rows + harness.copy_rows)[: options["limit"]]), None
                )

            def count(self, entity: str, **options: Any) -> int:
                return len(harness.rows + harness.copy_rows)

            def summarize(self, entity: str, field: str, **options: Any) -> sde.NumericSummary:
                visible = harness.rows + harness.copy_rows
                total = sum((row[field] for row in visible), Decimal(0))
                return sde.NumericSummary(len(visible), len(visible), None, None, total, None)

        def connect(_model: Any, _placement: Any, factories: Any, **options: Any) -> Client:
            harness.bindings.append(set(factories))
            return Client()

        monkeypatch.setattr(sde.Session, "connect", connect)


@pytest.mark.parametrize("mode", ["absent", "partial", "copy_only"])
def test_uncertain_batch_is_never_replayed_or_reported_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    local(tmp_path)
    fake = Workload(monkeypatch, mode)
    with pytest.raises(project.DemoRefused, match="uncertain"):
        runtime.run(tmp_path, iterations=1, batch_size=2, recovery_ms=0)
    report = project.read(next((tmp_path / "runs").glob("*/report.json")))
    assert fake.saves == 1 and len(fake.rows) == (1 if mode == "partial" else 0)
    assert report["status"] == "incomplete" and report["pending"] == {"first": 1, "count": 2}
    assert report["acknowledged_rows"] == report["verified_rows"] == 0
    assert all(client.closed for client in fake.sessions)
    assert "runtime-secret-marker" not in json.dumps(report)


def test_exact_visible_batch_resolves_lost_ack_without_second_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local(tmp_path)
    fake = Workload(monkeypatch, "visible")
    report = runtime.run(tmp_path, iterations=1, batch_size=2, recovery_ms=0)
    assert fake.saves == 1 and len(fake.rows) == 2
    assert report["status"] == "complete" and report["pending"] is None
    assert report["acknowledged_rows"] == 0 and report["verified_after_uncertain_rows"] == 2
    assert all(client.closed for client in fake.sessions)


def test_map_refresh_closes_the_previous_owned_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, sign = local(tmp_path)
    fake = Workload(monkeypatch)

    def advance() -> None:
        raw = deepcopy(bundle["current_map"])
        raw["map_version"] = 2
        project.write(tmp_path / "state" / "active-map.json", sign(raw))

    fake.after_save = advance
    report = runtime.run(tmp_path, iterations=2, batch_size=1, interval_ms=0)
    assert report["map_versions"] == [1, 2] and report["status"] == "complete"
    assert len(fake.sessions) == 2 and all(client.closed for client in fake.sessions)


def test_reset_marker_stops_a_runtime_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local(tmp_path)
    fake = Workload(monkeypatch)
    project.write(tmp_path / "reset-request.json", {"allocation_id": "demo"})
    with pytest.raises(project.DemoRefused, match="Reset"):
        runtime.run(tmp_path)
    assert fake.saves == 0 and not fake.sessions


def test_shared_weather_model_and_generator_fixture() -> None:
    root = Path(__file__).resolve().parents[2] / "examples" / "weather"
    fixture = json.loads((root / "generator.json").read_bytes())
    assert json.loads((root / "model.json").read_bytes()) == sde.neutral_declaration(model())
    assert fixture["model_version"] == model().version
    row = reading(fixture["run_id"], fixture["worker"], fixture["sequence"])
    encoded = {key: str(value) for key, value in row.items()}
    encoded["at"] = row["at"].isoformat(timespec="microseconds").replace("+00:00", "Z")
    assert encoded == fixture["reading"]


def test_runtime_does_not_open_unused_engine_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local(tmp_path)
    fake = Workload(monkeypatch)
    assert runtime.run(tmp_path, iterations=1, batch_size=1)["status"] == "complete"
    assert fake.bindings == [{"postgres"}]


def test_operator_status_does_not_open_engines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sde._cutover_project import ProjectState
    from sde_demo import resources
    from sde_demo.__main__ import operator

    bundle, _ = local(tmp_path)
    from sde._local_state import write_bytes

    write_bytes(tmp_path / "state" / "active-map.json", sde.canonical_bytes(bundle["current_map"]))
    ProjectState(tmp_path / "state", bundle["project_id"], model().version).enroll(
        bundle["current_map"], sde.canonical_bytes(bundle["current_map"])
    )
    monkeypatch.setattr(resources, "verify", lambda *args: pytest.fail("status contacted engines"))
    project.write(tmp_path / "reset-request.json", {"allocation_id": "test"})
    assert operator(tmp_path, "status", None)["active_map_version"] == 1


def test_operator_checks_reset_after_acquiring_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    from sde_demo import __main__ as cli

    local(tmp_path)

    @contextmanager
    def reset_won(root: Path) -> Any:
        project.write(root / "reset-request.json", {"allocation_id": "test"})
        yield

    monkeypatch.setattr(cli, "transaction", reset_won)
    with pytest.raises(project.DemoRefused, match="Reset"):
        cli.operator(tmp_path, "resume", None)


def test_completed_setup_reconfirms_local_state_durability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sde import _local_state
    from sde._cutover_project import ProjectState
    from sde_demo import resources

    bundle, _ = local(tmp_path)
    _local_state.write_bytes(
        tmp_path / "state" / "active-map.json", sde.canonical_bytes(bundle["current_map"])
    )
    store = ProjectState(tmp_path / "state", bundle["project_id"], model().version)
    store.enroll(bundle["current_map"], sde.canonical_bytes(bundle["current_map"]))
    monkeypatch.setattr(resources, "allocate", lambda *args: {})
    before = {
        path: (path.read_bytes(), path.stat().st_ino) for path in (store.path, store.map_path)
    }
    assert project.setup(tmp_path, bundle, {}) == {"status": "ready", "map_version": 1}
    sync = _local_state.sync_directory

    def fail_state(directory: Path) -> None:
        if directory == tmp_path / "state":
            raise OSError("controlled state fsync failure")
        sync(directory)

    with monkeypatch.context() as changed:
        changed.setattr(_local_state, "sync_directory", fail_state)
        with pytest.raises(OSError, match="fsync"):
            project.setup(tmp_path, bundle, {})
    assert {path: (path.read_bytes(), path.stat().st_ino) for path in before} == before
    assert project.setup(tmp_path, bundle, {}) == {"status": "ready", "map_version": 1}

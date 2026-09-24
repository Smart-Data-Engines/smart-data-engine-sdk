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


@pytest.mark.parametrize("action", ["index", "abandon"])
def test_operator_passes_index_builds_and_abandonment_to_the_local_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    from contextlib import contextmanager
    from types import SimpleNamespace

    from sde_demo import __main__ as cli
    from sde_demo import resources

    local(tmp_path)
    calls: list[tuple[str, ...]] = []

    class Executor:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def index(self, plan: Any) -> Any:
            calls.append(("index", plan.index_id))
            return SimpleNamespace(as_record=lambda: {"outcome": "built"})

        def abandon(self) -> Any:
            calls.append(("abandon",))
            return SimpleNamespace(as_record=lambda: {"outcome": "abandoned"})

    @contextmanager
    def no_connections(_root: Path, _settings: Any) -> Any:
        yield {}, {}

    monkeypatch.setattr(resources, "verify", lambda _root: None)
    monkeypatch.setattr(cli, "connections", no_connections)
    monkeypatch.setattr(cli.sde, "LocalCutover", Executor)
    if action == "abandon":
        assert cli.operator(tmp_path, "abandon", None) == {"outcome": "abandoned"}
        assert calls == [("abandon",)]
        return
    with pytest.raises(project.DemoRefused, match="require --plan"):
        cli.operator(tmp_path, "index", None)
    monkeypatch.setattr(
        cli.sde, "load_index_plan", lambda raw, **_: SimpleNamespace(index_id=raw["index_id"])
    )
    packet = tmp_path / "index.json"
    packet.write_text(json.dumps({"index_id": "a" * 32}))
    assert cli.operator(tmp_path, "index", packet) == {"outcome": "built"}
    assert calls == [("index", "a" * 32)]


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


def test_generator_identity_and_supported_sequence_domain_are_frozen() -> None:
    from sde_demo.model import GENERATOR_ID, GENERATOR_SPEC

    fixture = json.loads(
        (Path(__file__).resolve().parents[2] / "examples/weather/generator.json").read_bytes()
    )
    assert fixture["generator_id"] == GENERATOR_ID
    assert fixture["generator_spec"] == GENERATOR_SPEC
    domain = fixture["domain"]
    digest = hashlib.sha256()
    for identity in domain["run_ids"]:
        for sequence in range(domain["first_sequence"], domain["last_sequence"] + 1):
            row = reading(identity, domain["worker"], sequence)
            encoded = {key: str(value) for key, value in row.items()}
            encoded["at"] = row["at"].isoformat(timespec="microseconds").replace("+00:00", "Z")
            digest.update(sde.canonical_bytes(encoded))
    assert digest.hexdigest() == domain["sha256"]


def test_a_designed_initial_map_bootstraps_the_starter() -> None:
    """An AI-designed first map is placement map contract 5; the starter must take it.

    It refused anything but contract 4 until the physical design arrived, so the Weather demo could
    not start from the one kind of map the product now issues by default for a design.
    """
    bundle, sign = supplied()
    current = deepcopy(bundle["current_map"])
    current["contract"] = 5
    (group,) = current["groups"].values()
    entity = next(iter(group["source"]["layout"]["tables"]))
    group["source"]["layout"]["key_order"] = {entity: ["at", "station"]}
    bundle["current_map"] = sign(current)
    config, _ = project.bootstrap(bundle)
    assert config["project_id"] == bundle["project_id"]


FLEET_RUNS = {"a" * 32: 7, "0" * 32: 3, "c" * 31 + "1": 12}


def test_fleet_expectation_is_every_run_row_in_the_window() -> None:
    """Checked against brute force: every row of every run, filtered to the window, in key order."""
    through, limit = 5, 4
    rows = [
        reading(identity, 0, sequence)
        for identity, written in FLEET_RUNS.items()
        for sequence in range(1, written + 1)
        if sequence <= through
    ]
    rows.sort(key=lambda row: (row["station"], row["at"]))
    page, total, celsius = runtime.fleet_expected(FLEET_RUNS, through, limit)
    assert page == rows[:limit]
    assert total == len(rows) == 3 + 5 + 5
    assert celsius == sum((row["celsius"] for row in rows), Decimal(0))
    assert runtime.fleet_expected(FLEET_RUNS, through, 100)[0] == rows


def _report(root: Path, identity: str, **fields: Any) -> None:
    report = {
        "protocol": 2,
        "run_id": identity,
        "project_id": "p" * 32,
        "status": "complete",
        "pending": None,
        "generator_id": runtime.GENERATOR_ID,
        "verified_rows": 4,
        **fields,
    }
    project.write(root / "runs" / identity / "report.json", report)


def test_fleet_reads_every_completed_run_of_this_project_and_only_those(tmp_path: Path) -> None:
    _report(tmp_path, "a" * 32)
    _report(tmp_path, "b" * 32, verified_rows=9)
    _report(tmp_path, "c" * 32, project_id="q" * 32, status="running")  # another project: skipped
    assert runtime.fleet_runs(tmp_path, project_id="p" * 32) == {"a" * 32: 4, "b" * 32: 9}
    assert runtime.fleet_runs(tmp_path / "empty", project_id="p" * 32) == {}


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "running"},
        {"status": "incomplete"},
        {"pending": {"first": 1, "count": 2}},
        {"generator_id": "weather-v0:other"},
        {"verified_rows": True},
        {"verified_rows": -1},
        {"verified_rows": 10001},
        {"run_id": "b" * 32},
    ],
    ids=["running", "incomplete", "pending", "generator", "bool", "negative", "too-many", "moved"],
)
def test_fleet_refuses_when_an_earlier_run_of_this_project_is_not_complete(
    tmp_path: Path, fields: dict[str, Any]
) -> None:
    _report(tmp_path, "a" * 32, **fields)
    with pytest.raises(project.DemoRefused, match="every earlier run"):
        runtime.fleet_runs(tmp_path, project_id="p" * 32)


def test_fleet_refuses_a_report_without_its_pending_field(tmp_path: Path) -> None:
    """Absent is not settled: a report that does not say its batch resolved did not say so."""
    _report(tmp_path, "a" * 32)
    path = tmp_path / "runs" / ("a" * 32) / "report.json"
    report = project.read(path)
    del report["pending"]
    project.write(path, report)
    with pytest.raises(project.DemoRefused, match="every earlier run"):
        runtime.fleet_runs(tmp_path, project_id="p" * 32)


def test_the_cli_and_the_runtime_offer_the_same_workloads(tmp_path: Path) -> None:
    from sde_demo import __main__ as cli

    assert cli.WORKLOADS is runtime.WORKLOADS  # one tuple, imported by both
    # The parser accepts every workload the runtime runs (the missing setup refuses afterwards,
    # which returns a status) and rejects anything else before the runtime is reached.
    assert isinstance(cli.main(["--directory", str(tmp_path), "run", "--workload", "fleet"]), int)
    with pytest.raises(SystemExit):
        cli.main(["--directory", str(tmp_path), "run", "--workload", "bogus"])
    with pytest.raises(project.DemoRefused, match="fleet"):
        runtime.run(tmp_path, workload="bogus")

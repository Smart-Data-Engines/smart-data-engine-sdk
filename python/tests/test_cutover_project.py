"""Local executor state is atomic, checked and isolated by project and model."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sde import MigrationRefused
from sde import _local_state as storage
from sde._cutover_project import ProjectState, encode

PROJECT = "1" * 32
MODEL = "2" * 16


def test_conflicting_active_file_is_refused_before_state_is_created(tmp_path: Path) -> None:
    (tmp_path / "active-map.json").write_bytes(b"existing-map")
    store = ProjectState(tmp_path, PROJECT, MODEL)
    with pytest.raises(MigrationRefused, match="existing active map"):
        store.enroll({"map_version": 1}, b"different-map")
    assert not store.path.exists()
    assert store.map_path.read_bytes() == b"existing-map"


def test_state_checksum_refuses_changed_decision(tmp_path: Path) -> None:
    store = ProjectState(tmp_path, PROJECT, MODEL)
    store.enroll({"map_version": 1}, b"map")
    envelope = json.loads(store.path.read_bytes())
    envelope["payload"]["execution"] = {"decision": "success"}
    store.path.write_bytes(encode(envelope))
    with pytest.raises(MigrationRefused, match="corrupt"):
        store.read()


def test_a_valid_file_from_another_project_is_refused(tmp_path: Path) -> None:
    owner = ProjectState(tmp_path, PROJECT, MODEL)
    owner.enroll({"map_version": 1}, b"map")
    with pytest.raises(MigrationRefused, match="corrupt"):
        ProjectState(tmp_path, "9" * 32, MODEL).read()


def test_failed_write_preserves_the_previous_complete_state(
    tmp_path: Path, monkeypatch: Any
) -> None:
    store = ProjectState(tmp_path, PROJECT, MODEL)
    store.enroll({"map_version": 1}, b"map")
    original = store.path.read_bytes()
    state = store.read()
    state["execution"] = {"phase": "prepared"}

    def failed(_fd: int, _body: bytes) -> None:
        raise OSError("controlled full disk")

    monkeypatch.setattr(storage, "_write_all", failed)
    with pytest.raises(OSError, match="full disk"):
        store.write(state)
    assert store.path.read_bytes() == original
    assert store.read()["execution"] is None


def test_post_publication_sync_failure_is_uncertain_not_rolled_back(
    tmp_path: Path, monkeypatch: Any
) -> None:
    store = ProjectState(tmp_path, PROJECT, MODEL)
    store.enroll({"map_version": 1}, b"map")
    state = store.read()
    state["execution"] = {"phase": "prepared"}

    def failed(_directory: Path) -> None:
        raise OSError("controlled directory sync failure")

    monkeypatch.setattr(storage, "sync_directory", failed)
    with pytest.raises(storage.DurabilityUncertain):
        store.write(state)
    assert store.read()["execution"] == {"phase": "prepared"}


def test_native_names_are_not_rewritten_by_state_storage(tmp_path: Path) -> None:
    store = ProjectState(tmp_path, PROJECT, MODEL)
    store.enroll({"map_version": 1}, b"map")
    state = store.read()
    state["execution"] = {"principal": "cafe\u0301"}
    store.write(state)
    assert store.read()["execution"]["principal"] == "cafe\u0301"


def test_file_sync_failure_does_not_publish_unconfirmed_state(
    tmp_path: Path, monkeypatch: Any
) -> None:
    store = ProjectState(tmp_path, PROJECT, MODEL)
    store.enroll({"map_version": 1}, b"map")
    before = store.path.read_bytes()
    state = store.read()
    state["execution"] = {"decision": "success"}

    def failed(_fd: int) -> None:
        raise OSError("controlled file sync failure")

    monkeypatch.setattr(storage.os, "fsync", failed)
    with pytest.raises(OSError, match="file sync"):
        store.write(state)
    assert store.path.read_bytes() == before
    assert store.read()["execution"] is None


def _rewrite(store: ProjectState, contract: int, payload: dict[str, Any]) -> None:
    """A well-formed envelope with a valid checksum: only the contract rule can refuse it."""
    import hashlib

    envelope = {
        "storage_contract": contract,
        "payload": payload,
        "sha256": hashlib.sha256(encode(payload)).hexdigest(),
    }
    store.path.write_bytes(encode(envelope))


def test_an_index_build_history_raises_the_state_contract_to_three(tmp_path: Path) -> None:
    store = ProjectState(tmp_path, PROJECT, MODEL)
    store.enroll({"map_version": 1}, b"map")
    state = store.read()
    state["stages"], state["indexes"] = {}, {"a" * 32: {"receipt": {"outcome": "built"}}}
    store.write(state)
    assert json.loads(store.path.read_bytes())["storage_contract"] == 3
    assert store.read()["indexes"] == {"a" * 32: {"receipt": {"outcome": "built"}}}


@pytest.mark.parametrize(
    ("contract", "drop", "history"),
    [
        (3, "indexes", None),  # contract 3 without its history
        (3, None, []),  # a history that is not an object
        (2, None, {}),  # an index history under a contract that does not know it
        (1, None, {}),
    ],
)
def test_the_index_build_history_belongs_to_contract_three_only(
    tmp_path: Path, contract: int, drop: str | None, history: Any
) -> None:
    store = ProjectState(tmp_path, PROJECT, MODEL)
    store.enroll({"map_version": 1}, b"map")
    payload = store.read()
    payload["stages"], payload["indexes"] = {}, history
    if drop is not None:
        del payload[drop]
    if contract == 1:
        del payload["stages"]
    _rewrite(store, contract, payload)
    with pytest.raises(MigrationRefused, match="corrupt"):
        store.read()


def _stage_record(outcome: str) -> dict[str, Any]:
    return {"plan_fingerprint": "f" * 64, "plan": {}, "receipt": {"outcome": outcome}}


def test_an_abandoned_staging_raises_the_state_contract_to_four(tmp_path: Path) -> None:
    store = ProjectState(tmp_path, PROJECT, MODEL)
    store.enroll({"map_version": 1}, b"map")
    state = store.read()
    state["stages"] = {"a" * 32: _stage_record("prepared")}
    state["indexes"] = {}
    store.write(state)
    assert json.loads(store.path.read_bytes())["storage_contract"] == 3
    state["stages"]["b" * 32] = _stage_record("abandoned")
    store.write(state)
    assert json.loads(store.path.read_bytes())["storage_contract"] == 4
    assert store.read()["stages"]["b" * 32]["receipt"]["outcome"] == "abandoned"


@pytest.mark.parametrize("contract", [2, 3])
def test_an_abandoned_staging_record_needs_contract_four(tmp_path: Path, contract: int) -> None:
    store = ProjectState(tmp_path, PROJECT, MODEL)
    store.enroll({"map_version": 1}, b"map")
    payload = store.read()
    payload["stages"] = {"b" * 32: _stage_record("abandoned")}
    if contract == 3:
        payload["indexes"] = {}
    _rewrite(store, contract, payload)
    with pytest.raises(MigrationRefused, match="corrupt"):
        store.read()


def test_contract_four_still_carries_the_index_history(tmp_path: Path) -> None:
    store = ProjectState(tmp_path, PROJECT, MODEL)
    store.enroll({"map_version": 1}, b"map")
    payload = store.read()
    payload["stages"] = {"b" * 32: _stage_record("abandoned")}
    _rewrite(store, 4, payload)  # no "indexes" field
    with pytest.raises(MigrationRefused, match="corrupt"):
        store.read()


def _index_record(protocol: int) -> dict[str, Any]:
    return {"plan_fingerprint": "f" * 64, "plan": {"protocol": protocol}, "receipt": {}}


def test_an_index_change_raises_the_state_contract_to_five(tmp_path: Path) -> None:
    store = ProjectState(tmp_path, PROJECT, MODEL)
    store.enroll({"map_version": 1}, b"map")
    state = store.read()
    state["stages"], state["indexes"] = {}, {"a" * 32: _index_record(1)}
    store.write(state)
    assert json.loads(store.path.read_bytes())["storage_contract"] == 3
    state["indexes"]["b" * 32] = _index_record(2)
    store.write(state)
    assert json.loads(store.path.read_bytes())["storage_contract"] == 5
    assert store.read()["indexes"]["b" * 32]["plan"]["protocol"] == 2
    # An executing change is one too, before it has any history.
    state["indexes"], state["execution"] = {}, {"kind": "index", "plan": {"protocol": 2}}
    store.write(state)
    assert json.loads(store.path.read_bytes())["storage_contract"] == 5


@pytest.mark.parametrize("where", ["history", "execution"])
@pytest.mark.parametrize("contract", [3, 4])
def test_an_index_change_needs_contract_five(tmp_path: Path, contract: int, where: str) -> None:
    """What makes an operator that knows contracts 1 to 4 refuse, not resume, an index change."""
    store = ProjectState(tmp_path, PROJECT, MODEL)
    store.enroll({"map_version": 1}, b"map")
    payload = store.read()
    payload["stages"], payload["indexes"] = {}, {}
    if where == "history":
        payload["indexes"]["b" * 32] = _index_record(2)
    else:
        payload["execution"] = {"kind": "index", "plan": {"protocol": 2}}
    _rewrite(store, contract, payload)
    with pytest.raises(MigrationRefused, match="corrupt"):
        store.read()
    _rewrite(store, 5, payload)
    assert store.read()["stages"] == {}

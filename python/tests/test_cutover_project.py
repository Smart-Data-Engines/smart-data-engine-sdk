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

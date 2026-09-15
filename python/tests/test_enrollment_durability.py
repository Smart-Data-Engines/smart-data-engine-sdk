"""Enrollment retries confirm existing native-free state before claiming completion."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sde import _local_state as storage
from sde._cutover_project import ProjectState, encode

PROJECT = "1" * 32
MODEL = "2" * 16
DOCUMENT = {"contract": 4, "project_id": PROJECT, "model_version": MODEL, "map_version": 1}
MAP_BYTES = encode(DOCUMENT)


def snapshot(store: ProjectState) -> dict[str, tuple[bytes, int]]:
    return {
        path.name: (path.read_bytes(), path.stat().st_ino) for path in (store.path, store.map_path)
    }


def test_enrollment_retry_does_not_acknowledge_unconfirmed_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProjectState(tmp_path / "state", PROJECT, MODEL)
    real_sync = storage.sync_directory
    failed_syncs = 0

    def cannot_sync_published_map(directory: Path) -> None:
        nonlocal failed_syncs
        if directory == store.root and store.map_path.exists():
            failed_syncs += 1
            raise OSError("controlled enrollment directory sync failure")
        real_sync(directory)

    with monkeypatch.context() as patch:
        patch.setattr(storage, "sync_directory", cannot_sync_published_map)
        with pytest.raises(storage.DurabilityUncertain):
            store.enroll(DOCUMENT, MAP_BYTES)
        assert failed_syncs == 1
        before = snapshot(store)
        retry = ProjectState(store.root, PROJECT, MODEL)
        with pytest.raises(OSError, match="enrollment directory sync failure"):
            retry.enroll(DOCUMENT, MAP_BYTES)
        assert failed_syncs == 2
        assert snapshot(retry) == before
    retry.enroll(DOCUMENT, MAP_BYTES)
    assert snapshot(retry) == before
    assert retry.read()["active_map"] == DOCUMENT


@pytest.mark.parametrize("filename", ["project.json", "active-map.json"])
def test_identical_enrollment_retry_requires_both_existing_files_to_be_synced(
    filename: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProjectState(tmp_path / "state", PROJECT, MODEL)
    store.enroll(DOCUMENT, MAP_BYTES)
    before = snapshot(store)
    selected = (store.root / filename).stat()
    real_fsync = storage.os.fsync
    failed_syncs = 0

    def cannot_sync_selected_file(fd: int) -> None:
        nonlocal failed_syncs
        actual = os.fstat(fd)
        if (actual.st_dev, actual.st_ino) == (selected.st_dev, selected.st_ino):
            failed_syncs += 1
            raise OSError("controlled enrollment file sync failure")
        real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(storage.os, "fsync", cannot_sync_selected_file)
        with pytest.raises(OSError, match="enrollment file sync failure"):
            ProjectState(store.root, PROJECT, MODEL).enroll(DOCUMENT, MAP_BYTES)
        assert failed_syncs == 1
        assert snapshot(store) == before
    ProjectState(store.root, PROJECT, MODEL).enroll(DOCUMENT, MAP_BYTES)
    assert snapshot(store) == before

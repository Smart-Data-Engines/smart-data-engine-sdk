"""Durable project state for a client-side cutover; no rows or credentials are stored here."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import _local_state
from .errors import MigrationRefused


def encode(value: Any) -> bytes:
    # Native principal/namespace names are exact strings, not canonical map identifiers.
    # Keep their spelling; the state checksum is over this explicitly versioned raw-JSON form.
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


class ProjectState:
    """One POSIX directory and lock, shared by all local executors for this project."""

    def __init__(self, root: Path, project_id: str, model_version: str) -> None:
        self.root = root.resolve()
        self.project_id = project_id
        self.model_version = model_version
        self.path = self.root / "project.json"
        self.map_path = self.root / "active-map.json"

    def lock(self) -> Any:
        return _local_state.transaction(self.root)

    def read(self) -> dict[str, Any]:
        try:
            envelope = json.loads(self.path.read_bytes())
            if not isinstance(envelope, dict) or set(envelope) != {
                "storage_contract",
                "payload",
                "sha256",
            }:
                raise ValueError("invalid state envelope")
            if type(envelope["storage_contract"]) is not int or envelope[
                "storage_contract"
            ] not in (1, 2):
                raise ValueError("unsupported state storage contract")
            payload = envelope["payload"]
            if (
                not isinstance(payload, dict)
                or hashlib.sha256(encode(payload)).hexdigest() != envelope["sha256"]
            ):
                raise ValueError("state checksum mismatch")
            if (
                payload.get("project_id") != self.project_id
                or payload.get("model_version") != self.model_version
            ):
                raise ValueError("state belongs to another local project/model")
            fields = {
                "project_id",
                "model_version",
                "active_map",
                "execution",
                "completed",
                "retired_names",
            }
            if envelope["storage_contract"] == 2:
                fields.add("stages")
                if not isinstance(payload.get("stages"), dict):
                    raise ValueError("staging history must be an object")
            if set(payload) != fields:
                raise ValueError("unknown or missing project state fields")
            return payload
        except (OSError, ValueError, TypeError) as exc:
            raise MigrationRefused(
                "local cutover state is missing or corrupt; "
                "restore verified state before proceeding"
            ) from exc

    def confirm(self) -> None:
        # Call under the project lock. Neither reading matching bytes nor an in-memory receipt
        # confirms the directory fsync that may have failed after an earlier publication.
        _local_state.confirm_file(self.map_path)
        _local_state.confirm_file(self.path)

    def write(self, payload: dict[str, Any]) -> None:
        body = encode(payload)
        envelope = {
            "storage_contract": 2 if "stages" in payload else 1,
            "payload": payload,
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        _local_state.write_bytes(self.path, encode(envelope))

    def enroll(self, document: dict[str, Any], map_payload: bytes) -> None:
        with self.lock():
            if self.map_path.exists() and self.map_path.read_bytes() != map_payload:
                raise MigrationRefused("an existing active map differs from the enrolled state")
            if self.path.exists():
                state = self.read()
                if (
                    state["active_map"] != document
                    or state["execution"] is not None
                    or state["completed"]
                ):
                    raise MigrationRefused(
                        "local project is already enrolled; use its current state"
                    )
            else:
                state = {
                    "project_id": self.project_id,
                    "model_version": self.model_version,
                    "active_map": document,
                    "execution": None,
                    "completed": {},
                    "retired_names": [],
                }
                self.write(state)
            if not self.map_path.exists():
                _local_state.write_bytes(self.map_path, map_payload, replace=False)
            # Matching visible files may come from a publication whose directory fsync failed.
            # An identical retry must confirm their durability before enrollment succeeds.
            self.confirm()

    def publish(self, payload: bytes) -> None:
        _local_state.write_bytes(self.map_path, payload)

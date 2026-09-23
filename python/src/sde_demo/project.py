"""Validated customer configuration and local Weather bootstrap orchestration."""

from __future__ import annotations

import base64
import json
import re
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import sde
from sde._local_state import confirm_file, transaction, write_bytes
from sde.generation import GENERATIONS_SINCE, check_map_project

from .model import model


class DemoRefused(ValueError):
    """An actionable starter error whose message contains no driver or input values."""


def payload(path: Path, *, private: bool = False) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise DemoRefused("A required local file is missing or is a symlink.")
    if private and stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise DemoRefused("Credential files must have mode 0600; fix their permissions.")
    with path.open("rb") as stream:
        data = stream.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise DemoRefused("The local JSON input exceeds 2 MiB.")
    return data


def decode(data: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in pairs:
            if name in result:
                raise DemoRefused("JSON contains duplicate keys; restore the original file.")
            result[name] = value
        return result

    value = json.loads(data, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise DemoRefused("The local JSON input must be an object.")
    return value


def read(path: Path, *, private: bool = False) -> dict[str, Any]:
    return decode(payload(path, private=private))


def write(path: Path, value: Mapping[str, Any], *, once: bool = False) -> None:
    payload = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()
    if once and path.exists():
        if path.is_symlink() or path.read_bytes() != payload:
            raise DemoRefused("This directory belongs to another setup; use a fresh directory.")
        confirm_file(path)
        return
    write_bytes(path, payload, replace=not once)


def public_keys(value: Any) -> dict[str, bytes]:
    if not isinstance(value, dict) or not value:
        raise DemoRefused("Bootstrap needs trusted public keys supplied by your operator.")
    result = {}
    for name, encoded in value.items():
        if not isinstance(name, str) or not name or not isinstance(encoded, str):
            raise DemoRefused("Invalid public key configuration.")
        decoded = base64.b64decode(encoded, validate=True)
        if len(decoded) != 32:
            raise DemoRefused("Ed25519 public keys must contain 32 bytes.")
        result[name] = decoded
    return result


def bootstrap(value: dict[str, Any]) -> tuple[dict[str, Any], sde.PlacementMap]:
    if (
        set(value)
        != {"kind", "protocol", "project_id", "model", "public_keys", "engines", "current_map"}
        or value["kind"] != "sde-weather-bootstrap"
        or type(value["protocol"]) is not int
        or value["protocol"] != 1
    ):
        raise DemoRefused("Use the complete protocol-1 Weather bootstrap from your operator.")
    from sde.testing.loader import model_from_neutral

    logical = model()
    if model_from_neutral(value["model"]).version != logical.version:
        raise DemoRefused("This starter requires the Weather model; request a matching bootstrap.")
    engines = value["engines"]
    if (
        not isinstance(engines, dict)
        or not 1 <= len(engines) <= 2
        or any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", name) for name in engines)
        or any(dialect not in ("postgres", "clickhouse") for dialect in engines.values())
        or len(set(engines.values())) != len(engines)
    ):
        raise DemoRefused(
            "Weather demo needs one or two uniquely named PostgreSQL/ClickHouse bindings."
        )
    keys = public_keys(value["public_keys"])
    placement = sde.load_map(
        value["current_map"], model=logical, public_key=keys, require_signature=True
    )
    check_map_project(placement, value["project_id"])
    if placement.contract < GENERATIONS_SINCE or placement.map_version != 1:
        raise DemoRefused(
            "A new demo must start from a signed generation-bearing initial map (version 1)."
        )
    for group in placement.groups.values():
        if len(group.all()) != 1 or group.source.engine not in engines:
            raise DemoRefused("Bootstrap must contain only bound initial source materializations.")
    config = {
        "protocol": 1,
        "project_id": value["project_id"],
        "model": sde.neutral_declaration(logical),
        "public_keys": value["public_keys"],
        "engines": {
            name: {
                "dialect": dialect,
                "operator_dsn_env": f"SDE_WEATHER_OPERATOR_{index}",
                "runtime_dsn_envs": [f"SDE_WEATHER_RUNTIME_{index}"],
            }
            for index, (name, dialect) in enumerate(sorted(engines.items()))
        },
    }
    return config, placement


def config(root: Path, *, ready: bool = True) -> dict[str, Any]:
    expected, _ = bootstrap(read(root / "bootstrap.json"))
    if read(root / "config.json") != expected:
        raise DemoRefused("Local config differs from the enrolled bootstrap; restore it.")
    if ready:
        if (root / "reset-request.json").exists():
            raise DemoRefused("Reset was requested; finish reset and set up a fresh directory.")
        marker = read(root / "setup-complete.json")
        if marker != {"protocol": 1, "config_digest": sde.digest16(expected)}:
            raise DemoRefused(
                "Setup is incomplete or changed; rerun setup with its original input."
            )
    return expected


def credentials(root: Path, purpose: str, bindings: Mapping[str, Any]) -> dict[str, str]:
    import hashlib

    filename = purpose + "-credentials.json"
    data = payload(root / filename, private=True)
    value = decode(data)
    if set(value) != set(bindings) or any(
        not isinstance(dsn, str) or not dsn for dsn in value.values()
    ):
        raise DemoRefused("The local credential bindings are incomplete.")
    # Verify the file this caller needs, without opening the other privilege level's secrets.
    metadata = read(root / "resources.json")
    hashes = metadata.get("credential_hashes")
    if (
        not isinstance(hashes, dict)
        or metadata.get("status") != "ready"
        or hashes.get(purpose) != hashlib.sha256(data).hexdigest()
    ):
        raise DemoRefused("Local credentials differ from the resource allocation; restore them.")
    return value


def engine(dialect: str, dsn: str) -> Any:
    if dialect == "postgres":
        from sde.engines.postgres import PostgresEngine

        return PostgresEngine(dsn)
    if dialect == "clickhouse":
        from sde.engines.clickhouse import ClickHouseEngine

        return ClickHouseEngine(dsn)
    raise DemoRefused("Unsupported engine dialect.")


@contextmanager
def connections(
    root: Path, settings: dict[str, Any]
) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    opened: list[Any] = []
    try:
        operators: dict[str, Any] = {}
        runtime: dict[str, Any] = {}
        for purpose, target in (("operator", operators), ("runtime", runtime)):
            dsns = credentials(root, purpose, settings["engines"])
            for name, binding in sorted(settings["engines"].items()):
                adapter = engine(binding["dialect"], dsns[name])
                opened.append(adapter)
                adapter.connect()
                target[name] = adapter
        yield operators, runtime
    finally:
        # Attempt every cleanup, even if an earlier adapter cannot close.
        failures: list[Exception] = []
        for adapter in reversed(opened):
            try:
                adapter.close()
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise DemoRefused("An engine connection could not close; stop this process.")


def setup(root: Path, supplied: dict[str, Any], admin_dsns: Mapping[str, str]) -> dict[str, Any]:
    from .resources import allocate, grant_tables

    settings, placement = bootstrap(supplied)
    logical = model()
    with transaction(root):
        if (root / "reset-request.json").exists():
            raise DemoRefused(
                "This allocation is being reset; use a new directory for the next demo."
            )
        write(root / "bootstrap.json", supplied, once=True)
        write(root / "config.json", settings, once=True)
        allocate(root, supplied["engines"], admin_dsns)
        if (root / "setup-complete.json").exists():
            config(root)
            from sde._cutover_project import ProjectState

            store = ProjectState(root / "state", settings["project_id"], logical.version)
            with store.lock():
                store.read()
                current = sde.load_local_map(
                    root / "state",
                    model=logical,
                    project_id=settings["project_id"],
                    public_key=public_keys(settings["public_keys"]),
                )
                store.confirm()
                confirm_file(root / "setup-complete.json")
                return {"status": "ready", "map_version": current.map_version}
        if (root / "state" / "active-map.json").exists():
            current = sde.load_local_map(
                root / "state",
                model=logical,
                project_id=settings["project_id"],
                public_key=public_keys(settings["public_keys"]),
            )
            if current.fingerprint != placement.fingerprint:
                raise DemoRefused(
                    "An unfinished setup already advanced its map; inspect local state."
                )
        with connections(root, settings) as (operators, runtime):
            sde.prepare_schema(logical, placement, operators, project_id=settings["project_id"])
            tables: dict[str, list[str]] = {name: ["sde_map_state"] for name in operators}
            for group in placement.groups.values():
                tables[group.source.engine].extend(group.source.layout.tables.values())
            grant_tables(root, tables, admin_dsns)
            local = sde.LocalCutover(
                root / "state",
                model=logical,
                project_id=settings["project_id"],
                public_key=public_keys(settings["public_keys"]),
                operators=operators,
                runtime={name: [adapter] for name, adapter in runtime.items()},
            )
            local.enroll(supplied["current_map"])
            with sde.Session(logical, placement, runtime, project_id=settings["project_id"]):
                pass
        write(
            root / "setup-complete.json",
            {"protocol": 1, "config_digest": sde.digest16(settings)},
            once=True,
        )
        return {"status": "ready", "map_version": placement.map_version}

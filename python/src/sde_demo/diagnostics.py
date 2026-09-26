"""Local diagnostics using existing native identity and grant qualification."""

from __future__ import annotations

import platform
from pathlib import Path
from typing import Any

import sde
from sde._local_state import transaction
from sde.engines._operator import NativeOperator

from . import resources
from .model import model
from .project import config, connections, public_keys, require_drivers


def doctor(root: Path) -> dict[str, Any]:
    with transaction(root):
        settings = config(root)
        require_drivers(binding["dialect"] for binding in settings["engines"].values())
        resources.verify(root)
        return _diagnose(root, settings)


def _diagnose(root: Path, settings: dict[str, Any]) -> dict[str, Any]:
    logical = model()
    placement = sde.load_local_map(
        root / "state",
        model=logical,
        project_id=settings["project_id"],
        public_key=public_keys(settings["public_keys"]),
    )
    tables: dict[str, set[str]] = {name: {"sde_map_state"} for name in settings["engines"]}
    for group in placement.groups.values():
        for material in group.all():
            tables[material.engine].update(material.layout.tables.values())
    results = {}
    with connections(root, settings) as (operators, runtime):
        for name, operator in operators.items():
            native = NativeOperator(operator, [runtime[name]])
            native.qualify(sorted(tables[name]), sorted(tables[name]))
            if operator.dialect == "postgres":
                version = str(native.rows("SHOW server_version")[0][0])
                tls = bool(
                    native.rows(
                        "SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()",
                        engine=runtime[name],
                    )[0][0]
                )
            else:
                version = str(native.rows("SELECT version()")[0][0])
                tls = runtime[name]._cx.url.startswith("https://")
            results[name] = {
                "dialect": operator.dialect,
                "version": version,
                "native_identity": "matched",
                "runtime_privileges": "qualified",
                "connection_tls": tls,
            }
        with sde.Session(
            logical, placement, runtime, project_id=settings["project_id"]
        ) as session:
            storage = session.measure_storage()
        # Whether each engine can give the window a group's size, by the class of the reason -
        # never the size itself. An engine that holds no group's source has nothing to measure.
        measured = {size.engine for size in storage.sizes}
        for name, result in results.items():
            sources = [
                group for group, spot in placement.groups.items() if spot.source.engine == name
            ]
            reasons = sorted(
                {storage.unavailable[group] for group in sources if group in storage.unavailable}
            )
            result["storage_telemetry"] = (
                "no_source" if not sources
                else "available" if name in measured and not reasons
                else "unavailable: " + ", ".join(reasons)
            )
    return {
        "protocol": 1,
        "status": "ready",
        "scope": "local-weather-demo",
        "sdk_version": sde.__version__,
        "python_version": platform.python_version(),
        "project_id": settings["project_id"],
        "model_version": logical.version,
        "map_version": placement.map_version,
        "signature": "verified",
        "engines": results,
        "production_qualification": "not_performed",
    }

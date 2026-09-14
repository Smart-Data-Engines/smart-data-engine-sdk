"""Disposable customer-side project for qualification, using the same native role fixtures as CI."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_runtime_privileges_live import runtime_roles

import sde
from sde.testing.loader import model_from_neutral

DECLARATION = {
    "entities": [
        {
            "name": "WeatherReading",
            "fields": [
                {"name": "at", "type": "timestamptz"},
                {"name": "celsius", "type": "decimal(8,2)"},
                {"name": "humidity", "type": "int64"},
                {"name": "id", "type": "uuid"},
                {"name": "station", "type": "string"},
            ],
            "key": ["station", "at"],
            "residency": "EU",
        }
    ],
    "relations": [],
    "atomic": [],
    "cost_ceiling": {"amount": "500.00", "currency": "EUR"},
}


def postgres_uri(dsn: str) -> str:
    """Give both SDKs the same URI rather than a Python-only libpq keyword connection string."""
    parameters = conninfo_to_dict(dsn)

    def required(name: str) -> str:
        value = parameters.pop(name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"qualification connection needs a nonempty {name}")
        return value

    user = quote(required("user"), safe="")
    password = quote(required("password"), safe="")
    host = required("host")
    if ":" in host:
        host = "[" + host + "]"
    port = parameters.pop("port", "5432")
    database = quote(required("dbname"), safe="")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}?{urlencode(parameters)}"


@contextmanager
def project(
    root: Path,
    source: str,
    *,
    bootstrap: Callable[[Path, str, dict[str, Any]], dict[str, Any]] | None = None,
) -> Iterator[dict[str, Any]]:
    root.mkdir(parents=True, exist_ok=False)
    model = model_from_neutral(DECLARATION)
    key = Ed25519PrivateKey.generate()
    public = {"qualification": key.public_key().public_bytes_raw()}
    project_id = uuid4().hex

    def signed(raw: dict[str, Any]) -> dict[str, Any]:
        document = deepcopy(raw)
        document.pop("signature", None)
        document["signature"] = {
            "alg": "ed25519",
            "key_id": "qualification",
            "value": base64.b64encode(key.sign(sde.canonical_bytes(document))).decode(),
        }
        return document

    group = sde.colocation_groups(model)[0]
    layout = sde.default_layout(model, group, dialect=source)
    layout_document = {
        "tables": dict(layout.tables),
        "columns": {entity: dict(fields) for entity, fields in layout.columns.items()},
        "indexes": [dict(index) for index in layout.indexes],
    }
    current = signed(
        {
            "contract": 4,
            "project_id": project_id,
            "model_version": model.version,
            "map_version": 1,
            "groups": {
                group.name: {
                    "source": {"id": "source", "engine": source, "layout": layout_document},
                    "write_epoch": 1,
                }
            },
        }
    )
    supplied: dict[str, Any] = {}
    if bootstrap is not None:
        supplied = bootstrap(root, source, sde.neutral_declaration(model))
        current, project_id, public = (
            supplied["current"],
            supplied["project_id"],
            supplied["public_keys"],
        )
    with runtime_roles("postgres") as pg, runtime_roles("clickhouse") as ch:
        roles = {"postgres": pg, "clickhouse": ch}
        operators = {name: role.operator for name, role in roles.items()}
        runtime = {name: [role.runtime] for name, role in roles.items()}
        placement = sde.load_map(current, model=model, public_key=public)
        sde.prepare_schema(model, placement, operators, project_id=project_id)
        for table in placement.groups[group.name].source.layout.tables.values():
            roles[source].grant(table)
        for role in roles.values():
            role.grant("sde_map_state")
        local = sde.LocalCutover(
            root / "state",
            model=model,
            project_id=project_id,
            public_key=public,
            operators=operators,
            runtime=runtime,
        )
        local.enroll(current)
        config: dict[str, Any] = {
            "protocol": 1,
            "project_id": project_id,
            "model": DECLARATION,
            "public_keys": {
                name: base64.b64encode(value).decode() for name, value in public.items()
            },
            "engines": {},
        }
        environment = {}
        for name, role in roles.items():
            op_name, app_name = (
                "SDE_QUALIFY_OPERATOR_" + name.upper(),
                "SDE_QUALIFY_APP_" + name.upper(),
            )
            environment[op_name] = (
                make_conninfo(role.operator._dsn, options="-csearch_path=" + role.namespace)
                if name == "postgres"
                else role.operator._dsn
            )
            environment[app_name] = role.runtime._dsn
            if name == "postgres":
                environment[op_name] = postgres_uri(environment[op_name])
                environment[app_name] = postgres_uri(environment[app_name])
            config["engines"][name] = {
                "dialect": name,
                "operator_dsn_env": op_name,
                "runtime_dsn_envs": [app_name],
            }
        config_path = root / "config.json"
        config_path.write_text(json.dumps(config))
        yield {
            **{
                name: supplied[name]
                for name in ("issue_stage", "issue_cutover", "observe", "controller_state")
                if name in supplied
            },
            "model": model,
            "public": public,
            "project_id": project_id,
            "signed": signed,
            "roles": roles,
            "operator": local,
            "config": config_path,
            "environment": environment,
            "source": source,
            "directory": root,
        }

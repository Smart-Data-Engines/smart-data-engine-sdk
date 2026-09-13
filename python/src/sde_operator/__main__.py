"""Local operator CLI. Configuration names environment variables; credentials never leave it."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sde.errors import EngineError, MigrationRefused, SdeError
from sde.generation import check_map_project
from sde.local_cutover import CutoverRecoveryRequired, LocalCutover, _local_status
from sde.placement import load_map
from sde.testing.loader import model_from_neutral


def read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise MigrationRefused("operator input file is missing or invalid JSON") from exc
    if not isinstance(value, dict):
        raise MigrationRefused("operator input must be a JSON object")
    return value


def environment(name: Any) -> str:
    if not isinstance(name, str) or not name or not os.environ.get(name):
        raise MigrationRefused("a configured local connection environment variable is missing")
    return os.environ[name]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("current-map")
    commands.add_parser("resume")
    enroll = commands.add_parser("enroll")
    enroll.add_argument("--map", type=Path, required=True)
    execute = commands.add_parser("execute")
    execute.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args(argv)
    adapters: list[Any] = []
    try:
        config = read(args.config)
        if (
            set(config) != {"protocol", "project_id", "model", "public_keys", "engines"}
            or type(config["protocol"]) is not int
            or config["protocol"] != 1
        ):
            raise MigrationRefused("unsupported or incomplete local operator configuration")
        model = model_from_neutral(config["model"])
        project = config["project_id"]
        if not isinstance(config["public_keys"], dict) or not config["public_keys"]:
            raise MigrationRefused("operator configuration needs trusted public keys")
        keys = {
            name: base64.b64decode(value, validate=True)
            for name, value in config["public_keys"].items()
        }
        if args.command == "current-map":
            raw = read(args.project_dir / "active-map.json")
            current = load_map(raw, model=model, public_key=keys, require_signature=True)
            check_map_project(current, project)
            print(json.dumps(raw))
            return 0
        if args.command == "status":
            print(
                json.dumps(
                    _local_status(
                        args.project_dir, model=model, project_id=project, public_key=keys
                    )
                )
            )
            return 0
        from sde.cutover import load_cutover_plan
        from sde.engines.clickhouse import ClickHouseEngine
        from sde.engines.postgres import PostgresEngine

        if not isinstance(config["engines"], dict) or not config["engines"]:
            raise MigrationRefused("operator configuration needs local engine bindings")
        operators: dict[str, Any] = {}
        runtime: dict[str, list[Any]] = {}
        for name, record in config["engines"].items():
            if not isinstance(record, dict) or set(record) != {
                "dialect",
                "operator_dsn_env",
                "runtime_dsn_envs",
            }:
                raise MigrationRefused("invalid local engine binding configuration")
            if record["dialect"] not in ("postgres", "clickhouse"):
                raise MigrationRefused("the local operator supports PostgreSQL and ClickHouse")
            probes = record["runtime_dsn_envs"]
            if not isinstance(probes, list) or not probes:
                raise MigrationRefused("engine binding needs dedicated runtime probes")
            kind = PostgresEngine if record["dialect"] == "postgres" else ClickHouseEngine
            operators[name] = kind(environment(record["operator_dsn_env"]))
            adapters.append(operators[name])
            operators[name].connect()
            runtime[name] = []
            for variable in probes:
                engine = kind(environment(variable))
                adapters.append(engine)
                engine.connect()
                runtime[name].append(engine)
        executor = LocalCutover(
            args.project_dir,
            model=model,
            project_id=project,
            public_key=keys,
            operators=operators,
            runtime=runtime,
        )
        if args.command == "enroll":
            executor.enroll(read(args.map))
            print(json.dumps(executor.status()))
        elif args.command == "execute":
            plan = load_cutover_plan(
                read(args.plan), model=model, project_id=project, public_key=keys
            )
            print(json.dumps(executor.execute(plan).as_record()))
        else:
            print(json.dumps(executor.resume().as_record()))
        return 0
    except CutoverRecoveryRequired:
        print(
            json.dumps(
                {
                    "error": "recovery_required",
                    "message": (
                        "Inspect local status and resume with fresh connections; "
                        "the durable decision must be preserved."
                    ),
                }
            ),
            file=sys.stderr,
        )
        return 3
    except MigrationRefused as exc:
        print(json.dumps({"error": "refused", "message": str(exc)}), file=sys.stderr)
        return 2
    except EngineError:
        # Driver exceptions can contain SQL values or a malformed DSN. Do not put them in a
        # machine-readable receipt that an operator may send back to the controller.
        print(
            json.dumps(
                {
                    "error": "engine_error",
                    "message": (
                        "The local engine operation failed. Inspect project status before retrying."
                    ),
                }
            ),
            file=sys.stderr,
        )
        return 3
    except (SdeError, ValueError, TypeError, KeyError):
        print(
            json.dumps(
                {"error": "configuration_error", "message": "Invalid local operator input."}
            ),
            file=sys.stderr,
        )
        return 2
    except OSError:
        print(
            json.dumps(
                {
                    "error": "storage_error",
                    "message": ("Local state is unavailable. Inspect status before retrying."),
                }
            ),
            file=sys.stderr,
        )
        return 3
    finally:
        for engine in adapters:
            engine.close()


if __name__ == "__main__":
    raise SystemExit(main())

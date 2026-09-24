"""Run the customer-side Weather starter; engine credentials and values stay local."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import sde
from sde._local_state import transaction

from .model import WORKLOADS, model
from .project import DemoRefused, config, connections, public_keys, read, setup, write
from .resources import ResourceRefused


def operator(root: Path, action: str, plan: Path | None) -> dict[str, Any]:
    from sde.local_cutover import _local_status

    from . import resources

    with transaction(root):
        settings, logical = config(root, ready=action != "status"), model()
        keys = public_keys(settings["public_keys"])
        if action == "status":
            return _local_status(
                root / "state", model=logical, project_id=settings["project_id"], public_key=keys
            )
        resources.verify(root)
        with connections(root, settings) as (operators, runtime):
            local = sde.LocalCutover(
                root / "state",
                model=logical,
                project_id=settings["project_id"],
                public_key=keys,
                operators=operators,
                runtime={name: [adapter] for name, adapter in runtime.items()},
            )
            if action == "resume":
                return local.resume().as_record()
            if action == "abandon":
                return local.abandon().as_record()
            if plan is None:
                raise DemoRefused("Stage, index and execute require --plan from the controller.")
            if action == "index":
                return local.index(
                    sde.load_index_plan(
                        read(plan),
                        model=logical,
                        project_id=settings["project_id"],
                        public_key=keys,
                    )
                ).as_record()
            if action == "stage":
                return local.stage(
                    sde.load_staging_plan(
                        read(plan),
                        model=logical,
                        project_id=settings["project_id"],
                        public_key=keys,
                    )
                ).as_record()
            return local.execute(
                sde.load_cutover_plan(
                    read(plan), model=logical, project_id=settings["project_id"], public_key=keys
                )
            ).as_record()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("model", help="print the neutral Weather model for the controller")
    start = commands.add_parser("setup", help="allocate loopback-only demo resources and enroll")
    start.add_argument("--bootstrap", type=Path, required=True)
    reset = commands.add_parser(
        "reset", help="remove this allocation after stopping demo processes"
    )
    reset.add_argument("--confirm-allocation", required=True)
    for command in (start, reset):
        command.add_argument("--postgres-admin-env", default="SDE_POSTGRES_DSN")
        command.add_argument("--clickhouse-admin-env", default="SDE_CLICKHOUSE_DSN")
    commands.add_parser("doctor", help="check the local map, connections and runtime grants")
    workload = commands.add_parser("run", help="run a bounded logical workload with runtime rights")
    workload.add_argument("--iterations", type=int, default=10)
    workload.add_argument("--batch-size", type=int, default=10)
    workload.add_argument("--interval-ms", type=int, default=100)
    workload.add_argument("--recovery-ms", type=int, default=10000)
    workload.add_argument("--workload", choices=WORKLOADS, default="mixed")
    verify = commands.add_parser(
        "verify-runs", help="verify earlier completed Python/TypeScript runs"
    )
    verify.add_argument("--run-id", action="append", required=True)
    query = commands.add_parser(
        "query-count", help="execute the current approved Weather COUNT locally"
    )
    query.add_argument("--record", type=Path, required=True)
    op = commands.add_parser(
        "operator", help="use the existing local staging/index/cutover executor"
    )
    op.add_argument("action", choices=("status", "stage", "index", "execute", "resume", "abandon"))
    op.add_argument("--plan", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "model":
            result = sde.neutral_declaration(model())
        else:
            if args.directory is None:
                raise DemoRefused("Provide --directory for this customer-side demo allocation.")
            root = args.directory.absolute()
            if args.command in ("setup", "reset"):
                admin = {
                    dialect: os.environ[variable]
                    for dialect, variable in (
                        ("postgres", args.postgres_admin_env),
                        ("clickhouse", args.clickhouse_admin_env),
                    )
                    if os.environ.get(variable)
                }
                if args.command == "setup":
                    result = setup(root, read(args.bootstrap), admin)
                else:
                    from .resources import reset as reset_resources

                    with transaction(root):
                        allocation = read(root / "resources.json")["allocation_id"]
                        if args.confirm_allocation != allocation:
                            raise DemoRefused(
                                "Confirm the allocation_id from local resources.json."
                            )
                        write(root / "reset-request.json", {"allocation_id": allocation}, once=True)
                        result = reset_resources(root, admin)
            elif args.command == "doctor":
                from .diagnostics import doctor

                result = doctor(root)
            elif args.command == "operator":
                result = operator(root, args.action, args.plan)
            elif args.command == "verify-runs":
                from .verification import verify_runs

                result = verify_runs(root, args.run_id)
            elif args.command == "query-count":
                from .query_count import run_count_query

                result = run_count_query(root, read(args.record))
            else:
                from .runtime import run

                result = run(
                    root,
                    iterations=args.iterations,
                    batch_size=args.batch_size,
                    interval_ms=args.interval_ms,
                    recovery_ms=args.recovery_ms,
                    workload=args.workload,
                )
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except (DemoRefused, ResourceRefused) as exc:
        print(json.dumps({"error": "demo_refused", "message": str(exc)}), file=sys.stderr)
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": type(exc).__name__,
                    "message": "Operation incomplete. Inspect local state and the run report "
                    "before retrying; uncertain writes must not be replayed.",
                }
            ),
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

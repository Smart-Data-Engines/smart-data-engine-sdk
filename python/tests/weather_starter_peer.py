"""Native test peer: bootstrap/reset locally and reveal only synthetic test metadata."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from _weather_fixture import supplied

from sde_demo import project, resources

parser = argparse.ArgumentParser()
parser.add_argument("command", choices=("setup", "reset"))
parser.add_argument("--directory", type=Path, required=True)
parser.add_argument("--source", choices=("postgres", "clickhouse"), default="postgres")
args = parser.parse_args()
admin = {
    dialect: os.environ[variable]
    for dialect, variable in (
        ("postgres", "SDE_POSTGRES_DSN"),
        ("clickhouse", "SDE_CLICKHOUSE_DSN"),
    )
}
try:
    if args.command == "setup":
        bundle, _ = supplied(args.source)
        print(json.dumps(project.setup(args.directory, bundle, admin)))
    elif (args.directory / "resources.json").exists():
        print(json.dumps({"status": resources.reset(args.directory, admin)["status"]}))
except Exception as exc:
    print(json.dumps({"error": type(exc).__name__}), file=sys.stderr)
    sys.exit(1)

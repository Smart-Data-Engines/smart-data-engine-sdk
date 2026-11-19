"""Build what the soak runs, as a client installs it, and name each artifact by its SHA-256.

    PYTHONPATH=python/tests python/.venv/bin/python -m qualification.soak_artifacts \\
        --out /absolute/new-directory

- Python: a wheel of this tree, installed with its extras into a fresh virtual environment;
- TypeScript: ``npm pack`` of this tree, installed with ``pg`` into a fresh consumer;
- C++: the library built in Release and installed into a prefix, and the soak's process built
  against that prefix with ``find_package(sde)``, as somebody else's build would.

``artifacts.json`` in the output directory names them, with the tree's commit and whether it had
uncommitted changes; ``soak --artifacts`` reads it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

SDK = Path(__file__).resolve().parents[3]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def run(command: list[str], log: Path, cwd: Path | None = None) -> None:
    with log.open("ab") as out:
        out.write(("$ " + " ".join(command) + "\n").encode())
        out.flush()
        subprocess.run(command, cwd=cwd, stdout=out, stderr=subprocess.STDOUT, check=True)


def python(out: Path, log: Path) -> dict[str, Any]:
    run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", str(out),
         str(SDK / "python")], log)  # fmt: skip
    [wheel] = sorted(out.glob("smart_data_engine_sdk-*.whl"))
    venv = out / "wheel-venv"
    run([sys.executable, "-m", "venv", str(venv)], log)
    run([str(venv / "bin" / "pip"), "install", "-q", f"{wheel}[signed,postgres,clickhouse]"], log)
    return {"wheel": wheel.name, "sha256": sha256(wheel), "interpreter": str(venv / "bin/python")}


def typescript(out: Path, log: Path) -> dict[str, Any]:
    package = SDK / "typescript"
    if not (package / "node_modules").exists():
        run(["npm", "ci", "--no-audit", "--no-fund"], log, cwd=package)
    run(["npm", "run", "build"], log, cwd=package)
    run(["npm", "pack", "--pack-destination", str(out)], log, cwd=package)
    [tarball] = sorted(out.glob("smart-data-engines-sde-*.tgz"))
    consumer = out / "npm-consumer"
    consumer.mkdir()
    (consumer / "package.json").write_text('{"name": "soak-consumer", "private": true}\n')
    run(["npm", "install", "--no-audit", "--no-fund", str(tarball), "pg"], log, cwd=consumer)
    return {
        "tarball": tarball.name,
        "sha256": sha256(tarball),
        "package": str(consumer / "node_modules/@smart-data-engines/sde"),
    }


def cpp(out: Path, log: Path) -> dict[str, Any]:
    build, prefix, worker_build = out / "cpp-build", out / "cpp-prefix", out / "soak-worker-build"
    run(["cmake", "-S", str(SDK / "cpp"), "-B", str(build), "-G", "Ninja",
         "-DCMAKE_BUILD_TYPE=Release", "-DSDE_BUILD_TESTS=OFF", "-DSDE_ORDERBOOK=OFF",
         f"-DCMAKE_INSTALL_PREFIX={prefix}"], log)  # fmt: skip
    run(["cmake", "--build", str(build)], log)
    run(["cmake", "--install", str(build)], log)
    run(["cmake", "-S", str(SDK / "cpp/tests/qualification"), "-B", str(worker_build), "-G",
         "Ninja", "-DCMAKE_BUILD_TYPE=Release", f"-DCMAKE_PREFIX_PATH={prefix}",
         f"-DCMAKE_INSTALL_PREFIX={prefix}"], log)  # fmt: skip
    run(["cmake", "--build", str(worker_build)], log)
    run(["cmake", "--install", str(worker_build)], log)
    worker = prefix / "bin" / "sde_soak_worker"
    libraries = sorted(p for p in (prefix / "lib").rglob("libsde*") if p.is_file())
    return {
        "worker": str(worker),
        "sha256": sha256(worker),
        "libraries": {p.name: sha256(p) for p in libraries},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--skip", nargs="*", default=[], choices=["python", "typescript", "cpp"])
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    log = args.out / "build.log"
    commit = subprocess.run(
        ["git", "-C", str(SDK), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "-C", str(SDK), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    artifacts: dict[str, Any] = {"sdk_commit": commit, "uncommitted_changes": dirty}
    for name, build in (("python", python), ("typescript", typescript), ("cpp", cpp)):
        if name not in args.skip:
            artifacts[name] = build(args.out, log)
    (args.out / "artifacts.json").write_text(json.dumps(artifacts, indent=1) + "\n")
    print(json.dumps(artifacts, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

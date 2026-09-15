"""Run native TLS checks in disposable engines with ephemeral, test-only certificates.

Requires Docker, the Python SDK test extras and a built TypeScript tree. Local users can pass
--sudo; CI uses its own Docker access. All container names and storage belong to this invocation.
"""

from __future__ import annotations

import argparse
import json
import os
import runpy
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import cast

REPO = Path(__file__).resolve().parents[1]
PASSWORD = "synthetic-tls-only"
PG_SCRIPT = """set -e
mkdir -p /run/sde-tls
cp /input/*.pem /run/sde-tls/
chown -R postgres:postgres /run/sde-tls
chmod 700 /run/sde-tls
chmod 600 /run/sde-tls/server_key.pem
exec docker-entrypoint.sh postgres -c ssl=on \\
  -c ssl_cert_file=/run/sde-tls/server_cert.pem \\
  -c ssl_key_file=/run/sde-tls/server_key.pem \\
  -c ssl_ca_file=/run/sde-tls/ca.pem
"""
CH_SCRIPT = """set -e
mkdir -p /run/sde-tls
cp /input/*.pem /run/sde-tls/
chown -R clickhouse:clickhouse /run/sde-tls
chmod 700 /run/sde-tls
chmod 600 /run/sde-tls/server_key.pem
exec /entrypoint.sh
"""
CH_CONFIG = """<clickhouse>
<https_port>8443</https_port>
<openSSL><server>
<certificateFile>/run/sde-tls/server_cert.pem</certificateFile>
<privateKeyFile>/run/sde-tls/server_key.pem</privateKeyFile>
<caConfig>/run/sde-tls/ca.pem</caConfig>
<verificationMode>none</verificationMode>
<disableProtocols>sslv2,sslv3,tlsv1,tlsv1_1</disableProtocols>
<loadDefaultCAFile>false</loadDefaultCAFile>
</server></openSSL>
<max_server_memory_usage>1000000000</max_server_memory_usage>
</clickhouse>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scratch", type=Path, required=True, help="New private test directory"
    )
    parser.add_argument("--sudo", action="store_true")
    parser.add_argument(
        "--language", choices=("python", "typescript", "both"), default="both"
    )
    args = parser.parse_args()
    scratch = args.scratch.resolve()
    scratch.mkdir(
        mode=0o700
    )  # Refuse an existing directory; never delete caller files.
    helper = runpy.run_path(str(REPO / "python/tests/tls_certificates.py"))
    create_material = cast(Callable[[Path], dict[str, Path]], helper["create_material"])
    material = create_material(scratch / "certs")
    config = scratch / "tls.xml"
    config.write_text(CH_CONFIG)
    prefix = ["sudo", "docker"] if args.sudo else ["docker"]
    token = uuid.uuid4().hex
    pg, ch = "sde-tls-pg-" + token, "sde-tls-ch-" + token
    created: list[str] = []

    def docker(*words: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            prefix + list(words),
            check=check,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def port(name: str, inside: str) -> int:
        info = json.loads(docker("inspect", name).stdout)[0]
        return int(info["NetworkSettings"]["Ports"][inside + "/tcp"][0]["HostPort"])

    try:
        for name, image, inside, script in (
            (pg, "postgres:15-alpine", "5432", PG_SCRIPT),
            (ch, "clickhouse/clickhouse-server:24.8-alpine", "8443", CH_SCRIPT),
        ):
            options = (
                ["-e", "POSTGRES_PASSWORD=" + PASSWORD, "-e", "POSTGRES_DB=sde"]
                if name == pg
                else [
                    "-e",
                    "CLICKHOUSE_PASSWORD=" + PASSWORD,
                    "-e",
                    "CLICKHOUSE_DB=sde",
                    "-e",
                    "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1",
                    "-v",
                    str(config) + ":/etc/clickhouse-server/config.d/sde-tls.xml:ro",
                ]
            )
            # Cleanup only names whose successful run this process recorded. UUID and --name
            # make an unrelated preexisting container a refusal instead of a cleanup target.
            docker(
                "run",
                "-d",
                "--rm",
                "--name",
                name,
                "--label",
                "sde.tls-test=" + token,
                "-p",
                "127.0.0.1::" + inside,
                "-v",
                str(scratch / "certs") + ":/input:ro",
                *options,
                "--entrypoint",
                "/bin/sh",
                image,
                "-c",
                script,
            )
            created.append(name)
        pg_port, ch_port = port(pg, "5432"), port(ch, "8443")
        record = {
            "postgres_port": pg_port,
            "clickhouse_port": ch_port,
            "password": PASSWORD,
            "material": {key: str(value) for key, value in material.items()},
        }
        manifest = scratch / "endpoints.json"
        with manifest.open("x") as output:
            os.chmod(manifest, 0o600)
            json.dump(record, output)
        context = ssl.create_default_context(cafile=str(material["ca"]))
        deadline = time.monotonic() + 60
        while True:
            pg_ok = (
                docker(
                    "exec", pg, "pg_isready", "-U", "postgres", "-d", "sde", check=False
                ).returncode
                == 0
            )
            try:
                # This is the same verified HTTPS host/port the SDK will use.
                with urllib.request.urlopen(
                    f"https://127.0.0.1:{ch_port}/ping", context=context, timeout=2
                ) as response:
                    ch_ok = response.status == 200
            except (OSError, urllib.error.URLError):
                ch_ok = False
            if pg_ok and ch_ok:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Native TLS engines did not become ready within 60 seconds"
                )
            time.sleep(0.25)
        env = os.environ | {"SDE_TLS_TEST": str(manifest), "TMPDIR": str(scratch)}
        if args.language in ("python", "both"):
            subprocess.run(
                [sys.executable, str(REPO / "tools/tls_python.py")],
                env=env,
                check=True,
                timeout=120,
            )
        if args.language in ("typescript", "both"):
            subprocess.run(
                ["node", "tests/qualification/tls-worker.mjs"],
                cwd=REPO / "typescript",
                env=env,
                check=True,
                timeout=120,
            )
        print(json.dumps({"native_tls": "passed", "language": args.language}))
    except BaseException:
        for name in created:
            logs = docker("logs", "--tail", "100", name, check=False)
            (scratch / (name + ".log")).write_text(logs.stdout + logs.stderr)
        raise
    finally:
        failed = []
        for name in reversed(created):
            # Verify ownership again before the destructive operation.
            info = docker("inspect", name, check=False)
            if info.returncode == 0:
                labels = json.loads(info.stdout)[0]["Config"]["Labels"]
                if (
                    labels.get("sde.tls-test") != token
                    or docker("rm", "-f", name, check=False).returncode != 0
                ):
                    failed.append(name)
        if failed:
            raise RuntimeError(
                "Could not clean up own test containers: " + ", ".join(failed)
            )


if __name__ == "__main__":
    main()

"""Add verification-request cases without regenerating any pre-existing vector.

The map fingerprint is checked against openssl over independently encoded ASCII JSON. The map,
model and data come from the already-frozen positive verification case. Existing outputs are never
silently overwritten: changing them requires the same explicit contract-change flag as other tools.
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))
import sde  # noqa: E402
from sde.testing.loader import model_from_neutral  # noqa: E402
from sde.testing.memory import engines_from  # noqa: E402

BASE = ROOT / "conformance/vectors/migration/017-a-verify-that-matches"
PROJECT = "1" * 32
AT = "2026-09-12T12:00:00Z"
DONE = "2026-09-12T12:00:01Z"


def read(name: str) -> Any:
    return json.loads((BASE / name).read_text())


def sign(document: dict[str, Any], directory: Path) -> tuple[dict[str, Any], str]:
    import base64

    key, public, payload, signature = (
        directory / name for name in ("key", "public", "payload", "sig")
    )
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(key)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["openssl", "pkey", "-in", str(key), "-pubout", "-outform", "DER", "-out", str(public)],
        check=True,
        capture_output=True,
    )
    payload.write_bytes(sde.canonical_bytes(document))
    subprocess.run(
        [
            "openssl",
            "pkeyutl",
            "-sign",
            "-inkey",
            str(key),
            "-rawin",
            "-in",
            str(payload),
            "-out",
            str(signature),
        ],
        check=True,
        capture_output=True,
    )
    result = copy.deepcopy(document)
    result["signature"] = {
        "alg": "ed25519",
        "value": base64.b64encode(signature.read_bytes()).decode(),
    }
    return result, base64.b64encode(public.read_bytes()[-32:]).decode()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-am-changing-the-contract", action="store_true", required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    args = parser.parse_args()
    model_document, document, engine_document = (
        read("model.json"),
        read("map.json"),
        read("engines.json"),
    )
    model = model_from_neutral(model_document)
    placement = sde.load_map(document, model=model)
    canonical = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    fingerprint = (
        subprocess.run(
            ["openssl", "dgst", "-sha256"], input=canonical, capture_output=True, check=True
        )
        .stdout.decode()
        .split()[-1]
    )
    assert fingerprint == placement.fingerprint
    request = sde.verification_request(
        placement, group="Reading", project_id=PROJECT, request_id="2" * 32, requested_at=AT
    ).as_record()
    cases: list[tuple[str, dict[str, Any], str | None]] = [
        ("022-verification-is-bound-to-its-request", {}, None),
        ("023-verification-refuses-another-project", {"project_id": "3" * 32}, "another project"),
        ("024-verification-refuses-another-model", {"model_version": "0" * 16}, "does not match"),
        ("025-verification-refuses-another-map-version", {"map_version": 2}, "does not match"),
        (
            "026-verification-refuses-another-map-fingerprint",
            {"map_fingerprint": "0" * 64},
            "does not match",
        ),
        ("027-verification-refuses-another-group", {"group": "Elsewhere"}, "another group"),
        (
            "028-verification-refuses-another-source-engine",
            {"source": {"engine": "other", "id": "Reading@pg"}},
            "does not match",
        ),
        (
            "029-verification-refuses-another-target-engine",
            {"targets": [{"engine": "other", "id": "Reading@pg2"}]},
            "does not match",
        ),
        ("030-verification-needs-a-locally-configured-project", {}, "no project_id"),
        ("031-verification-cannot-predate-the-request", {}, "predates"),
        ("032-verification-refuses-a-row-field", {"rows": []}, "unknown fields"),
        (
            "033-verification-refuses-a-boolean-protocol",
            {"protocol": True},
            "unsupported verification request protocol",
        ),
        (
            "034-verification-refuses-a-non-string-target",
            {"targets": [{"engine": "pg-copy", "id": []}]},
            "names must be",
        ),
        (
            "035-verification-checks-the-signature-requirement",
            {"requires_signature": True},
            "does not match",
        ),
        (
            "036-verification-refuses-another-source-id",
            {"source": {"engine": "pg-main", "id": "other"}},
            "does not match",
        ),
        (
            "037-verification-refuses-another-target-id",
            {"targets": [{"engine": "pg-copy", "id": "other"}]},
            "does not match",
        ),
        ("038-verification-needs-a-target", {"targets": []}, "nonempty"),
        ("039-verification-detects-layout-change-without-version-change", {}, "does not match"),
        ("040-verification-fingerprint-excludes-the-signature", {}, None),
        ("041-verification-fingerprint-excludes-the-key-hint", {}, None),
        (
            "042-verification-refuses-nonstandard-offset-spelling",
            {"requested_at": "2026-09-12T12:00:00+01:3059"},
            "ISO timestamp",
        ),
        (
            "043-verification-refuses-submicrosecond-time",
            {"requested_at": "2026-09-12T12:00:00.1234567Z"},
            "ISO timestamp",
        ),
        (
            "044-verification-normalizes-integral-json-numbers",
            {"protocol": 1.0, "map_version": 1.0},
            None,
        ),
        ("045-verification-never-truncates-a-fractional-version", {"map_version": 1.5}, "integer"),
    ]
    args.scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.scratch) as raw:
        existing_signed = (
            ROOT
            / "conformance/vectors/migration/040-verification-fingerprint-excludes-the-signature"
        )
        if existing_signed.is_dir():
            signed = json.loads((existing_signed / "map.json").read_text())
            public = json.loads((existing_signed / "keys.json").read_text())["signer"]
            if {key: value for key, value in signed.items() if key != "signature"} != document:
                signed, public = sign(document, Path(raw))
        else:
            signed, public = sign(document, Path(raw))
        for name, changes, error in cases:
            case_map = copy.deepcopy(document)
            body = copy.deepcopy(request)
            body.update(changes)
            want: dict[str, Any] = {
                "project_id": PROJECT,
                "request": body,
                "group": "Reading",
                "at": DONE,
                "chunk_rows": 3,
            }
            extra: dict[str, Any] = {}
            key = None
            if name.startswith("030"):
                del want["project_id"]
            if name.startswith("031"):
                want["at"] = "2000-01-01T00:00:00Z"
            if name.startswith("039"):
                case_map["groups"]["Reading"]["source"]["layout"]["tables"]["Reading"] = (
                    "other_reading"
                )
            if name.startswith(("040", "041")):
                import base64

                case_map = copy.deepcopy(signed)
                if name.startswith("041"):
                    case_map["signature"]["key_id"] = "a-different-untrusted-hint"
                key = base64.b64decode(public)
                extra = {
                    "keys.json": {"signer": public},
                    "load.json": {"public_key": "signer", "require_signature": True},
                }
                body["requires_signature"] = True
            loaded = sde.load_map(case_map, model=model, public_key=key)
            engines = engines_from(engine_document)
            session = sde.Session(model, loaded, engines, project_id=want.get("project_id"))
            if error:
                try:
                    parsed = sde.VerificationRequest.from_record(body)
                    sde.verify(session, "Reading", request=parsed, at=want["at"], chunk_rows=3)
                except sde.MigrationRefused as caught:
                    assert error in str(caught), (name, caught)
                else:
                    raise AssertionError(f"{name} did not refuse")
                want.update(error="MigrationRefused", match=error)
            else:
                report = sde.verify(
                    session,
                    "Reading",
                    request=sde.VerificationRequest.from_record(body),
                    at=DONE,
                    chunk_rows=3,
                )
                want.update(report=report.as_record(), matched=report.matched)
            calls = next(iter(engines.values())).recorded.as_list()
            output = {
                "model.json": model_document,
                "map.json": case_map,
                "engines.json": engine_document,
                "verification.json": want,
                "calls.json": calls,
                **extra,
            }
            destination = ROOT / "conformance/vectors/migration" / name
            destination.mkdir(exist_ok=True)
            for filename, contents in output.items():
                (destination / filename).write_text(
                    json.dumps(contents, indent=2, ensure_ascii=False) + "\n"
                )
    print(f"Wrote {len(cases)} verification vectors; original vectors were not regenerated")


if __name__ == "__main__":
    main()

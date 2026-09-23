#!/usr/bin/env python3
"""Generate signed staging and cutover cases for placement map contract 5, without SDK imports.

A physical design reaches a client through the staging packet first: the fresh copy is the one
place a new layout can appear, so the prepared map may raise the contract from 4 to 5 while the
current map stays where it is. Cutover then carries three candidates of one contract. These cases
pin both rules and the refusals around them, signed and verified by OpenSSL over canonical bytes
this tool encodes itself (the fixture encoder of ``nfc_map_vectors``), so neither library is the
author of its own expectations.

    python conformance/tools/physical_packet_vectors.py --i-am-changing-the-contract \
        --scratch-directory /path/outside/the/repository
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from cutover_vectors import before_map
from nfc_map_vectors import openssl, payload

ROOT = Path(__file__).resolve().parents[2]
VECTORS = ROOT / "conformance/vectors"
PROJECT, STAGE = "1" * 32, "5" * 32

# A design the ClickHouse copy can carry for the Event entity, whose key is `id` alone: two
# data-skipping indexes. No partition - `at` is outside the key, and a partition outside the key is
# the refusal errors/055 pins.
DESIGN = [
    {
        "entity": "Event",
        "name": "event_at",
        "columns": ["at"],
        "method": "minmax",
        "granularity": 4,
    },
    {
        "entity": "Event",
        "name": "event_name",
        "columns": ["name"],
        "method": "bloom_filter",
        "granularity": 1,
    },
]


def _source_only(document: dict[str, Any]) -> dict[str, Any]:
    document["groups"]["Event"].pop("derived")
    document["groups"]["Event"].pop("also_write")
    return document


def staging_cases() -> list[tuple[str, dict[str, Any], bool]]:
    current = _source_only(before_map())
    prepared = copy.deepcopy(current)
    prepared["contract"] = 5
    prepared["map_version"] = 2
    target = copy.deepcopy(before_map()["groups"]["Event"]["derived"][0])
    target["layout"]["tables"]["Event"] = "sde_m_" + STAGE + "_000001"
    target["layout"]["indexes"] = copy.deepcopy(DESIGN)
    prepared["groups"]["Event"].update(derived=[target], also_write=[target["id"]])
    template: dict[str, Any] = {
        "kind": "sde-stage",
        "protocol": 1,
        "stage_id": STAGE,
        "project_id": PROJECT,
        "group": "Event",
        "current": current,
        "prepared": prepared,
    }
    out: list[tuple[str, dict[str, Any], bool]] = []
    out.append(
        ("133-staging-raises-the-contract-for-a-designed-copy", copy.deepcopy(template), False)
    )

    lowered = copy.deepcopy(template)
    lowered["current"]["contract"] = 5
    lowered["prepared"]["contract"] = 4
    del lowered["prepared"]["groups"]["Event"]["derived"][0]["layout"]["indexes"]
    out.append(("134-staging-cannot-lower-the-contract", lowered, True))

    off_key = copy.deepcopy(template)
    off_key["prepared"]["groups"]["Event"]["derived"][0]["layout"]["partition_by"] = {
        "Event": {"field": "at", "granularity": "month"}
    }
    out.append(("135-staging-refuses-a-design-the-model-rejects", off_key, True))
    return out


def cutover_template() -> dict[str, Any]:
    before = before_map()
    before["contract"] = 5
    before["groups"]["Event"]["derived"][0]["layout"]["indexes"] = copy.deepcopy(DESIGN)
    success, abort = copy.deepcopy(before), copy.deepcopy(before)
    target = copy.deepcopy(before["groups"]["Event"]["derived"][0])
    del target["lag_budget_ms"]
    target["id"] = "source@ch-1"
    success["groups"]["Event"] = {"write_epoch": 3, "source": target}
    abort["groups"]["Event"] = {
        "write_epoch": 2,
        "source": copy.deepcopy(before["groups"]["Event"]["source"]),
    }
    for value, version in ((success, 2), (abort, 3)):
        value["map_version"] = version
        value["routing"] = {"12f8b2171bc2bf78": "Order@pg"}
    return {
        "kind": "sde-cutover",
        "protocol": 1,
        "plan_id": "3" * 32,
        "project_id": PROJECT,
        "group": "Event",
        "pause_budget_ms": 5000,
        "query_impact_digest": "a" * 64,
        "before": before,
        "success": success,
        "abort": abort,
    }


def cutover_cases() -> list[tuple[str, dict[str, Any], str | None]]:
    template = cutover_template()
    out: list[tuple[str, dict[str, Any], str | None]] = [
        ("136-cutover-activates-a-designed-copy-under-contract-5", copy.deepcopy(template), None)
    ]
    mixed = copy.deepcopy(template)
    mixed["abort"]["contract"] = 4
    out.append(
        ("137-cutover-candidates-share-one-contract", mixed, "cannot change other map attributes")
    )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-am-changing-the-contract", action="store_true", required=True)
    parser.add_argument("--scratch-directory", type=Path, required=True)
    args = parser.parse_args()
    scratch = args.scratch_directory.resolve()
    if not scratch.is_dir() or scratch.is_relative_to(ROOT):
        parser.error("use an existing scratch directory outside the repository")
    model = json.loads((VECTORS / "routing/002-dual-write-fan-out/model.json").read_text())
    with tempfile.TemporaryDirectory(prefix="sde-physical-packets-", dir=scratch) as tmp:
        work = Path(tmp)
        private, public = work / "key.pem", work / "public.pem"
        openssl("genpkey", "-algorithm", "ED25519", "-out", str(private))
        openssl("pkey", "-in", str(private), "-pubout", "-out", str(public))
        der = openssl("pkey", "-in", str(private), "-pubout", "-outform", "DER")
        assert len(der) == 44 and der[:12] == bytes.fromhex("302a300506032b6570032100")
        public_key = base64.b64encode(der[12:]).decode()

        def sign(document: dict[str, Any], key_id: str) -> None:
            document.pop("signature", None)
            (work / "payload").write_bytes(payload(document))
            openssl(
                "pkeyutl",
                "-sign",
                "-rawin",
                "-inkey",
                str(private),
                "-in",
                str(work / "payload"),
                "-out",
                str(work / "sig"),
            )
            openssl(
                "pkeyutl",
                "-verify",
                "-rawin",
                "-pubin",
                "-inkey",
                str(public),
                "-in",
                str(work / "payload"),
                "-sigfile",
                str(work / "sig"),
            )
            document["signature"] = {
                "alg": "ed25519",
                "key_id": key_id,
                "value": base64.b64encode((work / "sig").read_bytes()).decode(),
            }

        def write(name: str, files: dict[str, Any]) -> None:
            directory = VECTORS / "migration" / name
            directory.mkdir(exist_ok=True)
            for filename, value in files.items():
                (directory / filename).write_text(
                    json.dumps(value, indent=2, ensure_ascii=False) + "\n"
                )

        for name, plan, bad in staging_cases():
            for field in ("current", "prepared"):
                sign(plan[field], "staging")
            sign(plan, "staging")
            expected: dict[str, Any] = {"project_id": PROJECT}
            if bad:
                expected["error"] = "MigrationRefused"
            else:
                expected.update(
                    stage_fingerprint=hashlib.sha256(payload(plan)).hexdigest(),
                    verified_with="staging",
                    map_fingerprints={
                        field: hashlib.sha256(payload(plan[field])).hexdigest()
                        for field in ("current", "prepared")
                    },
                    tables=plan["prepared"]["groups"]["Event"]["derived"][0]["layout"]["tables"],
                )
            write(
                name,
                {
                    "model.json": model,
                    "plan.json": plan,
                    "keys.json": {"staging": public_key},
                    "staging.json": expected,
                },
            )

        for name, plan, message in cutover_cases():
            for candidate in ("before", "success", "abort"):
                sign(plan[candidate], "cutover")
            plan["verification"] = {
                "protocol": 1,
                "request_id": "2" * 32,
                "project_id": PROJECT,
                "model_version": plan["before"]["model_version"],
                "map_version": plan["before"]["map_version"],
                "map_fingerprint": hashlib.sha256(payload(plan["before"])).hexdigest(),
                "group": "Event",
                "source": {"engine": "pg-main", "id": "Event@pg"},
                "targets": [{"engine": "ch-1", "id": "Event@ch"}],
                "requested_at": "2026-09-23T18:00:00Z",
                "requires_signature": True,
            }
            sign(plan, "cutover")
            expected = {"project_id": PROJECT}
            if message:
                expected.update(error="MigrationRefused", match=message)
            else:
                expected.update(
                    plan_fingerprint=hashlib.sha256(payload(plan)).hexdigest(),
                    verified_with="cutover",
                    source_epoch=1,
                    maintenance_epoch=2,
                    activation_epoch=3,
                    candidate_fingerprints={
                        key: hashlib.sha256(payload(plan[key])).hexdigest()
                        for key in ("before", "success", "abort")
                    },
                )
            write(
                name,
                {
                    "model.json": model,
                    "plan.json": plan,
                    "keys.json": {"cutover": public_key},
                    "cutover.json": expected,
                },
            )
    print("Wrote five independently signed contract-5 staging and cutover fixtures")


if __name__ == "__main__":
    main()

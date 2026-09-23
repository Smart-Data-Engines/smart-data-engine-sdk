#!/usr/bin/env python3
"""Signed cutover packets with explicit maps/expectations and OpenSSL, without SDK imports."""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nfc_map_vectors import openssl, payload

ROOT = Path(__file__).resolve().parents[2]
VECTORS = ROOT / "conformance" / "vectors"
PROJECT = "1" * 32


def before_map() -> dict[str, Any]:
    return {
        "contract": 4,
        "project_id": PROJECT,
        "model_version": "59a263d15793fb78",
        "map_version": 1,
        "groups": {
            "Event": {
                "write_epoch": 1,
                "source": {
                    "engine": "pg-main",
                    "id": "Event@pg",
                    "layout": {
                        "tables": {"Event": "event_source"},
                        "columns": {"Event": {"at": "timestamptz", "id": "uuid", "name": "text"}},
                    },
                },
                "derived": [
                    {
                        "engine": "ch-1",
                        "id": "Event@ch",
                        "lag_budget_ms": 30000,
                        "layout": {
                            "tables": {"Event": "event_copy"},
                            "columns": {
                                "Event": {
                                    "at": "DateTime64(6, 'UTC')",
                                    "id": "UUID",
                                    "name": "String",
                                },
                            },
                        },
                    }
                ],
                "also_write": ["Event@ch"],
            },
            "Order": {
                "write_epoch": 7,
                "source": {
                    "engine": "pg-main",
                    "id": "Order@pg",
                    "layout": {
                        "tables": {"Order": "order", "Payment": "payment", "User": "user"},
                        "columns": {
                            "Order": {
                                "id": "uuid",
                                "placed_at": "timestamptz",
                                "tenant": "uuid",
                                "total": "numeric(12,2)",
                                "user_id": "uuid",
                            },
                            "Payment": {"amount": "numeric(12,2)", "id": "uuid"},
                            "User": {"email": "text", "id": "uuid"},
                        },
                    },
                },
            },
        },
        "routing": {"2477d087f39d0ae4": "Event@pg", "12f8b2171bc2bf78": "Order@pg"},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-am-changing-the-contract", action="store_true", required=True)
    parser.add_argument("--scratch-directory", type=Path, required=True)
    args = parser.parse_args()
    scratch = args.scratch_directory.resolve()
    if not scratch.is_dir() or scratch.is_relative_to(ROOT):
        parser.error("use an existing scratch directory outside the repository")
    model = json.loads((VECTORS / "routing/002-dual-write-fan-out/model.json").read_text())
    before = before_map()
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
    template: dict[str, Any] = {
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
    cases: list[tuple[str, Callable[[dict[str, Any]], None] | None, str | None]] = []

    def case(
        name: str,
        mutate: Callable[[dict[str, Any]], None] | None = None,
        message: str | None = None,
    ) -> None:
        cases.append((name, mutate, message))

    case("079-cutover-packet-authorizes-one-group")
    case(
        "080-cutover-packet-refuses-other-local-project",
        lambda p: p.update(project_id="9" * 32),
        "another locally configured project",
    )
    case(
        "081-cutover-packet-orders-candidate-versions",
        lambda p: p["success"].update(map_version=1),
        "versions must increase",
    )
    case(
        "082-cutover-abort-needs-decision-generation",
        lambda p: p["abort"]["groups"]["Event"].update(write_epoch=1),
        "decision generation",
    )
    case(
        "083-cutover-success-needs-activation-generation",
        lambda p: p["success"]["groups"]["Event"].update(write_epoch=2),
        "decision generation",
    )
    case(
        "084-cutover-keeps-authorized-target-layout",
        lambda p: p["success"]["groups"]["Event"]["source"]["layout"]["tables"].update(
            Event="another_copy"
        ),
        "authorized physical materialization",
    )
    case(
        "085-cutover-preserves-other-group",
        lambda p: p["success"]["groups"]["Order"].update(write_epoch=8),
        "unaffected group",
    )
    case(
        "086-cutover-preserves-other-routing",
        lambda p: p["success"].update(routing={}),
        "terminal routing",
    )
    case(
        "087-cutover-refuses-unbound-request",
        lambda p: p.update(bad_request=True),
        "does not match this session",
    )
    case(
        "088-cutover-refuses-unknown-instructions",
        lambda p: p.update(endpoint="not-an-instruction-field"),
        "missing or unknown fields",
    )
    case(
        "089-cutover-before-reads-stay-on-source",
        lambda p: p["before"]["routing"].update({"2477d087f39d0ae4": "Event@ch"}),
        "reads must still use the source",
    )
    case(
        "090-cutover-checks-envelope-signature",
        lambda p: p.update(bad_envelope=True),
        "signature does not verify",
    )
    case(
        "091-cutover-checks-candidate-signature",
        lambda p: p.update(bad_candidate=True),
        "signature does not verify",
    )
    case(
        "092-cutover-refuses-unknown-protocol",
        # 3, not 2: protocol 2 is the relayout, and a vector about an unknown number must name one.
        lambda p: p.update(protocol=3),
        "unsupported cutover plan protocol",
    )

    def exhausted(p: dict[str, Any]) -> None:
        p["before"]["groups"]["Event"]["write_epoch"] = 9007199254740990
        p["success"]["groups"]["Event"]["write_epoch"] = 9007199254740991
        p["abort"]["groups"]["Event"]["write_epoch"] = 9007199254740991

    case(
        "093-cutover-needs-two-available-generations", exhausted, "two available write generations"
    )
    case(
        "094-cutover-refuses-an-unbounded-pause",
        lambda p: p.update(pause_budget_ms=0),
        "positive safe integer",
    )
    case(
        "095-cutover-refuses-malformed-derived-list",
        lambda p: p["before"]["groups"]["Event"].update(derived=None),
        "derived copies must be an array",
    )
    case(
        "096-cutover-requires-explicit-layouts",
        lambda p: p["before"]["groups"]["Event"]["source"].update(
            layout={"auto": True, "dialect": "postgres"}
        ),
        "explicit physical layouts",
    )
    case(
        "097-cutover-requires-signed-verification",
        lambda p: p.update(weak_request=True),
        "must require the signed before map",
    )

    def extra_copy(p: dict[str, Any]) -> None:
        copy_ = copy.deepcopy(p["before"]["groups"]["Event"]["derived"][0])
        copy_["id"], copy_["engine"] = "Event@extra", "ch-extra"
        copy_["layout"]["tables"]["Event"] = "event_extra"
        p["before"]["groups"]["Event"]["derived"].append(copy_)

    case("098-cutover-refuses-additional-group-copies", extra_copy, "exactly one derived copy")

    def relayout(p: dict[str, Any]) -> None:
        """The same group, a copy in the source's own engine under a fresh name: protocol 2."""
        source = p["before"]["groups"]["Event"]["source"]
        copy_ = {
            "engine": source["engine"],
            "id": "Event@pg-relayout",
            "lag_budget_ms": 30000,
            "layout": {
                "tables": {"Event": "event_relayout"},
                "columns": copy.deepcopy(source["layout"]["columns"]),
            },
        }
        p["before"]["groups"]["Event"].update(derived=[copy_], also_write=[copy_["id"]])
        target = {key: value for key, value in copy_.items() if key != "lag_budget_ms"}
        target["id"] = "source@pg-relayout"
        p["success"]["groups"]["Event"]["source"] = target
        p["protocol"] = 2
        p["_verification_targets"] = [{"engine": copy_["engine"], "id": copy_["id"]}]

    case("140-cutover-relayout-activates-a-copy-in-the-same-engine", relayout)
    case(
        "141-cutover-relayout-needs-the-same-binding",
        lambda p: p.update(protocol=2),
        "own engine binding",
    )

    def move_in_one_engine(p: dict[str, Any]) -> None:
        relayout(p)
        p["protocol"] = 1

    case(
        "142-cutover-move-still-needs-another-binding",
        move_in_one_engine,
        "different engine bindings",
    )
    with tempfile.TemporaryDirectory(prefix="sde-cutover-packets-", dir=scratch) as tmp:
        work = Path(tmp)
        private, public = work / "key.pem", work / "public.pem"
        openssl("genpkey", "-algorithm", "ED25519", "-out", str(private))
        openssl("pkey", "-in", str(private), "-pubout", "-out", str(public))
        der = openssl("pkey", "-in", str(private), "-pubout", "-outform", "DER")
        assert len(der) == 44 and der[:12] == bytes.fromhex("302a300506032b6570032100")

        def sign(document: dict[str, Any]) -> None:
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
                "key_id": "cutover",
                "value": base64.b64encode((work / "sig").read_bytes()).decode(),
            }

        for name, mutate, message in cases:
            plan = copy.deepcopy(template)
            if mutate:
                mutate(plan)
            bad_request = plan.pop("bad_request", False)
            weak_request = plan.pop("weak_request", False)
            bad_envelope = plan.pop("bad_envelope", False)
            bad_candidate = plan.pop("bad_candidate", False)
            targets = plan.pop("_verification_targets", [{"engine": "ch-1", "id": "Event@ch"}])
            for candidate in ("before", "success", "abort"):
                sign(plan[candidate])
            if bad_candidate:
                plan["success"]["signature"]["value"] = base64.b64encode(bytes(64)).decode()
            plan["verification"] = {
                "protocol": 1,
                "request_id": "2" * 32,
                "project_id": PROJECT,
                "model_version": before["model_version"],
                "map_version": plan["before"]["map_version"],
                "map_fingerprint": "0" * 64
                if bad_request
                else hashlib.sha256(payload(plan["before"])).hexdigest(),
                "group": "Event",
                "source": {"engine": "pg-main", "id": "Event@pg"},
                "targets": targets,
                "requested_at": "2026-09-13T18:00:00Z",
                "requires_signature": not weak_request,
            }
            sign(plan)
            if bad_envelope:
                plan["signature"]["value"] = base64.b64encode(bytes(64)).decode()
            expected: dict[str, Any] = {"project_id": PROJECT}
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
            directory = VECTORS / "migration" / name
            directory.mkdir(exist_ok=True)
            for filename, value in {
                "model.json": model,
                "plan.json": plan,
                "keys.json": {"cutover": base64.b64encode(der[12:]).decode()},
                "cutover.json": expected,
            }.items():
                (directory / filename).write_text(
                    json.dumps(value, indent=2, ensure_ascii=False) + "\n"
                )
    print(f"Wrote {len(cases)} independently signed cutover packet fixtures")


if __name__ == "__main__":
    main()

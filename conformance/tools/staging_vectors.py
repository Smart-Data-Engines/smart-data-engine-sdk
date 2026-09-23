#!/usr/bin/env python3
"""Generate signed staging cases with explicit expectations and OpenSSL, without SDK imports."""

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

from cutover_vectors import before_map
from nfc_map_vectors import openssl, payload

ROOT = Path(__file__).resolve().parents[2]
VECTORS = ROOT / "conformance/vectors"
PROJECT, STAGE = "1" * 32, "5" * 32


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-am-changing-the-contract", action="store_true", required=True)
    parser.add_argument("--scratch-directory", type=Path, required=True)
    args = parser.parse_args()
    scratch = args.scratch_directory.resolve()
    if not scratch.is_dir() or scratch.is_relative_to(ROOT):
        parser.error("use an existing scratch directory outside the repository")
    model = json.loads((VECTORS / "routing/002-dual-write-fan-out/model.json").read_text())
    current = before_map()
    current["groups"]["Event"].pop("derived")
    current["groups"]["Event"].pop("also_write")
    prepared = copy.deepcopy(current)
    prepared["map_version"] = 2
    target = copy.deepcopy(before_map()["groups"]["Event"]["derived"][0])
    target["layout"]["tables"]["Event"] = "sde_m_" + STAGE + "_000001"
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
    cases: list[tuple[str, Callable[[dict[str, Any]], None] | None, bool]] = []

    def case(
        name: str, change: Callable[[dict[str, Any]], None] | None = None, bad: bool = True
    ) -> None:
        cases.append((name, change, bad))

    case("099-staging-authorizes-one-fresh-copy", bad=False)
    case("100-staging-refuses-another-local-project", lambda p: p.update(project_id="9" * 32))
    case("101-staging-needs-a-newer-map", lambda p: p["prepared"].update(map_version=1))
    case(
        "102-staging-keeps-current-source",
        lambda p: p["prepared"]["groups"]["Event"]["source"]["layout"]["tables"].update(
            Event="another_source"
        ),
    )
    case(
        "103-staging-keeps-write-generation",
        lambda p: p["prepared"]["groups"]["Event"].update(write_epoch=2),
    )
    case(
        "104-staging-keeps-other-groups",
        lambda p: p["prepared"]["groups"]["Order"].update(write_epoch=8),
    )
    case("105-staging-keeps-routing", lambda p: p["prepared"].update(routing={}))
    case(
        "106-staging-copy-needs-fanout",
        lambda p: p["prepared"]["groups"]["Event"].update(also_write=[]),
    )

    def two_copies(p: dict[str, Any]) -> None:
        other = copy.deepcopy(p["prepared"]["groups"]["Event"]["derived"][0])
        other["id"], other["engine"] = "extra", "ch-extra"
        other["layout"]["tables"]["Event"] = "extra_copy"
        p["prepared"]["groups"]["Event"]["derived"].append(other)

    case("107-staging-refuses-extra-copies", two_copies)

    def existing_copy(p: dict[str, Any]) -> None:
        p["current"]["groups"]["Event"].update(
            derived=copy.deepcopy(p["prepared"]["groups"]["Event"]["derived"]),
            also_write=["Event@ch"],
        )

    case("108-staging-needs-source-only-current", existing_copy)
    case(
        "109-staging-needs-another-binding",
        lambda p: p["prepared"]["groups"]["Event"]["derived"][0].update(engine="pg-main"),
    )
    case(
        "110-staging-name-binds-stage-id",
        lambda p: p["prepared"]["groups"]["Event"]["derived"][0]["layout"]["tables"].update(
            Event="sde_m_" + "9" * 32 + "_000001"
        ),
    )

    def renamed(p: dict[str, Any]) -> None:
        columns = p["prepared"]["groups"]["Event"]["derived"][0]["layout"]["columns"]["Event"]
        columns["label"] = columns.pop("name")

    case("111-staging-columns-match-model", renamed)
    case(
        "112-staging-refuses-auto-layout",
        lambda p: p["prepared"]["groups"]["Event"]["derived"][0].update(
            layout={"auto": True, "dialect": "clickhouse"}
        ),
    )
    case("113-staging-model-is-bound", lambda p: p["prepared"].update(model_version="0" * 16))
    case("114-staging-verifies-envelope", lambda p: p.update(bad_envelope=True))
    case("115-staging-verifies-both-maps", lambda p: p.update(bad_map=True))
    # 3, not 2: protocol 2 is the relayout, and a vector about an unknown number must name one.
    case("116-staging-refuses-unknown-protocol", lambda p: p.update(protocol=3))
    case("117-staging-group-must-exist", lambda p: p.update(group="Missing"))

    def exhausted(p: dict[str, Any]) -> None:
        for name in ("current", "prepared"):
            p[name]["groups"]["Event"]["write_epoch"] = 9007199254740990

    case("118-staging-reserves-cutover-generations", exhausted)
    case("119-staging-refuses-extra-instructions", lambda p: p.update(sql="arbitrary instruction"))
    case("120-staging-current-must-be-signed", lambda p: p.update(unsigned_current=True))

    def three_entities(p: dict[str, Any]) -> None:
        p["group"] = "Order"
        p["prepared"]["groups"]["Event"] = copy.deepcopy(p["current"]["groups"]["Event"])
        columns = copy.deepcopy(p["current"]["groups"]["Order"]["source"]["layout"]["columns"])
        converted = {
            "uuid": "UUID",
            "text": "String",
            "timestamptz": "DateTime64(6, 'UTC')",
            "numeric(12,2)": "Decimal(12, 2)",
        }
        for fields in columns.values():
            for key, value in fields.items():
                fields[key] = converted[value]
        copy_ = {
            "id": "Order@ch",
            "engine": "ch-1",
            "lag_budget_ms": 1000,
            "layout": {
                "tables": {
                    entity: "sde_m_" + STAGE + f"_{index:06d}"
                    for index, entity in enumerate(("Order", "Payment", "User"), 1)
                },
                "columns": columns,
            },
        }
        p["prepared"]["groups"]["Order"].update(derived=[copy_], also_write=["Order@ch"])

    case("121-staging-names-colocated-entities-in-order", three_entities, False)

    def relayout(p: dict[str, Any]) -> None:
        """A fresh copy in the source's own engine, under the stage's name: protocol 2."""
        source = p["current"]["groups"]["Event"]["source"]
        target = p["prepared"]["groups"]["Event"]["derived"][0]
        target["engine"] = source["engine"]
        target["layout"]["columns"] = copy.deepcopy(source["layout"]["columns"])
        p["protocol"] = 2

    case("138-staging-relayout-authorizes-a-copy-in-the-same-engine", relayout, False)
    case("139-staging-relayout-needs-the-same-binding", lambda p: p.update(protocol=2))
    with tempfile.TemporaryDirectory(prefix="sde-staging-vectors-", dir=scratch) as tmp:
        work = Path(tmp)
        private, public = work / "key.pem", work / "public.pem"
        openssl("genpkey", "-algorithm", "ED25519", "-out", str(private))
        openssl("pkey", "-in", str(private), "-pubout", "-out", str(public))
        der = openssl("pkey", "-in", str(private), "-pubout", "-outform", "DER")
        assert len(der) == 44 and der[:12] == bytes.fromhex("302a300506032b6570032100")

        def sign(document: dict[str, Any]) -> None:
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
                "key_id": "staging",
                "value": base64.b64encode((work / "sig").read_bytes()).decode(),
            }

        for name, change, bad in cases:
            plan = copy.deepcopy(template)
            if change:
                change(plan)
            bad_envelope = plan.pop("bad_envelope", False)
            bad_map = plan.pop("bad_map", False)
            unsigned = plan.pop("unsigned_current", False)
            for field in ("current", "prepared"):
                sign(plan[field])
            if bad_map:
                plan["prepared"]["signature"]["value"] = base64.b64encode(bytes(64)).decode()
            if unsigned:
                del plan["current"]["signature"]
            sign(plan)
            if bad_envelope:
                plan["signature"]["value"] = base64.b64encode(bytes(64)).decode()
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
                    tables=plan["prepared"]["groups"][plan["group"]]["derived"][0]["layout"][
                        "tables"
                    ],
                )
            directory = VECTORS / "migration" / name
            directory.mkdir(exist_ok=True)
            for filename, value in {
                "model.json": model,
                "plan.json": plan,
                "keys.json": {
                    "staging": base64.b64decode(der[12:]).decode()
                    if False
                    else base64.b64encode(der[12:]).decode()
                },
                "staging.json": expected,
            }.items():
                (directory / filename).write_text(
                    json.dumps(value, indent=2, ensure_ascii=False) + "\n"
                )
    print(f"Wrote {len(cases)} independently signed staging fixtures")


if __name__ == "__main__":
    main()

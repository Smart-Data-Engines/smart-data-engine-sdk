#!/usr/bin/env python3
"""Explicit frozen-comparison fixtures, composed from already pinned logical scan traces."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

P = "1" * 32
H = "6" * 32
AT = "2026-09-12T12:00:00Z"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--i-am-changing-the-contract", action="store_true", required=True
    )
    parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "vectors" / "migration"
    base = root / "068-generation-verification-compares-data-not-epochs"

    def read(name: str) -> Any:
        return json.loads((base / (name + ".json")).read_text())

    model, placement, engines, generation = (
        read("model"),
        read("map"),
        read("engines"),
        read("generation"),
    )
    scan_calls = read("calls")
    spot = placement["groups"]["Reading"]
    source = spot["source"]
    target = spot["derived"][0]
    payload = json.dumps(
        placement, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    assert payload.isascii(), (
        "the independent fixture encoder is scoped to ASCII map inputs"
    )
    request = {
        "protocol": 1,
        "request_id": "7" * 32,
        "project_id": P,
        "model_version": placement["model_version"],
        "map_version": placement["map_version"],
        "map_fingerprint": hashlib.sha256(payload).hexdigest(),
        "group": "Reading",
        "source": {"engine": source["engine"], "id": source["id"]},
        "targets": [{"engine": target["engine"], "id": target["id"]}],
        "requested_at": AT,
        "requires_signature": False,
    }
    epochs = {source["id"]: 2, target["id"]: 2}
    barriers: list[dict[str, Any]] = []
    prefix: list[dict[str, Any]] = []
    for material in sorted(
        (source, target), key=lambda item: (item["engine"], item["id"])
    ):
        for table in sorted(material["layout"]["tables"].values()):
            barriers.append(
                {
                    "engine": material["engine"],
                    "materialization": material["id"],
                    "table": table,
                    "identity": material["engine"] + "/" + table,
                    "project_id": P,
                    "epoch": 2,
                    "hold_id": H,
                }
            )
            prefix.extend(
                [
                    {
                        "engine": material["engine"],
                        "call": "fence_add",
                        "table": table,
                        "arguments": ["__sde_f_hold_" + H, "0"],
                    },
                    {
                        "engine": material["engine"],
                        "call": "fence_drain",
                        "table": table,
                        "arguments": [P, H],
                    },
                ]
            )
    comparison = copy.deepcopy(generation["actions"][0]["report"])
    comparison["request"] = request
    cases: list[tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]] = []

    def case(
        name: str,
        *,
        extra: bool = False,
        bad_epochs: dict[str, int] | None = None,
        bad_request: dict[str, Any] | None = None,
        message: str | None = None,
        retry: bool = False,
    ) -> None:
        state = copy.deepcopy(engines)
        if extra:
            state[target["engine"]]["tables"]["reading"].append(
                {"id": 99, "celsius": 99, "__sde_write_epoch": 2}
            )
        result = copy.deepcopy(comparison)
        if extra:
            result["rows_target"] = 2
        want: dict[str, Any] = {
            "project_id": P,
            "engine_generations": generation["engine_generations"],
            "group": "Reading",
            "request": request if bad_request is None else bad_request,
            "hold_id": H,
            "epochs": epochs if bad_epochs is None else bad_epochs,
            "at": AT,
            "retry": retry,
        }
        calls = prefix + scan_calls
        if message is not None:
            want.update(error="MigrationRefused", match=message)
            calls = []
        else:
            want["report"] = {
                "protocol": 1,
                "comparison": result,
                "barriers": barriers,
                "elapsed_ms": "<measured>",
                "matched": not extra,
            }
            if retry:
                calls = calls + calls
        cases.append((name, state, want, calls))

    case("071-frozen-comparison-closes-before-reading")
    case("072-frozen-comparison-refuses-target-extras", extra=True)
    case(
        "073-frozen-comparison-needs-exact-epoch-coverage",
        bad_epochs={source["id"]: 2},
        message="epochs must name exactly",
    )
    case(
        "074-frozen-comparison-refuses-inactive-generation",
        bad_epochs={source["id"]: 2, target["id"]: 3},
        message="expected write generation",
    )
    bad = copy.deepcopy(request)
    bad["project_id"] = "9" * 32
    case(
        "075-frozen-comparison-refuses-other-request-project",
        bad_request=bad,
        message="another project",
    )
    case("076-frozen-comparison-redrains-on-retry", retry=True)
    changed = copy.deepcopy(engines)
    changed[target["engine"]]["tables"]["reading"][0]["celsius"] = 99
    altered_report = copy.deepcopy(comparison)
    altered_report["chunks_mismatched"] = 1
    differing_calls = copy.deepcopy(scan_calls)
    target_scan = next(
        i
        for i, item in enumerate(differing_calls)
        if item["engine"] == target["engine"] and item["call"] == "key_range"
    )
    differing_calls.insert(
        target_scan + 1, {"engine": target["engine"], "call": "get", "table": "reading"}
    )
    cases.append(
        (
            "077-frozen-comparison-needs-values-as-well-as-counts",
            changed,
            {
                "project_id": P,
                "engine_generations": generation["engine_generations"],
                "group": "Reading",
                "request": request,
                "hold_id": H,
                "epochs": epochs,
                "at": AT,
                "retry": False,
                "report": {
                    "protocol": 1,
                    "comparison": altered_report,
                    "barriers": barriers,
                    "elapsed_ms": "<measured>",
                    "matched": False,
                },
            },
            prefix + differing_calls,
        )
    )
    case(
        "078-frozen-comparison-refuses-unrelated-epoch",
        bad_epochs={**epochs, "unrelated": 2},
        message="epochs must name exactly",
    )
    for name, state, want, calls in cases:
        directory = root / name
        directory.mkdir(exist_ok=True)
        for filename, body in (
            ("model.json", model),
            ("map.json", placement),
            ("engines.json", state),
            ("frozen.json", want),
            ("calls.json", calls),
        ):
            (directory / filename).write_text(
                json.dumps(body, ensure_ascii=False, indent=2) + "\n"
            )
    print(f"Wrote {len(cases)} frozen verification fixtures without an SDK import")


if __name__ == "__main__":
    main()

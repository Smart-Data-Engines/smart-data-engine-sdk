#!/usr/bin/env python3
"""Generate the cases that hold one rule of every signed packet: a map in a packet carries no
`auto`, whatever the member says.

A staging, a cutover and an in-place index build refused a layout whose `auto` the loading library
called true, and that is not one question: an empty array is true in JavaScript and is not in
Python. So a map with `"auto": []` beside a layout written out was taken by the Python and C++
loaders and refused by the TypeScript ones. These cases pin the rule that leaves no library to
decide - the generation advance had it from the start (`migration/224`, `225`) - with the two
values the languages disagreed on and agreed on: `[]` and `false`.

Explicit expectations and OpenSSL, no SDK imports; `--only` writes the named cases alone.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nfc_map_vectors import payload
from unfenced_vectors import (
    PROJECT,
    Signer,
    cutover_template,
    dump,
    index_template,
    staging_template,
)

ROOT = Path(__file__).resolve().parents[2]
VECTORS = ROOT / "conformance/vectors"

WORDS = {
    "staging": "staging maps need explicit physical layouts",
    "index": "index build maps need explicit physical layouts",
    "cutover": "cutover maps require explicit physical layouts",
}
TEMPLATES: dict[str, Callable[[], dict[str, Any]]] = {
    "staging": staging_template,
    "index": index_template,
    "cutover": cutover_template,
}
MAPS = {
    "staging": ("current", "prepared"),
    "index": ("current", "prepared"),
    "cutover": ("before", "success", "abort"),
}
# The group whose layout carries the member: one every map of the packet holds unchanged, so that
# no other rule of the packet is the one that refuses.
CARRIER = {"staging": "Event", "index": "Order", "cutover": "Order"}
CASES: list[tuple[str, str, Any]] = [
    ("231-staging-refuses-auto-beside-a-layout", "staging", []),
    ("232-staging-refuses-auto-whatever-it-says", "staging", False),
    ("233-index-build-refuses-auto-beside-a-layout", "index", []),
    ("234-index-build-refuses-auto-whatever-it-says", "index", False),
    ("235-cutover-refuses-auto-beside-a-layout", "cutover", []),
    ("236-cutover-refuses-auto-whatever-it-says", "cutover", False),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-am-changing-the-contract", action="store_true", required=True)
    parser.add_argument("--scratch-directory", type=Path, required=True)
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="write only these vectors (repeatable); every vector carries its own key, so the "
        "others stay byte for byte as they are",
    )
    args = parser.parse_args()
    scratch = args.scratch_directory.resolve()
    if not scratch.is_dir() or scratch.is_relative_to(ROOT):
        parser.error("use an existing scratch directory outside the repository")
    unknown = set(args.only) - {label for label, _, _ in CASES}
    if unknown:
        parser.error(f"no such vector: {sorted(unknown)}")
    model = json.loads((VECTORS / "routing/002-dual-write-fan-out/model.json").read_text())
    written = [case for case in CASES if not args.only or case[0] in args.only]
    with tempfile.TemporaryDirectory(prefix="sde-auto-member-vectors-", dir=scratch) as tmp:
        signers = {kind: Signer(Path(tmp), kind) for kind in TEMPLATES}
        for label, kind, value in written:
            plan = TEMPLATES[kind]()
            for name in MAPS[kind]:
                plan[name]["groups"][CARRIER[kind]]["source"]["layout"]["auto"] = value
                signers[kind].sign(plan[name])
            if kind == "cutover":
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
                    "requested_at": "2026-10-02T18:00:00Z",
                    "requires_signature": True,
                }
            signers[kind].sign(plan)
            directory = VECTORS / "migration" / label
            directory.mkdir(exist_ok=True)
            for filename, document in {
                "model.json": model,
                "plan.json": plan,
                "keys.json": {kind: signers[kind].public_key},
                f"{kind}.json": {
                    "project_id": PROJECT,
                    "error": "MigrationRefused",
                    "match": WORDS[kind],
                },
            }.items():
                dump(directory / filename, document)
    print(f"Wrote {len(written)} signed packets whose maps carry an auto member")


if __name__ == "__main__":
    main()

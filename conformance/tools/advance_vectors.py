#!/usr/bin/env python3
"""Generate signed generation-advance cases with explicit expectations and OpenSSL, no SDK imports.

A packet that advances one group's write generation: the map in force and the map that follows it,
which is the first with a newer version and that group's generation raised by one. Every library
loads it under the same rules and refuses the same packets by the same words.
"""

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
from unfenced_vectors import unfenced_order

ROOT = Path(__file__).resolve().parents[2]
VECTORS = ROOT / "conformance/vectors"
PROJECT, ADVANCE = "1" * 32, "7" * 32
MAX_EPOCH = 9_007_199_254_740_991

# What each refused case's refusal says, in every library. A refusal of a map inside the packet
# keeps the map loader's own words after the common prefix.
ADVANCE_MATCHES: dict[str, str] = {
    "203-advance-refuses-another-local-project": (
        "advance authorization belongs to another local project"
    ),
    "204-advance-needs-a-newer-map": "an advance must allocate a newer map",
    "205-advance-raises-by-exactly-one": (
        "an advance raises the group's write generation by exactly one"
    ),
    "206-advance-cannot-keep-the-generation": (
        "an advance raises the group's write generation by exactly one"
    ),
    "207-advance-keeps-the-source": (
        "an advance cannot change where the group is placed or which copies it maintains"
    ),
    "208-advance-keeps-the-copy": (
        "an advance cannot change where the group is placed or which copies it maintains"
    ),
    "209-advance-keeps-other-groups": "an advance cannot change an unaffected group",
    "210-advance-keeps-routing": "an advance cannot change routing or other map attributes",
    "211-advance-refuses-unknown-protocol": "unsupported advance authorization kind or protocol",
    "212-advance-refuses-another-kind": "unsupported advance authorization kind or protocol",
    "213-advance-group-must-exist": "an advance names a group the map in force does not have",
    "214-advance-refuses-extra-instructions": "advance authorization has missing or unknown fields",
    "215-advance-needs-a-pause-budget": "advance pause_budget_ms must be a positive safe integer",
    "216-advance-verifies-envelope": "advance authorization refused: ",
    "217-advance-verifies-both-maps": "advance authorization refused: ",
    "218-advance-before-must-be-signed": "advance signature must be an object",
    "219-advance-needs-an-available-generation": "an advance needs one available write generation",
    "220-advance-keeps-the-contract": "an advance cannot change routing or other map attributes",
    "221-advance-id-is-32-hex-digits": "advance advance_id must be 32 lowercase hexadecimal digits",
    "222-advance-needs-explicit-layouts": "advance maps need explicit physical layouts",
    "223-advance-needs-every-layout-explicit": "advance maps need explicit physical layouts",
    "224-advance-refuses-auto-beside-a-layout": "advance maps need explicit physical layouts",
    "225-advance-refuses-auto-whatever-it-says": "advance maps need explicit physical layouts",
    "227-advance-refuses-a-group-without-a-generation": (
        "carries no write generation, so there is none to advance"
    ),
    "228-advance-cannot-give-a-group-a-generation": "an advance cannot change an unaffected group",
    "229-advance-cannot-go-back-a-map": "an advance must allocate a newer map",
    "230-advance-maps-are-the-local-projects": (
        "this generation-bearing map needs its locally configured project_id"
    ),
}


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
    model = json.loads((VECTORS / "routing/002-dual-write-fan-out/model.json").read_text())
    before = before_map()
    after = copy.deepcopy(before)
    after["map_version"] = 2
    after["groups"]["Event"]["write_epoch"] = 2
    template: dict[str, Any] = {
        "kind": "sde-advance",
        "protocol": 1,
        "advance_id": ADVANCE,
        "project_id": PROJECT,
        "group": "Event",
        "pause_budget_ms": 30000,
        "before": before,
        "after": after,
    }
    cases: list[tuple[str, Callable[[dict[str, Any]], None] | None, bool]] = []

    def case(
        name: str, change: Callable[[dict[str, Any]], None] | None = None, bad: bool = True
    ) -> None:
        cases.append((name, change, bad))

    case("201-advance-raises-one-group-by-one-generation", bad=False)

    def source_only(p: dict[str, Any]) -> None:
        for name in ("before", "after"):
            p[name]["groups"]["Event"].pop("derived")
            p[name]["groups"]["Event"].pop("also_write")

    case("202-advance-of-a-group-without-a-copy", source_only, False)
    case("203-advance-refuses-another-local-project", lambda p: p.update(project_id="9" * 32))
    case("204-advance-needs-a-newer-map", lambda p: p["after"].update(map_version=1))
    case(
        "205-advance-raises-by-exactly-one",
        lambda p: p["after"]["groups"]["Event"].update(write_epoch=3),
    )
    case(
        "206-advance-cannot-keep-the-generation",
        lambda p: p["after"]["groups"]["Event"].update(write_epoch=1),
    )
    case(
        "207-advance-keeps-the-source",
        lambda p: p["after"]["groups"]["Event"]["source"]["layout"]["tables"].update(
            Event="another_source"
        ),
    )

    def drops_the_copy(p: dict[str, Any]) -> None:
        p["after"]["groups"]["Event"].pop("derived")
        p["after"]["groups"]["Event"].pop("also_write")

    case("208-advance-keeps-the-copy", drops_the_copy)
    case(
        "209-advance-keeps-other-groups",
        lambda p: p["after"]["groups"]["Order"].update(write_epoch=8),
    )
    # A shape of the other group, routed to that group's own source: a map the loader accepts.
    case(
        "210-advance-keeps-routing",
        lambda p: p["after"].update(routing={"12f8b2171bc2bf78": "Order@pg"}),
    )
    case("211-advance-refuses-unknown-protocol", lambda p: p.update(protocol=2))
    case("212-advance-refuses-another-kind", lambda p: p.update(kind="sde-stage"))
    case("213-advance-group-must-exist", lambda p: p.update(group="Missing"))
    case("214-advance-refuses-extra-instructions", lambda p: p.update(statement="DROP TABLE x"))
    case("215-advance-needs-a-pause-budget", lambda p: p.update(pause_budget_ms=0))
    case("216-advance-verifies-envelope", lambda p: p.update(bad_envelope=True))
    case("217-advance-verifies-both-maps", lambda p: p.update(bad_map=True))
    case("218-advance-before-must-be-signed", lambda p: p.update(unsigned_before=True))

    def last_generation(p: dict[str, Any]) -> None:
        p["before"]["groups"]["Event"].update(write_epoch=MAX_EPOCH)
        p["after"]["groups"]["Event"].update(write_epoch=MAX_EPOCH)

    case("219-advance-needs-an-available-generation", last_generation)
    case("220-advance-keeps-the-contract", lambda p: p["after"].update(contract=5))
    case("221-advance-id-is-32-hex-digits", lambda p: p.update(advance_id="7" * 31))

    # A layout left to be derived names no table anybody signed, and the operator fences tables.
    def derived_layout(group: str) -> Callable[[dict[str, Any]], None]:
        def change(p: dict[str, Any]) -> None:
            for name in ("before", "after"):
                p[name]["groups"][group]["source"]["layout"] = {"auto": True}

        return change

    case("222-advance-needs-explicit-layouts", derived_layout("Event"))
    # Of every group, as for a staging and a cutover: the rule is the map's, not the group's.
    case("223-advance-needs-every-layout-explicit", derived_layout("Order"))

    # The member is refused whatever it says, a layout written out beside it or not. Whether a
    # value asks for a derived layout is each language's own notion of a true value - an empty
    # array is one in JavaScript and not in Python - so no library is left to decide it.
    def auto_of(value: Any, group: str) -> Callable[[dict[str, Any]], None]:
        def change(p: dict[str, Any]) -> None:
            for name in ("before", "after"):
                p[name]["groups"][group]["source"]["layout"]["auto"] = value

        return change

    case("224-advance-refuses-auto-beside-a-layout", auto_of([], "Event"))
    case("225-advance-refuses-auto-whatever-it-says", auto_of(False, "Order"))

    # Contract 6: a group whose engine cannot fence writes carries no generation (section 7k). An
    # advance of another group carries it byte for byte, and there is nothing to advance in it.
    def beside_a_group_without_a_generation(p: dict[str, Any]) -> None:
        for name in ("before", "after"):
            unfenced_order(p[name])

    case(
        "226-advance-carries-a-group-without-a-generation",
        beside_a_group_without_a_generation,
        False,
    )

    def of_a_group_without_a_generation(p: dict[str, Any]) -> None:
        beside_a_group_without_a_generation(p)
        p["group"] = "Order"

    case("227-advance-refuses-a-group-without-a-generation", of_a_group_without_a_generation)

    def gives_a_generation(p: dict[str, Any]) -> None:
        beside_a_group_without_a_generation(p)
        p["after"]["groups"]["Order"]["write_epoch"] = 1

    case("228-advance-cannot-give-a-group-a-generation", gives_a_generation)

    def goes_back(p: dict[str, Any]) -> None:
        p["before"]["map_version"] = 5
        p["after"]["map_version"] = 3

    case("229-advance-cannot-go-back-a-map", goes_back)

    # The envelope names the local project and its maps another's: a map is bound to the project
    # the caller configured, never to the one a document says it is for.
    def maps_of_another_project(p: dict[str, Any]) -> None:
        for name in ("before", "after"):
            p[name]["project_id"] = "9" * 32

    case("230-advance-maps-are-the-local-projects", maps_of_another_project)
    with tempfile.TemporaryDirectory(prefix="sde-advance-vectors-", dir=scratch) as tmp:
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
                "pkeyutl", "-sign", "-rawin", "-inkey", str(private),
                "-in", str(work / "payload"), "-out", str(work / "sig"),
            )
            openssl(
                "pkeyutl", "-verify", "-rawin", "-pubin", "-inkey", str(public),
                "-in", str(work / "payload"), "-sigfile", str(work / "sig"),
            )
            document["signature"] = {
                "alg": "ed25519",
                "key_id": "advance",
                "value": base64.b64encode((work / "sig").read_bytes()).decode(),
            }

        unknown = set(args.only) - {name for name, _, _ in cases}
        if unknown:
            parser.error(f"no such vector: {sorted(unknown)}")
        written = [entry for entry in cases if not args.only or entry[0] in args.only]
        for name, change, bad in written:
            plan = copy.deepcopy(template)
            if change:
                change(plan)
            bad_envelope = plan.pop("bad_envelope", False)
            bad_map = plan.pop("bad_map", False)
            unsigned = plan.pop("unsigned_before", False)
            for field in ("before", "after"):
                sign(plan[field])
            if bad_map:
                plan["after"]["signature"]["value"] = base64.b64encode(bytes(64)).decode()
            if unsigned:
                del plan["before"]["signature"]
            sign(plan)
            if bad_envelope:
                plan["signature"]["value"] = base64.b64encode(bytes(64)).decode()
            expected: dict[str, Any] = {"project_id": PROJECT}
            if bad:
                expected["error"] = "MigrationRefused"
                expected["match"] = ADVANCE_MATCHES[name]
            else:
                group = plan["group"]
                expected.update(
                    advance_fingerprint=hashlib.sha256(payload(plan)).hexdigest(),
                    verified_with="advance",
                    map_fingerprints={
                        field: hashlib.sha256(payload(plan[field])).hexdigest()
                        for field in ("before", "after")
                    },
                    write_epochs={
                        field: plan[field]["groups"][group]["write_epoch"]
                        for field in ("before", "after")
                    },
                    pause_budget_ms=plan["pause_budget_ms"],
                )
            directory = VECTORS / "migration" / name
            directory.mkdir(exist_ok=True)
            for filename, value in {
                "model.json": model,
                "plan.json": plan,
                "keys.json": {"advance": base64.b64encode(der[12:]).decode()},
                "advance.json": expected,
            }.items():
                (directory / filename).write_text(
                    json.dumps(value, indent=2, ensure_ascii=False) + "\n"
                )
    print(f"Wrote {len(written)} independently signed advance fixtures")


if __name__ == "__main__":
    main()

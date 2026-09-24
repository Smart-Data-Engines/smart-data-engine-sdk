#!/usr/bin/env python3
"""Generate signed in-place index build cases with explicit expectations, without SDK imports.

An in-place build authorization binds the exact map in force and a next map that differs from it
only by indexes added to one group's source. Every rule of the loader has a case here - three
acceptances and a refusal per rule - and every refusal names a fragment of the message both
libraries must give, so a library that refuses for another reason fails the vector. The documents
are signed and verified by OpenSSL over canonical bytes this tool encodes itself (the fixture
encoder of ``nfc_map_vectors``), so neither library is the author of its own expectations.

    python conformance/tools/index_vectors.py --i-am-changing-the-contract \
        --scratch-directory /path/outside/the/repository
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

ROOT = Path(__file__).resolve().parents[2]
VECTORS = ROOT / "conformance/vectors"
PROJECT, INDEX = "1" * 32, "6" * 32
BUDGET = 3_600_000
KEPT = {"entity": "Event", "name": "event_name", "columns": ["name"], "method": "btree"}
REFUSED = "index build authorization refused"


def name(position: int, identity: str = INDEX) -> str:
    return f"sde_i_{identity}_{position:06d}"


def source(document: dict[str, Any]) -> dict[str, Any]:
    value: dict[str, Any] = document["groups"]["Event"]["source"]
    return value


def template() -> dict[str, Any]:
    """Event, source-only in PostgreSQL under contract 4; the next map adds one B-tree."""
    current = before_map()
    current["groups"]["Event"].pop("derived")
    current["groups"]["Event"].pop("also_write")
    prepared = copy.deepcopy(current)
    prepared["contract"], prepared["map_version"] = 5, 2
    source(prepared)["layout"]["indexes"] = [
        {"entity": "Event", "name": name(1), "columns": ["at"]}
    ]
    return {
        "kind": "sde-index",
        "protocol": 1,
        "index_id": INDEX,
        "project_id": PROJECT,
        "group": "Event",
        "current": current,
        "prepared": prepared,
        "build_budget_ms": BUDGET,
    }


def with_kept(plan: dict[str, Any], *kept: dict[str, Any]) -> None:
    """The map in force already carries a design (contract 5); the next map keeps it first."""
    plan["current"]["contract"] = 5
    source(plan["current"])["layout"]["indexes"] = copy.deepcopy(list(kept))
    added = source(plan["prepared"])["layout"]["indexes"]
    source(plan["prepared"])["layout"]["indexes"] = [*copy.deepcopy(list(kept)), *added]


def several(plan: dict[str, Any]) -> None:
    with_kept(plan, KEPT)
    source(plan["prepared"])["layout"]["indexes"] = [
        copy.deepcopy(KEPT),
        {"entity": "Event", "name": name(1), "columns": ["at"], "method": "brin"},
        {"entity": "Event", "name": name(2), "columns": ["name", "id"]},
    ]


def in_clickhouse(plan: dict[str, Any]) -> None:
    """The group lives in ClickHouse; the next map adds two data-skipping indexes."""
    copy_ = copy.deepcopy(before_map()["groups"]["Event"]["derived"][0])
    del copy_["lag_budget_ms"]
    for document in (plan["current"], plan["prepared"]):
        document["groups"]["Event"]["source"] = copy.deepcopy(copy_)
        document["routing"]["2477d087f39d0ae4"] = copy_["id"]
    source(plan["prepared"])["layout"]["indexes"] = [
        {
            "entity": "Event",
            "name": name(1),
            "columns": ["at"],
            "method": "minmax",
            "granularity": 4,
        },
        {
            "entity": "Event",
            "name": name(2),
            "columns": ["name"],
            "method": "set",
            "max_rows": 100,
            "granularity": 2,
        },
    ]


def lowered(plan: dict[str, Any]) -> None:
    legacy = {key: value for key, value in KEPT.items() if key != "method"}
    with_kept(plan, legacy)
    plan["prepared"]["contract"] = 4


def source_only_lost(plan: dict[str, Any]) -> None:
    copy_ = copy.deepcopy(before_map()["groups"]["Event"]["derived"][0])
    for document in (plan["current"], plan["prepared"]):
        document["groups"]["Event"].update(derived=[copy.deepcopy(copy_)], also_write=[copy_["id"]])


def dropped(plan: dict[str, Any]) -> None:
    with_kept(plan, KEPT)
    source(plan["prepared"])["layout"]["indexes"] = [
        {"entity": "Event", "name": name(1), "columns": ["at"]}
    ]


def nothing_new(plan: dict[str, Any]) -> None:
    with_kept(plan, KEPT)
    source(plan["prepared"])["layout"]["indexes"] = [copy.deepcopy(KEPT)]


def reordered(plan: dict[str, Any]) -> None:
    other = {"entity": "Event", "name": "event_at", "columns": ["at"], "method": "brin"}
    with_kept(plan, KEPT, other)
    source(plan["prepared"])["layout"]["indexes"] = [
        copy.deepcopy(other),
        copy.deepcopy(KEPT),
        {"entity": "Event", "name": name(1), "columns": ["id", "at"]},
    ]


def renamed_index(identity: str, position: int) -> Callable[[dict[str, Any]], None]:
    def change(plan: dict[str, Any]) -> None:
        source(plan["prepared"])["layout"]["indexes"][0]["name"] = name(position, identity)

    return change


def reused(plan: dict[str, Any]) -> None:
    for document in (plan["current"], plan["prepared"]):
        document["groups"]["Order"]["source"]["layout"]["tables"]["Order"] = name(1)


OTHER = {"entity": "Event", "name": "event_at", "columns": ["at"], "method": "brin"}
BUILT = {"entity": "Event", "name": name(1, "7" * 32), "columns": ["id"]}
"""An index a previous in-place build added: a bound name of another build id."""


def change(*prepared: dict[str, Any], kept: tuple[dict[str, Any], ...] = (KEPT,)) -> Callable[
    [dict[str, Any]], None
]:
    """Protocol 2: the map in force carries ``kept``; the next map carries ``prepared``.

    With nothing left the next map has no ``indexes`` at all - the form a controller writes, for
    which an empty list would claim that indexing was considered and none chosen.
    """

    def apply(plan: dict[str, Any]) -> None:
        plan["protocol"] = 2
        with_kept(plan, *kept)
        layout = source(plan["prepared"])["layout"]
        if prepared:
            layout["indexes"] = copy.deepcopy(list(prepared))
        else:
            layout.pop("indexes", None)

    return apply


def cases() -> list[tuple[str, Callable[[dict[str, Any]], None] | None, str | None]]:
    out: list[tuple[str, Callable[[dict[str, Any]], None] | None, str | None]] = []

    def case(
        label: str, change: Callable[[dict[str, Any]], None] | None, match: str | None
    ) -> None:
        out.append((label, change, match))

    def at(path: str, value: Any) -> Callable[[dict[str, Any]], None]:
        def change(plan: dict[str, Any]) -> None:
            *parents, last = path.split("/")
            target = plan
            for part in parents:
                target = target[part]
            target[last] = value

        return change

    case("143-index-build-adds-a-btree-in-place", None, None)
    case("144-index-build-adds-several-after-the-indexes-in-force", several, None)
    case("145-index-build-adds-clickhouse-skip-indexes", in_clickhouse, None)
    case(
        "146-index-build-refuses-another-local-project",
        at("project_id", "9" * 32),
        "index build authorization belongs to another local project",
    )
    case(
        "147-index-build-refuses-an-unknown-field",
        at("sql", "CREATE INDEX anything"),
        "index build authorization has missing or unknown fields",
    )
    case(
        "148-index-build-needs-its-budget",
        lambda p: p.pop("build_budget_ms"),
        "index build authorization has missing or unknown fields",
    )
    case(
        "149-index-build-refuses-an-unknown-protocol",
        at("protocol", 3),
        "unsupported index build authorization kind or protocol",
    )
    case(
        "150-index-build-refuses-another-kind",
        at("kind", "sde-stage"),
        "unsupported index build authorization kind or protocol",
    )
    case(
        "151-index-build-refuses-a-boolean-protocol",
        at("protocol", True),
        "unsupported index build authorization kind or protocol",
    )
    case(
        "152-index-build-id-is-lowercase-hexadecimal",
        at("index_id", INDEX.upper().replace("6", "A")),
        "index build index_id must be 32 lowercase hexadecimal digits",
    )
    case(
        "153-index-build-names-a-group",
        at("group", ""),
        "index build group must be a nonempty string",
    )
    budget = "index build budget must be an integer from 1 through 86400000 ms"
    case("154-index-build-budget-is-positive", at("build_budget_ms", 0), budget)
    case("155-index-build-budget-is-at-most-a-day", at("build_budget_ms", 86_400_001), budget)
    # The canonical form has no fractions, so this budget replaces a signed one after signing; the
    # budget rule comes before the signature in both loaders, and is what must refuse it.
    case("156-index-build-budget-is-an-integer", lambda p: p.update(fractional_budget=True), budget)
    case("157-index-build-verifies-the-envelope", lambda p: p.update(bad_envelope=True), REFUSED)
    case("158-index-build-verifies-both-maps", lambda p: p.update(bad_map=True), REFUSED)
    case(
        "159-index-build-current-must-be-signed",
        lambda p: p.update(unsigned_current=True),
        "index build signature must be an object",
    )
    case(
        "160-index-build-refuses-an-auto-layout",
        at("prepared/groups/Event/source/layout", {"auto": True, "dialect": "postgres"}),
        "index build maps need explicit physical layouts",
    )
    case(
        "161-index-build-needs-a-newer-map",
        at("prepared/map_version", 1),
        "an index build must allocate a newer prepared map",
    )
    case(
        "162-index-build-cannot-lower-the-contract",
        lowered,
        "an index build cannot lower the placement map contract",
    )
    case(
        "163-index-build-group-must-exist",
        at("group", "Missing"),
        "an index build cannot add or remove colocation groups",
    )
    case(
        "164-index-build-needs-a-source-only-group",
        source_only_lost,
        "an index build begins and ends with a source-only group",
    )
    case(
        "165-index-build-keeps-the-write-generation",
        at("prepared/groups/Event/write_epoch", 2),
        "an index build keeps the source's write generation",
    )
    case(
        "166-index-build-group-is-a-source-and-its-generation",
        at("prepared/groups/Event/derived", []),
        "an index build group is a source and its write generation only",
    )
    only_indexes = "an index build changes nothing about the source but its indexes"
    case(
        "167-index-build-keeps-the-tables",
        at("prepared/groups/Event/source/layout/tables", {"Event": "event_rebuilt"}),
        only_indexes,
    )
    case(
        "168-index-build-keeps-the-key-order",
        at("prepared/groups/Event/source/layout/key_order", {"Event": ["id"]}),
        only_indexes,
    )
    case(
        "169-index-build-keeps-the-engine",
        at("prepared/groups/Event/source/engine", "ch-1"),
        only_indexes,
    )
    in_force = "an index build keeps every index in force, in order, and adds at least one"
    case("170-index-build-keeps-every-index-in-force", dropped, in_force)
    case("171-index-build-adds-at-least-one-index", nothing_new, in_force)
    case("172-index-build-keeps-the-order-of-indexes-in-force", reordered, in_force)
    bound = "new indexes need fresh names bound to the index build id and their position"
    case("173-index-build-names-bind-the-build-id", renamed_index("9" * 32, 1), bound)
    case("174-index-build-names-bind-the-position", renamed_index(INDEX, 2), bound)
    case(
        "175-index-build-cannot-reuse-a-name-in-force",
        reused,
        "an index build cannot reuse a name the current map uses",
    )
    case(
        "176-index-build-keeps-routing",
        at("prepared/routing", {}),
        "an index build cannot change routing or other map attributes",
    )
    case(
        "177-index-build-keeps-other-groups",
        at("prepared/groups/Order/write_epoch", 8),
        "an index build cannot change an unaffected group",
    )
    case(
        "178-index-build-refuses-an-index-the-model-rejects",
        at(
            "prepared/groups/Event/source/layout/indexes",
            [{"entity": "Event", "name": name(1), "columns": ["missing"]}],
        ),
        REFUSED,
    )
    case("179-index-build-model-is-bound", at("prepared/model_version", "0" * 16), REFUSED)
    # Protocol 2: indexes the map in force declares are removed as well as added.
    new_btree = {"entity": "Event", "name": name(1), "columns": ["at"]}
    case("180-index-change-removes-an-index-in-place", change(), None)
    case("181-index-change-replaces-an-index", change(new_btree), None)
    case(
        "182-index-change-keeps-the-others-in-order",
        change(copy.deepcopy(OTHER), new_btree, kept=(KEPT, OTHER, BUILT)),
        None,
    )
    case(
        "183-index-change-removes-an-index-a-build-added",
        change(copy.deepcopy(KEPT), kept=(KEPT, BUILT)),
        None,
    )
    case(
        "184-index-change-removes-at-least-one",
        change(copy.deepcopy(KEPT), new_btree),
        "index build protocol 2 removes at least one index in force",
    )
    case(
        "185-index-change-keeps-the-order-of-the-others",
        change(copy.deepcopy(BUILT), copy.deepcopy(OTHER), kept=(KEPT, OTHER, BUILT)),
        "an index change keeps the other indexes in force, in order, before the new ones",
    )
    case(
        "186-index-change-names-bind-the-build-id",
        change({**new_btree, "name": name(1, "9" * 32)}),
        bound,
    )

    def moved_key(plan: dict[str, Any]) -> None:
        change(new_btree)(plan)
        source(plan["prepared"])["layout"]["key_order"] = {"Event": ["id"]}

    case("187-index-change-keeps-the-key-order", moved_key, only_indexes)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-am-changing-the-contract", action="store_true", required=True)
    parser.add_argument("--scratch-directory", type=Path, required=True)
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="write only these vectors (repeatable); every vector has its own key, so the others "
        "stay byte for byte as they are",
    )
    args = parser.parse_args()
    scratch = args.scratch_directory.resolve()
    if not scratch.is_dir() or scratch.is_relative_to(ROOT):
        parser.error("use an existing scratch directory outside the repository")
    model = json.loads((VECTORS / "routing/002-dual-write-fan-out/model.json").read_text())
    with tempfile.TemporaryDirectory(prefix="sde-index-vectors-", dir=scratch) as tmp:
        work = Path(tmp)
        private, public = work / "key.pem", work / "public.pem"
        openssl("genpkey", "-algorithm", "ED25519", "-out", str(private))
        openssl("pkey", "-in", str(private), "-pubout", "-out", str(public))
        der = openssl("pkey", "-in", str(private), "-pubout", "-outform", "DER")
        assert len(der) == 44 and der[:12] == bytes.fromhex("302a300506032b6570032100")
        public_key = base64.b64encode(der[12:]).decode()

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
                "key_id": "index",
                "value": base64.b64encode((work / "sig").read_bytes()).decode(),
            }

        written = [entry for entry in cases() if not args.only or entry[0] in args.only]
        unknown = set(args.only) - {entry[0] for entry in cases()}
        if unknown:
            parser.error(f"no such vector: {sorted(unknown)}")
        for label, change, match in written:
            plan = template()
            if change:
                change(plan)
            bad_envelope = plan.pop("bad_envelope", False)
            bad_map = plan.pop("bad_map", False)
            unsigned = plan.pop("unsigned_current", False)
            fractional = plan.pop("fractional_budget", False)
            for field in ("current", "prepared"):
                sign(plan[field])
            if bad_map:
                plan["prepared"]["signature"]["value"] = base64.b64encode(bytes(64)).decode()
            if unsigned:
                del plan["current"]["signature"]
            sign(plan)
            if bad_envelope:
                plan["signature"]["value"] = base64.b64encode(bytes(64)).decode()
            if fractional:
                plan["build_budget_ms"] = 1.5
            expected: dict[str, Any] = {"project_id": PROJECT}
            if match is not None:
                expected.update(error="MigrationRefused", match=match)
            else:
                indexes = source(plan["prepared"])["layout"].get("indexes", [])
                remaining = {index["name"] for index in indexes}
                in_force = source(plan["current"])["layout"].get("indexes", [])
                kept = [index for index in in_force if index["name"] in remaining]
                expected.update(
                    index_fingerprint=hashlib.sha256(payload(plan)).hexdigest(),
                    verified_with="index",
                    map_fingerprints={
                        field: hashlib.sha256(payload(plan[field])).hexdigest()
                        for field in ("current", "prepared")
                    },
                    added=[index["name"] for index in indexes[len(kept) :]],
                    build_budget_ms=plan["build_budget_ms"],
                )
                if plan["protocol"] == 2:
                    expected["removed"] = [
                        index["name"] for index in in_force if index["name"] not in remaining
                    ]
            directory = VECTORS / "migration" / label
            directory.mkdir(exist_ok=True)
            for filename, value in {
                "model.json": model,
                "plan.json": plan,
                "keys.json": {"index": public_key},
                "index.json": expected,
            }.items():
                (directory / filename).write_text(
                    json.dumps(value, indent=2, ensure_ascii=False) + "\n"
                )
    print(f"Wrote {len(written)} independently signed in-place index build fixtures")


if __name__ == "__main__":
    main()

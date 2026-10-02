#!/usr/bin/env python3
"""Generate the contract-6 cases - a group that carries no write generation - without SDK imports.

Map contract 6 lets a group leave ``write_epoch`` out, for an engine that cannot fence writes: our
own orderbook engine. These cases hold what that absence means, in four places:

- ``migration/`` session cases. A session writes the generation-bearing group with its generation
  and the other group without one, reserves the generation column in both, and refuses an engine
  whose fences contradict the map, in either direction.
- ``errors/`` map refusals. ``null`` is not absence, such a group has only a source - not even an
  empty list of copies - and a contract-5 map still needs a generation in every group.
- ``signature/``. A signed contract-6 map, whose write generations name only the groups that have
  one.
- ``migration/`` staging, in-place index and cutover authorizations. None can act on such a group
  or raise a map to contract 6, and each carries such a group byte for byte when it acts on another
  one.

Every expectation is written here rather than computed by a library. The signed documents are
signed and verified by OpenSSL over canonical bytes this tool encodes itself, with the fixture
encoder of ``nfc_map_vectors``.

    python conformance/tools/unfenced_vectors.py --i-am-changing-the-contract \
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
PROJECT, STAGE, INDEX = "1" * 32, "5" * 32, "6" * 32
E = "__sde_write_epoch"

MODEL: dict[str, Any] = {
    "entities": [
        {
            "name": "Reading",
            "fields": [{"name": "celsius", "type": "int32"}, {"name": "id", "type": "int64"}],
            "key": ["id"],
        },
        {
            "name": "Tick",
            "fields": [
                {"name": "at", "type": "int64"},
                {"name": "price", "type": "int64"},
                {"name": "venue", "type": "string"},
            ],
            "key": ["venue", "at"],
        },
    ]
}
# The contract's hash of MODEL (section 2). Not computed here, because that would take a library or
# a second implementation of the IR; it needs no trust either, since both loaders refuse a map whose
# model_version is not their own hash of the model, so a wrong value fails every case below.
MODEL_VERSION = "6217c681f69d94c7"


def session_map() -> dict[str, Any]:
    """Reading in an engine that fences writes, Tick in one that cannot: a contract-6 map."""
    return {
        "contract": 6,
        "project_id": PROJECT,
        "model_version": MODEL_VERSION,
        "map_version": 2,
        "groups": {
            "Reading": {
                "write_epoch": 2,
                "source": {
                    "id": "Reading@pg",
                    "engine": "pg-main",
                    "layout": {
                        "tables": {"Reading": "reading"},
                        "columns": {"Reading": {"id": "bigint", "celsius": "integer"}},
                    },
                },
            },
            "Tick": {
                "source": {
                    "id": "Tick@book",
                    "engine": "book-1",
                    "layout": {
                        "tables": {"Tick": "tick"},
                        "columns": {"Tick": {"venue": "text", "at": "bigint", "price": "bigint"}},
                    },
                },
            },
        },
    }


def dump(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def call(engine: str, name: str, **args: Any) -> dict[str, Any]:
    return {"engine": engine, "call": name, **args}


def session_cases(root: Path) -> int:
    empty: dict[str, dict[str, list[dict[str, Any]]]] = {
        "pg-main": {"reading": []},
        "book-1": {"tick": []},
    }
    fenced = {"pg-main": {"reading": {"project_id": PROJECT, "epoch": 2}}}
    reading, tick = {"id": 1, "celsius": 3}, {"venue": "XBT", "at": 5, "price": 7}
    written = 0

    def case(
        name: str,
        *,
        actions: list[dict[str, Any]] | None = None,
        tables: dict[str, Any] | None = None,
        calls: list[dict[str, Any]] | None = None,
        states: dict[str, Any] | None = None,
        error: str | None = None,
        match: str | None = None,
    ) -> None:
        nonlocal written
        engines = {
            "pg-main": {"tables": copy.deepcopy(empty["pg-main"])},
            # Our orderbook engine has nowhere to keep the map's bookkeeping either.
            "book-1": {"tables": copy.deepcopy(empty["book-1"]), "bookkeeping": False},
        }
        want: dict[str, Any] = {
            "engine_generations": copy.deepcopy(fenced if states is None else states),
            "actions": actions or [],
            "tables": copy.deepcopy(empty if tables is None else tables),
            "project_id": PROJECT,
        }
        if error is not None:
            want.update(error=error, match=match)
        directory = root / name
        directory.mkdir(exist_ok=True)
        for filename, body in (
            ("model.json", MODEL),
            ("map.json", session_map()),
            ("engines.json", engines),
            ("generation.json", want),
            ("calls.json", calls or []),
        ):
            dump(directory / filename, body)
        written += 1

    case(
        "188-a-group-without-a-generation-writes-without-one",
        actions=[
            {"op": "save", "entity": "Reading", "values": reading},
            {"op": "save", "entity": "Tick", "values": tick},
            {"op": "get", "entity": "Tick", "key": {"venue": "XBT", "at": 5}, "result": tick},
            {"op": "get", "entity": "Reading", "key": {"id": 1}, "result": reading},
        ],
        tables={"pg-main": {"reading": [{**reading, E: 2}]}, "book-1": {"tick": [tick]}},
        calls=[
            call("pg-main", "insert", table="reading"),
            call("book-1", "insert", table="tick"),
            call("book-1", "get", table="tick"),
            call("pg-main", "get", table="reading"),
        ],
    )
    case(
        "189-a-group-without-a-generation-still-reserves-the-column",
        actions=[
            {
                "op": "save",
                "entity": "Tick",
                "values": {**tick, E: 2},
                "error": "MigrationRefused",
                "match": "reserved",
            }
        ],
    )
    case(
        "190-a-group-without-a-generation-on-an-engine-that-fences",
        states={**fenced, "book-1": {"tick": {"project_id": PROJECT, "epoch": 2}}},
        error="MigrationRefused",
        match="carries no write generation on book-1, which fences writes",
    )
    case(
        "191-a-generation-on-an-engine-that-cannot-fence",
        states={},
        error="MigrationRefused",
        match="engine pg-main does not implement write generations",
    )
    return written


def map_refusals(root: Path) -> int:
    def refusal(name: str, mutate: Callable[[dict[str, Any]], None], match: str, why: str) -> None:
        body = session_map()
        mutate(body)
        directory = root / name
        directory.mkdir(exist_ok=True)
        expected = {"stage": "map", "error": "MapError", "match": match, "why": why}
        for filename, value in (
            ("model.json", MODEL),
            ("map.json", body),
            ("expected.json", expected),
        ):
            dump(directory / filename, value)

    copy_ = {
        "id": "Tick@book2",
        "engine": "book-2",
        "lag_budget_ms": 30000,
        "layout": {
            "tables": {"Tick": "tick"},
            "columns": {"Tick": {"venue": "text", "at": "bigint", "price": "bigint"}},
        },
    }
    refusal(
        "074-a-null-write-epoch-is-not-an-absent-one",
        lambda m: m["groups"]["Tick"].update(write_epoch=None),
        "requires a positive safe write_epoch, or no write_epoch key",
        "Absence is the only way a group says it carries no write generation. A null would be a "
        "second spelling of the same claim, and a canonical document has one.",
    )
    refusal(
        "075-a-group-without-a-generation-has-only-a-source",
        lambda m: m["groups"]["Tick"].update(
            derived=[copy.deepcopy(copy_)], also_write=[copy_["id"]]
        ),
        "a group without a write generation has only a source",
        "A maintained copy and a fan-out exist only through a migration, and a migration cuts off "
        "old writers by the generation this group does not carry.",
    )
    refusal(
        "076-a-group-without-a-generation-has-no-empty-list-of-copies",
        lambda m: m["groups"]["Tick"].update(derived=[]),
        "a group without a write generation has only a source",
        "The rule is on the key, not on its length: such a group says nothing about copies, and an "
        "empty list is a statement about them.",
    )

    def contract_5(m: dict[str, Any]) -> None:
        m["contract"] = 5

    refusal(
        "077-contract-5-needs-a-generation-in-every-group",
        contract_5,
        "contract 4 requires a positive safe write_epoch",
        "A group may leave its generation out from contract 6, so a contract-5 document without "
        "one is still refused. A contract-5 library would refuse this map whole, which is why the "
        "number moved.",
    )
    return 4


class Signer:
    """One Ed25519 key per run, generated and used by OpenSSL alone."""

    def __init__(self, work: Path, key_id: str) -> None:
        self.work, self.key_id = work, key_id
        self.private, self.public = work / f"{key_id}.pem", work / f"{key_id}.pub.pem"
        openssl("genpkey", "-algorithm", "ED25519", "-out", str(self.private))
        openssl("pkey", "-in", str(self.private), "-pubout", "-out", str(self.public))
        der = openssl("pkey", "-in", str(self.private), "-pubout", "-outform", "DER")
        assert len(der) == 44 and der[:12] == bytes.fromhex("302a300506032b6570032100")
        self.public_key = base64.b64encode(der[12:]).decode()

    def sign(self, document: dict[str, Any]) -> None:
        document.pop("signature", None)
        message, signature = self.work / "payload", self.work / "sig"
        message.write_bytes(payload(document))
        openssl(
            "pkeyutl", "-sign", "-rawin", "-inkey", str(self.private),
            "-in", str(message), "-out", str(signature),
        )  # fmt: skip
        openssl(
            "pkeyutl", "-verify", "-rawin", "-pubin", "-inkey", str(self.public),
            "-in", str(message), "-sigfile", str(signature),
        )  # fmt: skip
        document["signature"] = {
            "alg": "ed25519",
            "key_id": self.key_id,
            "value": base64.b64encode(signature.read_bytes()).decode(),
        }


def signed_map(root: Path, work: Path) -> int:
    signer = Signer(work, "unfenced")
    document = session_map()
    fingerprint = hashlib.sha256(payload(document)).hexdigest()
    signer.sign(document)
    directory = root / "015-a-signed-map-with-a-group-without-a-generation"
    directory.mkdir(exist_ok=True)
    expected = {
        "verified_with": "unfenced",
        "map_fingerprint": fingerprint,
        "project_id": PROJECT,
        # Only the groups that carry one: Tick has no generation to report.
        "write_epochs": {"Reading": 2},
    }
    for filename, value in (
        ("model.json", MODEL),
        ("map.json", document),
        ("keys.json", {"unfenced": signer.public_key}),
        ("expected.json", expected),
    ):
        dump(directory / filename, value)
    return 1


def unfenced_order(document: dict[str, Any]) -> None:
    """Order, Payment and User in an engine that cannot fence: contract 6, no generation."""
    document["contract"] = 6
    group = document["groups"]["Order"]
    group.pop("write_epoch")
    group["source"].update(engine="book-1", id="Order@book")
    document["routing"]["12f8b2171bc2bf78"] = "Order@book"


def unfenced_event(document: dict[str, Any]) -> None:
    document["contract"] = 6
    group = document["groups"]["Event"]
    group.pop("write_epoch")
    group["source"].update(engine="book-1", id="Event@book")
    document["routing"]["2477d087f39d0ae4"] = "Event@book"


def staging_template() -> dict[str, Any]:
    """Event, source-only in PostgreSQL, staged to a copy in ClickHouse - as staging/099."""
    current = before_map()
    current["groups"]["Event"].pop("derived")
    current["groups"]["Event"].pop("also_write")
    prepared = copy.deepcopy(current)
    prepared["map_version"] = 2
    target = copy.deepcopy(before_map()["groups"]["Event"]["derived"][0])
    target["layout"]["tables"]["Event"] = "sde_m_" + STAGE + "_000001"
    prepared["groups"]["Event"].update(derived=[target], also_write=[target["id"]])
    return {
        "kind": "sde-stage",
        "protocol": 1,
        "stage_id": STAGE,
        "project_id": PROJECT,
        "group": "Event",
        "current": current,
        "prepared": prepared,
    }


def index_template() -> dict[str, Any]:
    """Event, source-only in PostgreSQL; the next map adds one B-tree - as index/158."""
    current = before_map()
    current["groups"]["Event"].pop("derived")
    current["groups"]["Event"].pop("also_write")
    prepared = copy.deepcopy(current)
    prepared["contract"], prepared["map_version"] = 5, 2
    prepared["groups"]["Event"]["source"]["layout"]["indexes"] = [
        {"entity": "Event", "name": f"sde_i_{INDEX}_000001", "columns": ["at"]}
    ]
    return {
        "kind": "sde-index",
        "protocol": 1,
        "index_id": INDEX,
        "project_id": PROJECT,
        "group": "Event",
        "current": current,
        "prepared": prepared,
        "build_budget_ms": 3_600_000,
    }


def packets(root: Path, work: Path, model: dict[str, Any]) -> int:
    staging, index = Signer(work, "staging"), Signer(work, "index")

    def both(change: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], None]:
        def apply(plan: dict[str, Any]) -> None:
            for field in ("current", "prepared"):
                change(plan[field])

        return apply

    def staged_order(plan: dict[str, Any]) -> None:
        """Stage the group without a generation. The prepared map gives it one, so that both maps
        load and the staging rule is the one that refuses."""
        both(unfenced_order)(plan)
        current, prepared = plan["current"], plan["prepared"]
        prepared["groups"]["Event"] = copy.deepcopy(current["groups"]["Event"])
        columns = copy.deepcopy(current["groups"]["Order"]["source"]["layout"]["columns"])
        converted = {
            "uuid": "UUID",
            "text": "String",
            "timestamptz": "DateTime64(6, 'UTC')",
            "numeric(12,2)": "Decimal(12, 2)",
        }
        for fields in columns.values():
            for key, value in fields.items():
                fields[key] = converted[value]
        target = {
            "id": "Order@ch",
            "engine": "ch-1",
            "lag_budget_ms": 1000,
            "layout": {
                "tables": {
                    entity: "sde_m_" + STAGE + f"_{position:06d}"
                    for position, entity in enumerate(("Order", "Payment", "User"), 1)
                },
                "columns": columns,
            },
        }
        prepared["groups"]["Order"].update(
            write_epoch=1, derived=[target], also_write=[target["id"]]
        )
        plan["group"] = "Order"

    def raised(plan: dict[str, Any]) -> None:
        plan["current"]["contract"] = 5
        plan["prepared"]["contract"] = 6

    def index_on_unfenced(plan: dict[str, Any]) -> None:
        both(unfenced_event)(plan)

    def index_raised(plan: dict[str, Any]) -> None:
        plan["current"]["contract"] = 5
        plan["prepared"]["contract"] = 6

    staging_cases: list[tuple[str, Callable[[dict[str, Any]], None], str | None]] = [
        ("192-staging-carries-a-group-without-a-generation", both(unfenced_order), None),
        (
            "193-staging-refuses-a-group-without-a-generation",
            staged_order,
            "carries no write generation, so it cannot be staged",
        ),
        (
            "194-staging-cannot-raise-a-map-to-contract-6",
            raised,
            "staging cannot raise the placement map contract to 6",
        ),
    ]
    index_cases: list[tuple[str, Callable[[dict[str, Any]], None], str | None]] = [
        ("195-index-build-carries-a-group-without-a-generation", both(unfenced_order), None),
        (
            "196-index-build-refuses-a-group-without-a-generation",
            index_on_unfenced,
            "carries no write generation, so its indexes are not built in place",
        ),
        (
            "197-index-build-cannot-raise-a-map-to-contract-6",
            index_raised,
            "an index build cannot raise the placement map contract to 6",
        ),
    ]

    def write(directory: Path, files: dict[str, Any]) -> None:
        directory.mkdir(exist_ok=True)
        for filename, value in files.items():
            dump(directory / filename, value)

    for label, change, match in staging_cases:
        plan = staging_template()
        change(plan)
        for field in ("current", "prepared"):
            staging.sign(plan[field])
        staging.sign(plan)
        expected: dict[str, Any] = {"project_id": PROJECT}
        if match is not None:
            expected.update(error="MigrationRefused", match=match)
        else:
            expected.update(
                stage_fingerprint=hashlib.sha256(payload(plan)).hexdigest(),
                verified_with="staging",
                map_fingerprints={
                    field: hashlib.sha256(payload(plan[field])).hexdigest()
                    for field in ("current", "prepared")
                },
                tables=plan["prepared"]["groups"][plan["group"]]["derived"][0]["layout"]["tables"],
            )
        write(
            root / label,
            {
                "model.json": model,
                "plan.json": plan,
                "keys.json": {"staging": staging.public_key},
                "staging.json": expected,
            },
        )
    for label, change, match in index_cases:
        plan = index_template()
        change(plan)
        for field in ("current", "prepared"):
            index.sign(plan[field])
        index.sign(plan)
        expected = {"project_id": PROJECT}
        if match is not None:
            expected.update(error="MigrationRefused", match=match)
        else:
            added = plan["prepared"]["groups"]["Event"]["source"]["layout"]["indexes"]
            expected.update(
                index_fingerprint=hashlib.sha256(payload(plan)).hexdigest(),
                verified_with="index",
                map_fingerprints={
                    field: hashlib.sha256(payload(plan[field])).hexdigest()
                    for field in ("current", "prepared")
                },
                added=[entry["name"] for entry in added],
                build_budget_ms=plan["build_budget_ms"],
            )
        write(
            root / label,
            {
                "model.json": model,
                "plan.json": plan,
                "keys.json": {"index": index.public_key},
                "index.json": expected,
            },
        )
    return len(staging_cases) + len(index_cases)


def cutover_template() -> dict[str, Any]:
    """Event moves from PostgreSQL to its copy in ClickHouse; Order carries no generation."""
    before = before_map()
    unfenced_order(before)
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
        value["routing"] = {"12f8b2171bc2bf78": "Order@book"}
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


def cutovers(root: Path, work: Path, model: dict[str, Any]) -> int:
    signer = Signer(work, "cutover")

    def given_one(plan: dict[str, Any]) -> None:
        plan["success"]["groups"]["Order"]["write_epoch"] = 1

    cases: list[tuple[str, Callable[[dict[str, Any]], None] | None, str | None]] = [
        ("198-cutover-carries-a-group-without-a-generation", None, None),
        ("199-cutover-cannot-give-a-group-a-generation", given_one, "unaffected group"),
    ]
    for label, change, match in cases:
        plan = cutover_template()
        if change:
            change(plan)
        for candidate in ("before", "success", "abort"):
            signer.sign(plan[candidate])
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
        signer.sign(plan)
        expected: dict[str, Any] = {"project_id": PROJECT}
        if match is not None:
            expected.update(error="MigrationRefused", match=match)
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
        directory = root / label
        directory.mkdir(exist_ok=True)
        for filename, value in {
            "model.json": model,
            "plan.json": plan,
            "keys.json": {"cutover": signer.public_key},
            "cutover.json": expected,
        }.items():
            dump(directory / filename, value)
    return len(cases)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-am-changing-the-contract", action="store_true", required=True)
    parser.add_argument("--scratch-directory", type=Path, required=True)
    args = parser.parse_args()
    scratch = args.scratch_directory.resolve()
    if not scratch.is_dir() or scratch.is_relative_to(ROOT):
        parser.error("use an existing scratch directory outside the repository")
    routing_model = json.loads((VECTORS / "routing/002-dual-write-fan-out/model.json").read_text())
    with tempfile.TemporaryDirectory(prefix="sde-unfenced-vectors-", dir=scratch) as tmp:
        work = Path(tmp)
        counts = (
            session_cases(VECTORS / "migration"),
            map_refusals(VECTORS / "errors"),
            signed_map(VECTORS / "signature", work),
            packets(VECTORS / "migration", work, routing_model),
            cutovers(VECTORS / "migration", work, routing_model),
        )
    print(
        f"Wrote {counts[0]} session cases, {counts[1]} map refusals, {counts[2]} signed map, "
        f"{counts[3]} signed staging and index authorizations and {counts[4]} cutover plans "
        f"without importing an SDK"
    )


if __name__ == "__main__":
    main()

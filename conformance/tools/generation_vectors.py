#!/usr/bin/env python3
"""Add generation-bearing session cases with explicit row/call expectations, no SDK import."""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

P = "1" * 32
E = "__sde_write_epoch"
AT = "2026-09-12T12:00:00Z"


def call(engine: str, name: str, **args: Any) -> dict[str, Any]:
    return {"engine": engine, "call": name, **args}


def scan(
    engine: str, table: str, after: Any = None, upto: Any = None, limit: Any = 3
) -> dict[str, Any]:
    return call(engine, "key_range", table=table, after=after, upto=upto, limit=limit)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-am-changing-the-contract", action="store_true", required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "vectors" / "migration"
    base = root / "022-verification-is-bound-to-its-request"
    model = json.loads((base / "model.json").read_text())
    placement = json.loads((base / "map.json").read_text())
    placement.update(contract=4, project_id=P, map_version=2)
    placement["groups"]["Reading"]["write_epoch"] = 2
    empty: dict[str, dict[str, list[dict[str, Any]]]] = {
        name: {"reading": [], "sample": []} for name in ("pg-main", "pg-copy")
    }
    metadata = {
        name: {table: {"project_id": P, "epoch": 2} for table in tables}
        for name, tables in empty.items()
    }
    cases: list[tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]] = []

    def case(
        name: str,
        *,
        initial: dict[str, Any] | None = None,
        actions: list[dict[str, Any]] | None = None,
        tables: dict[str, Any] | None = None,
        calls: list[dict[str, Any]] | None = None,
        error: str | None = None,
        match: str | None = None,
        project: str | None = P,
        states: dict[str, Any] | None = None,
        markers: dict[str, Any] | None = None,
    ) -> None:
        before = copy.deepcopy(initial if initial is not None else empty)
        engine_spec = {engine: {"tables": value} for engine, value in before.items()}
        if markers:
            for engine, values in markers.items():
                engine_spec[engine]["markers"] = values
        want: dict[str, Any] = {
            "engine_generations": copy.deepcopy(metadata if states is None else states),
            "actions": actions or [],
            "tables": copy.deepcopy(tables if tables is not None else before),
        }
        if project is not None:
            want["project_id"] = project
        if error is not None:
            want.update(error=error, match=match)
        cases.append((name, engine_spec, want, calls or []))

    row = {"id": 1, "celsius": 3, E: 2}
    after = copy.deepcopy(empty)
    for value in after.values():
        value["reading"] = [row]
    case(
        "062-generation-stamps-source-and-copy",
        actions=[
            {"op": "save", "entity": "Reading", "values": {"id": 1, "celsius": 3}},
            {"op": "get", "entity": "Reading", "key": {"id": 1}, "result": {"id": 1, "celsius": 3}},
        ],
        tables=after,
        calls=[
            call("pg-main", "insert", table="reading"),
            call("pg-copy", "insert", table="reading"),
            call("pg-main", "get", table="reading"),
        ],
    )
    after = copy.deepcopy(empty)
    for value in after.values():
        value["reading"] = [row, {"id": 2, "celsius": 6, E: 2}]
    case(
        "063-generation-stamps-deferred-transaction-copies",
        actions=[
            {
                "op": "transaction",
                "entities": ["Reading"],
                "writes": [
                    {"entity": "Reading", "values": {"id": 1, "celsius": 3}},
                    {"entity": "Reading", "values": {"id": 2, "celsius": 6}},
                ],
            }
        ],
        tables=after,
        calls=[
            call("pg-main", "transaction"),
            call("pg-main", "insert", table="reading"),
            call("pg-main", "insert", table="reading"),
            call("pg-copy", "insert", table="reading"),
            call("pg-copy", "insert", table="reading"),
        ],
    )
    case(
        "064-generation-session-needs-local-project",
        project=None,
        error="MigrationRefused",
        match="locally configured",
    )
    case(
        "065-generation-session-refuses-other-project",
        project="9" * 32,
        error="MigrationRefused",
        match="locally configured",
    )
    older = copy.deepcopy(metadata)
    older["pg-main"]["reading"]["epoch"] = 1
    case(
        "066-generation-session-refuses-inactive-epoch",
        states=older,
        error="MigrationRefused",
        match="write generation",
    )
    before = copy.deepcopy(empty)
    before["pg-main"]["reading"] = [{"id": 1, "celsius": 3, E: 1}]
    after = copy.deepcopy(before)
    after["pg-copy"]["reading"] = [row]
    case(
        "067-generation-backfill-retags-historical-rows",
        initial=before,
        tables=after,
        actions=[{"op": "backfill", "group": "Reading"}],
        calls=[
            call("pg-copy", "backfill_marker", materialization="Reading@pg2", entity="Reading"),
            scan("pg-main", "reading"),
            call("pg-copy", "copy_in", table="reading", rows=1),
            call(
                "pg-copy",
                "record_backfill_marker",
                materialization="Reading@pg2",
                entity="Reading",
                rows=1,
            ),
            call("pg-copy", "backfill_marker", materialization="Reading@pg2", entity="Sample"),
            scan("pg-main", "sample"),
        ],
    )
    report = {
        "at": AT,
        "chunks_compared": 1,
        "chunks_mismatched": 0,
        "tail_rows_read": 0,
        "tail_rows_missing_in_target": 0,
        "rows_source": 1,
        "rows_target": 1,
    }
    case(
        "068-generation-verification-compares-data-not-epochs",
        initial=after,
        markers={"pg-copy": {"Reading@pg2|Reading": 1}},
        actions=[{"op": "verify", "group": "Reading", "at": AT, "matched": True, "report": report}],
        calls=[
            call("pg-copy", "backfill_marker", materialization="Reading@pg2", entity="Reading"),
            call("pg-main", "count", table="reading"),
            call("pg-copy", "count", table="reading"),
            scan("pg-main", "reading", limit=1),
            scan("pg-copy", "reading", upto=[1], limit=None),
            scan("pg-main", "reading", after=[1]),
            call("pg-copy", "backfill_marker", materialization="Reading@pg2", entity="Sample"),
            call("pg-main", "count", table="sample"),
            call("pg-copy", "count", table="sample"),
            scan("pg-main", "sample"),
        ],
    )
    case(
        "069-generation-column-is-not-application-input",
        actions=[
            {
                "op": "save",
                "entity": "Reading",
                "values": {"id": 1, "celsius": 3, E: 2},
                "error": "MigrationRefused",
                "match": "reserved",
            }
        ],
    )
    case(
        "070-generation-needs-adapter-support",
        states={},
        error="MigrationRefused",
        match="does not implement write generations",
    )
    for name, engines, want, calls in cases:
        directory = root / name
        directory.mkdir(exist_ok=True)
        for filename, body in (
            ("model.json", model),
            ("map.json", placement),
            ("engines.json", engines),
            ("generation.json", want),
            ("calls.json", calls),
        ):
            (directory / filename).write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n")
    errors: list[tuple[str, dict[str, Any], str]] = []

    def error(name: str, mutate: Any, message: str) -> None:
        body = copy.deepcopy(placement)
        mutate(body)
        errors.append((name, body, message))

    error("039-generation-map-needs-project", lambda m: m.pop("project_id"), "project_id")
    error(
        "040-generation-map-needs-epoch",
        lambda m: m["groups"]["Reading"].pop("write_epoch"),
        "positive safe write_epoch",
    )
    error(
        "041-generation-map-refuses-boolean-epoch",
        lambda m: m["groups"]["Reading"].update(write_epoch=True),
        "positive safe write_epoch",
    )
    error(
        "042-generation-map-refuses-fractional-epoch",
        lambda m: m["groups"]["Reading"].update(write_epoch=1.5),
        "positive safe write_epoch",
    )
    error(
        "043-generation-map-refuses-unsafe-epoch",
        lambda m: m["groups"]["Reading"].update(write_epoch=2**53),
        "positive safe write_epoch",
    )
    error(
        "044-generation-map-reserves-epoch-column",
        lambda m: m["groups"]["Reading"]["source"]["layout"]["columns"]["Reading"].update(
            {E: "bigint"}
        ),
        "write-epoch column is reserved",
    )
    error(
        "045-generation-project-needs-new-contract",
        lambda m: m.update(contract=3),
        "project_id requires",
    )

    def old_epoch(m: dict[str, Any]) -> None:
        m.update(contract=3)
        m.pop("project_id")

    error("046-generation-epoch-needs-new-contract", old_epoch, "write_epoch requires")
    error(
        "047-generation-map-must-be-canonical",
        lambda m: m.update(annotation=1.5),
        "canonically encodable",
    )
    error(
        "048-generation-map-refuses-invalid-project",
        lambda m: m.update(project_id="G" * 32),
        "project_id",
    )
    error(
        "049-generation-map-reserves-drain-log",
        lambda m: m["groups"]["Reading"]["source"]["layout"]["tables"].update(
            Reading="__sde_fence_drains"
        ),
        "drain log is reserved",
    )
    for name, body, message in errors:
        directory = root.parent / "errors" / name
        directory.mkdir(exist_ok=True)
        expected = {
            "stage": "map",
            "error": "MapError",
            "match": message,
            "why": (
                "Generation-bearing placement must refuse missing or ambiguous safety metadata "
                "before any engine operation."
            ),
        }
        for filename, payload in (
            ("model.json", model),
            ("map.json", body),
            ("expected.json", expected),
        ):
            (directory / filename).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            )
    signed_case(root, placement, model, args.scratch)
    print(
        f"Wrote {len(cases)} session cases and {len(errors)} map refusals without importing an SDK"
    )


def signed_case(
    root: Path, placement: dict[str, Any], model: dict[str, Any], scratch: Path
) -> None:
    directory = root.parent / "signature" / "008-generation-map-normalizes-json-integers"
    directory.mkdir(exist_ok=True)
    payload = json.dumps(
        placement, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
    assert payload.isascii(), "the independent fixture encoder covers ASCII map inputs"
    expected = {
        "verified_with": "generation",
        "map_fingerprint": hashlib.sha256(payload).hexdigest(),
        "project_id": P,
        "write_epochs": {"Reading": 2},
    }
    existing = directory / "map.json"
    reuse = False
    if existing.exists():
        stored = json.loads(existing.read_text())
        unsigned = {key: value for key, value in stored.items() if key != "signature"}
        reuse = unsigned == placement
    if not reuse:
        scratch.mkdir(parents=True, exist_ok=True)
        secret = scratch / "generation-private.pem"
        message = scratch / "generation-payload.json"
        signature = scratch / "generation-signature.bin"
        message.write_bytes(payload)
        subprocess.run(
            ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(secret)],
            check=True,
            capture_output=True,
        )
        public = subprocess.check_output(
            ["openssl", "pkey", "-in", str(secret), "-pubout", "-outform", "DER"]
        )
        assert len(public) == 44
        subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-sign",
                "-rawin",
                "-inkey",
                str(secret),
                "-in",
                str(message),
                "-out",
                str(signature),
            ],
            check=True,
        )
        actual = (
            subprocess.check_output(["openssl", "dgst", "-sha256", str(message)], text=True)
            .strip()
            .split()[-1]
        )
        assert actual == expected["map_fingerprint"]
        stored = copy.deepcopy(placement)
        stored["contract"] = 4.0
        stored["map_version"] = 2.0
        stored["groups"]["Reading"]["write_epoch"] = 2.0
        stored["signature"] = {
            "alg": "ed25519",
            "value": base64.b64encode(signature.read_bytes()).decode(),
            "key_id": "generation",
        }
        (directory / "map.json").write_text(json.dumps(stored, indent=2) + "\n")
        (directory / "keys.json").write_text(
            json.dumps({"generation": base64.b64encode(public[-32:]).decode()}, indent=2) + "\n"
        )
    for filename, value in (("model.json", model), ("expected.json", expected)):
        (directory / filename).write_text(json.dumps(value, indent=2) + "\n")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Generate only the Unicode map-boundary vectors, without importing either SDK.

Canonical JSON here is restricted to these fixtures: integer numbers, ordinary JSON values and
NFC strings. OpenSSL signs and independently verifies the canonical payload. Private keys live
only in the caller's mandatory scratch directory and are removed on completion.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
VECTORS = ROOT / "conformance" / "vectors"


def normalized(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        result = {
            unicodedata.normalize("NFC", key): normalized(child) for key, child in value.items()
        }
        assert len(result) == len(value), "fixture encoder refuses colliding normalized keys"
        return result
    if isinstance(value, list):
        return [normalized(child) for child in value]
    if isinstance(value, float):
        assert value.is_integer(), "fixture encoder supports integral JSON numbers only"
        return int(value)
    return value


def payload(document: dict[str, Any]) -> bytes:
    return json.dumps(
        normalized({key: value for key, value in document.items() if key != "signature"}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def openssl(*arguments: str) -> bytes:
    result = subprocess.run(["openssl", *arguments], capture_output=True, check=True)
    return result.stdout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-am-changing-the-contract", action="store_true", required=True)
    parser.add_argument("--scratch-directory", type=Path, required=True)
    args = parser.parse_args()
    scratch = args.scratch_directory.resolve()
    if scratch.is_relative_to(ROOT) or not scratch.is_dir():
        parser.error("use an existing scratch directory outside the repository")
    base = VECTORS / "signature" / "008-generation-map-normalizes-json-integers"
    model = json.loads((base / "model.json").read_text())
    document = normalized(json.loads((base / "map.json").read_text()))
    document.pop("signature")
    source = document["groups"]["Reading"]["source"]
    source["engine"] = "pg-caf\u00e9"
    source["layout"]["tables"]["Reading"] = "caf\u00e9_\U0001f6f0"
    document["metadata"] = {"cl\u00e9": ["d\u00e9j\u00e0"]}
    message = payload(document)
    with tempfile.TemporaryDirectory(prefix="sde-nfc-map-", dir=scratch) as temporary:
        work = Path(temporary)
        private, public = work / "key.pem", work / "public.pem"
        openssl("genpkey", "-algorithm", "ED25519", "-out", str(private))
        openssl("pkey", "-in", str(private), "-pubout", "-out", str(public))
        der = openssl("pkey", "-in", str(private), "-pubout", "-outform", "DER")
        assert der[:12] == bytes.fromhex("302a300506032b6570032100") and len(der) == 44
        (work / "payload").write_bytes(message)
        openssl(
            "pkeyutl",
            "-sign",
            "-rawin",
            "-inkey",
            str(private),
            "-in",
            str(work / "payload"),
            "-out",
            str(work / "signature"),
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
            str(work / "signature"),
        )
        signature = (work / "signature").read_bytes()
    document["signature"] = {
        "alg": "ed25519",
        "key_id": "unicode",
        "value": base64.b64encode(signature).decode(),
    }
    keys = {"unicode": base64.b64encode(der[12:]).decode()}
    accepted = {
        "verified_with": "unicode",
        "map_fingerprint": hashlib.sha256(message).hexdigest(),
        "project_id": document["project_id"],
        "write_epochs": {"Reading": 2},
    }

    def write(family: str, name: str, value: dict[str, Any], expected: dict[str, Any]) -> None:
        directory = VECTORS / family / name
        directory.mkdir(exist_ok=True)
        files = {"model.json": model, "map.json": value, "expected.json": expected}
        if family == "signature":
            files["keys.json"] = keys
        for filename, body in files.items():
            (directory / filename).write_text(json.dumps(body, indent=2, ensure_ascii=True) + "\n")

    write("signature", "009-nfc-map-identifiers", document, accepted)
    refused = {"error": "MapError", "match": "Unicode scalar text in NFC"}
    for name, part in (
        ("010-noncanonical-table-name", "table"),
        ("011-noncanonical-engine-binding", "engine"),
        ("012-noncanonical-payload-member", "member"),
        ("013-noncanonical-nested-value", "value"),
    ):
        altered = copy.deepcopy(document)
        if part == "table":
            altered["groups"]["Reading"]["source"]["layout"]["tables"]["Reading"] = (
                "cafe\u0301_\U0001f6f0"
            )
        elif part == "engine":
            altered["groups"]["Reading"]["source"]["engine"] = "pg-cafe\u0301"
        elif part == "member":
            altered["metadata"] = {"cle\u0301": ["d\u00e9j\u00e0"]}
        else:
            altered["metadata"]["cl\u00e9"] = ["de\u0301ja\u0300"]
        assert payload(altered) == message, "case must retain the very same signed payload"
        write("signature", name, altered, refused)
    hinted = copy.deepcopy(document)
    hinted["signature"]["key_id"] = "e\u0301"
    assert payload(hinted) == message
    write("signature", "014-key-hint-outside-canonical-payload", hinted, accepted)
    for name, surrogate in (
        ("050-high-surrogate-in-map-payload", "\ud800"),
        ("051-low-surrogate-in-map-member", "\udfff"),
    ):
        altered = copy.deepcopy(document)
        del altered["signature"]
        if "high" in name:
            altered["groups"]["Reading"]["source"]["layout"]["tables"]["Reading"] = surrogate
        else:
            altered["metadata"] = {surrogate: "value"}
        write("errors", name, altered, {**refused, "stage": "map"})
    print(
        "Wrote eight Unicode map-boundary vectors; "
        "canonical payload and signatures derived independently."
    )


if __name__ == "__main__":
    main()

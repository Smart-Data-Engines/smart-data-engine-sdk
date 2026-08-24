"""Generate the ``hashing/`` vectors, verifying every digest against openssl before writing.

The other generators have an honest limitation stated at the top of ``generate.py``: they are
produced by the implementation they are meant to verify, so a bug in the reference would be frozen
into the expectation. That is why ``model/001-single-entity`` is hand-written.

These vectors do not have that limitation, and it costs almost nothing to remove. An HMAC-SHA256 is
reproducible by anything - so every digest here is recomputed by shelling out to ``openssl dgst``,
which shares no code with this library, and a disagreement aborts before a single file is written.
What that checks is the part a port gets wrong: the message construction. Whether the parts are
NFC-normalised, whether U+0000 joins them, whether the prefix is inside or outside the message. The
HMAC itself was never the risk.

    python conformance/tools/hashing_vectors.py --i-am-changing-the-contract
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

import sde  # noqa: E402
from sde.canonical import canonical_bytes  # noqa: E402
from sde.hashing import DIGEST_CHARS, hash_identifiers  # noqa: E402
from sde.testing.loader import model_from_neutral  # noqa: E402

VECTORS = ROOT / "conformance" / "vectors" / "hashing"


def openssl_digest(salt: bytes, prefix: str, parts: list[str]) -> str:
    """The same derivation, by something that is not this library.

    NFC and the U+0000 join are applied here too - deliberately, because they are what is being
    pinned. If they were left out, this would agree with a broken implementation.
    """
    message = "\x00".join(unicodedata.normalize("NFC", part) for part in parts)
    completed = subprocess.run(
        [
            "openssl", "dgst", "-sha256", "-mac", "HMAC",
            "-macopt", f"hexkey:{salt.hex()}", "-r",
        ],
        input=message.encode("utf-8"),
        capture_output=True,
        check=True,
    )
    return prefix + completed.stdout.decode().split()[0][:DIGEST_CHARS]


def name_map_document(model: sde.LogicalModel, salt: bytes) -> dict[str, Any]:
    """Every digest the contract requires, each one confirmed by openssl first."""
    _, names = hash_identifiers(model, salt)

    entities: dict[str, str] = {}
    for entity, hashed in sorted(names.entities.items()):
        independent = openssl_digest(salt, "e_", [entity])
        if independent != hashed:
            raise SystemExit(
                f"entity {entity!r}: this library derives {hashed}, openssl derives {independent}. "
                "Refusing to write a vector that pins one implementation's mistake."
            )
        entities[entity] = hashed

    fields: dict[str, dict[str, str]] = {}
    for entity, mapping in sorted(names.fields.items()):
        fields[entity] = {}
        for field, hashed in sorted(mapping.items()):
            independent = openssl_digest(salt, "f_", [entity, field])
            if independent != hashed:
                raise SystemExit(
                    f"field {entity}.{field}: {hashed} vs openssl {independent}. The message is "
                    "built differently - check the NFC step, the U+0000 join, and whether the "
                    "prefix leaked into the message."
                )
            fields[entity][field] = hashed

    relations: dict[str, dict[str, str]] = {}
    for entity, mapping in sorted(names.relations.items()):
        relations[entity] = {}
        for relation, hashed in sorted(mapping.items()):
            independent = openssl_digest(salt, "r_", [entity, relation])
            if independent != hashed:
                raise SystemExit(f"relation {entity}.{relation}: {hashed} vs {independent}")
            relations[entity][relation] = hashed

    return {"entities": entities, "fields": fields, "relations": relations}


def write_json(path: Path, value: Any) -> None:
    # ASCII with \u escapes, like model/004: which normal form a file holds must not depend on what
    # opens it, and in this directory that is the whole subject.
    io.open(path, "w", encoding="ascii").write(
        json.dumps(value, indent=2, ensure_ascii=True, sort_keys=False) + "\n"
    )


def build(case: Path) -> None:
    salt = bytes.fromhex((case / "salt.hex").read_text().strip())
    if len(salt) < 16:
        raise SystemExit(f"{case.name}: the salt in the vector is too short to be accepted")

    sde.clear_registry()
    model = model_from_neutral(json.loads((case / "model.json").read_text(encoding="utf-8")))
    hashed, _ = hash_identifiers(model, salt)

    write_json(case / "names.json", name_map_document(model, salt))
    (case / "ir.json").write_bytes(canonical_bytes(hashed.ir))
    (case / "version.txt").write_text(hashed.version + "\n")
    write_json(
        case / "groups.json",
        [{"name": g.name, "members": list(g.members)} for g in sde.colocation_groups(hashed)],
    )

    # Only where the case is about two spellings of one name.
    decomposed = case / "model-decomposed.json"
    if decomposed.exists():
        sde.clear_registry()
        other = model_from_neutral(json.loads(decomposed.read_text(encoding="utf-8")))
        other_hashed, _ = hash_identifiers(other, salt)
        if other_hashed.version != hashed.version:
            raise SystemExit(
                f"{case.name}: the two normal forms give different hashed models "
                f"({hashed.version} vs {other_hashed.version}). That is the defect this vector "
                "exists to catch, so it is not written until it is fixed."
            )
        (case / "version-decomposed.txt").write_text(other_hashed.version + "\n")

    print(f"  {case.name}: {hashed.version}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--i-am-changing-the-contract", action="store_true")
    arguments = parser.parse_args()
    if not arguments.i_am_changing_the_contract:
        raise SystemExit(
            "A committed vector is the contract. Regenerating one means bumping "
            "conformance/contract-version.txt and every library declaring the new version. Pass "
            "--i-am-changing-the-contract if that is what you are doing."
        )
    for case in sorted(p for p in VECTORS.iterdir() if p.is_dir()):
        build(case)


if __name__ == "__main__":
    main()

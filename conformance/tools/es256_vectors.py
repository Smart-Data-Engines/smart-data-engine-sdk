"""Add the ES256 cases to ``signature/`` (016 on), every signature produced and checked by openssl.

    python conformance/tools/es256_vectors.py --i-am-changing-the-contract

A map's signature may be ES256 - ECDSA over P-256 with SHA-256 - beside Ed25519 (format contract §7,
"Signing"): the value is r and s, 32 bytes each (IEEE P1363), with s in the lower half of the order,
and a public key is an uncompressed SEC1 point, 65 bytes. As in ``signature_vectors.py``, the key
pairs live in a temporary directory and are discarded, the signatures come from openssl and are
verified by openssl before anything is written, and only the payload - the canonical bytes of the map
without its signature block - comes from this library. This file writes its own case directories
alone and leaves every other case of the family as it is.

openssl's ECDSA draws a fresh nonce for every signature, so regenerating writes new keys and new
signatures: a signature is not one of the artefacts that has to stay byte-stable. Moving s to the
lower half (n - s) is arithmetic this file does itself, and the moved signature is verified by
openssl again.
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
VECTORS = ROOT / "conformance" / "vectors" / "signature"
BASE = ROOT / "conformance" / "vectors" / "errors" / "014-a-signed-map-with-no-key-to-check-it"

# The 26-byte SubjectPublicKeyInfo prefix of an uncompressed P-256 public key in DER, and the
# 12-byte one of Ed25519: restated rather than parsed, so that this file shares no code with the
# libraries.
P256_DER_PREFIX = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")
ED25519_DER_PREFIX = bytes.fromhex("302a300506032b6570032100")
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
P = 2**256 - 2**224 + 2**192 + 2**96 - 1

CASES = (
    "016-an-es256-key-verifies",
    "017-keys-of-both-algorithms-the-es256-one-verifies",
    "018-an-ed25519-key-does-not-verify-an-es256-signature",
    "019-a-p256-key-does-not-verify-an-ed25519-signature",
    "020-an-es256-signature-over-other-bytes-does-not-verify",
    "021-an-es256-signature-with-s-in-the-upper-half-is-refused",
    "022-an-es256-signature-of-63-bytes-is-refused",
    "023-a-p256-key-off-the-curve-is-refused",
    "024-a-compressed-p256-key-is-refused",
    "025-an-unknown-algorithm-is-refused",
    "026-a-hybrid-p256-key-is-refused",
    "027-an-es256-signature-with-r-zero-is-refused",
    "028-an-es256-signature-with-r-at-the-order-is-refused",
    "029-an-es256-signature-with-s-zero-is-refused",
)


def openssl(*args: str) -> bytes:
    result = subprocess.run(["openssl", *args], capture_output=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"openssl {' '.join(args)} failed: {result.stderr.decode()}")
    return result.stdout


def der_ints(der: bytes) -> tuple[int, int]:
    """r and s of an ECDSA-Sig-Value: SEQUENCE { INTEGER r, INTEGER s }, short lengths only."""
    if der[0] != 0x30 or der[1] != len(der) - 2:
        raise SystemExit(f"unexpected ECDSA signature DER: {der.hex()}")
    out, at = [], 2
    for _ in range(2):
        if der[at] != 0x02:
            raise SystemExit(f"unexpected ECDSA signature DER: {der.hex()}")
        size = der[at + 1]
        out.append(int.from_bytes(der[at + 2 : at + 2 + size], "big"))
        at += 2 + size
    return out[0], out[1]


def der_of(r: int, s: int) -> bytes:
    def integer(value: int) -> bytes:
        raw = value.to_bytes((value.bit_length() + 8) // 8, "big")
        return bytes([0x02, len(raw)]) + raw

    body = integer(r) + integer(s)
    return bytes([0x30, len(body)]) + body


class P256:
    """One throwaway P-256 key pair, in a temporary directory."""

    def __init__(self, home: Path, name: str) -> None:
        self.home, self.name = home, name
        self.private = home / f"{name}.pem"
        openssl("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256",
                "-pkeyopt", "ec_param_enc:named_curve", "-out", str(self.private))
        der = openssl("pkey", "-in", str(self.private), "-pubout", "-outform", "DER")
        if not der.startswith(P256_DER_PREFIX) or len(der) != len(P256_DER_PREFIX) + 65:
            raise SystemExit(f"unexpected DER shape for {name}: {der.hex()}")
        self.public = der[len(P256_DER_PREFIX):]
        self.public_pem = home / f"{name}.pub.pem"
        self.public_pem.write_bytes(openssl("pkey", "-in", str(self.private), "-pubout"))

    def verified(self, payload: bytes, der: bytes) -> bool:
        message, signature = self.home / f"{self.name}.msg", self.home / f"{self.name}.sig"
        message.write_bytes(payload)
        signature.write_bytes(der)
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-verify", str(self.public_pem), "-signature",
             str(signature), str(message)], capture_output=True, check=False,
        )
        return result.returncode == 0 and b"Verified OK" in result.stdout

    def sign(self, payload: bytes) -> tuple[int, int]:
        """r and s of openssl's signature over ``payload``, s moved to the lower half."""
        message = self.home / f"{self.name}.msg"
        message.write_bytes(payload)
        r, s = der_ints(openssl("dgst", "-sha256", "-sign", str(self.private), str(message)))
        if s > N // 2:
            s = N - s
        if not self.verified(payload, der_of(r, s)):
            raise SystemExit(f"openssl would not verify its own signature for {self.name}")
        return r, s


class Ed25519:
    def __init__(self, home: Path, name: str) -> None:
        self.home, self.name = home, name
        self.private = home / f"{name}.pem"
        openssl("genpkey", "-algorithm", "ed25519", "-out", str(self.private))
        der = openssl("pkey", "-in", str(self.private), "-pubout", "-outform", "DER")
        if not der.startswith(ED25519_DER_PREFIX) or len(der) != len(ED25519_DER_PREFIX) + 32:
            raise SystemExit(f"unexpected DER shape for {name}: {der.hex()}")
        self.public = der[len(ED25519_DER_PREFIX):]

    def sign(self, payload: bytes) -> bytes:
        message, signature = self.home / f"{self.name}.msg", self.home / f"{self.name}.sig"
        message.write_bytes(payload)
        openssl("pkeyutl", "-sign", "-rawin", "-inkey", str(self.private), "-in", str(message),
                "-out", str(signature))
        return signature.read_bytes()


def payload_of(document: dict[str, Any]) -> bytes:
    """The canonical bytes a signature covers: everything but the signature block."""
    sys.path.insert(0, str(ROOT / "python" / "src"))
    from sde.canonical import canonical_bytes

    return canonical_bytes({k: v for k, v in document.items() if k != "signature"})


def write(case: str, *, model: Any, document: Any, keys: dict[str, str], expected: Any) -> None:
    target = VECTORS / case
    target.mkdir(parents=True, exist_ok=True)
    (target / "model.json").write_text(json.dumps(model, indent=2, sort_keys=True) + "\n")
    (target / "map.json").write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    (target / "keys.json").write_text(json.dumps(keys, indent=2, sort_keys=True) + "\n")
    (target / "expected.json").write_text(json.dumps(expected, indent=2, sort_keys=True) + "\n")
    print(f"  wrote {case}")


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def p1363(r: int, s: int) -> bytes:
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--i-am-changing-the-contract", action="store_true", required=True)
    parser.parse_args()
    model = json.loads((BASE / "model.json").read_text())
    document = json.loads((BASE / "map.json").read_text())
    document.pop("signature", None)
    payload = payload_of(document)
    for case in CASES:
        if (VECTORS / case).exists():
            shutil.rmtree(VECTORS / case)

    with tempfile.TemporaryDirectory() as home_name:
        home = Path(home_name)
        p1, ed = P256(home, "p1"), Ed25519(home, "ed")
        r, s = p1.sign(payload)

        def signed(value: bytes, alg: str = "es256") -> dict[str, Any]:
            return {**document, "signature": {"alg": alg, "key_id": "p1", "value": b64(value)}}

        by_p1 = signed(p1363(r, s))
        write(CASES[0], model=model, document=by_p1, keys={"": b64(p1.public)},
              expected={"verified_with": None,
                        "why": "an ES256 signature, r and s of 32 bytes each with s in the lower "
                        "half, over the same canonical bytes an Ed25519 signature covers, verified "
                        "by a P-256 key given as its uncompressed point"})
        write(CASES[1], model=model, document=by_p1,
              keys={"ec": b64(p1.public), "ed": b64(ed.public)},
              expected={"verified_with": "ec",
                        "why": "a set may hold keys of both algorithms, as a rotation from one to "
                        "the other does; the map's alg says which of them are tried, and the one "
                        "that verified is reported"})
        write(CASES[2], model=model, document=by_p1, keys={"": b64(ed.public)},
              expected={"error": "MapError",
                        "match": "is signed with es256 and none of the 1 key(s) supplied is an "
                        "ES256 (P-256) key",
                        "why": "a key of one algorithm never verifies the other's signature, and a "
                        "signature with no key of its algorithm is an unverifiable claim"})
        write(CASES[3], model=model, document=signed(ed.sign(payload), "ed25519"),
              keys={"": b64(p1.public)},
              expected={"error": "MapError",
                        "match": "is signed with ed25519 and none of the 1 key(s) supplied is an "
                        "Ed25519 key",
                        "why": "the same rule the other way round"})
        changed = {**by_p1, "map_version": by_p1["map_version"] + 1}
        write(CASES[4], model=model, document=changed, keys={"": b64(p1.public)},
              expected={"error": "MapError", "match": "does not verify against any of the 1 key",
                        "why": "a signature over the map before one number changed: the canonical "
                        "bytes differ, and so does what the signature covers"})
        if not p1.verified(payload, der_of(r, N - s)):
            raise SystemExit("openssl would not verify the upper-half signature")
        write(CASES[5], model=model, document=signed(p1363(r, N - s)), keys={"": b64(p1.public)},
              expected={"error": "MapError", "match": "s in its lower half",
                        "why": "ECDSA verifies (r, s) and (r, n - s) alike - openssl verified this "
                        "one - so a map would have two signatures; only the lower half is taken, as "
                        "Ed25519 takes one"})
        write(CASES[6], model=model, document=signed(p1363(r, s)[:63]), keys={"": b64(p1.public)},
              expected={"error": "MapError", "match": "r and s, 32 bytes each",
                        "why": "the value is 64 bytes, r then s; a byte short is not a signature"})
        x, y = int.from_bytes(p1.public[1:33], "big"), int.from_bytes(p1.public[33:], "big")
        off = b"\x04" + x.to_bytes(32, "big") + ((y + 1) % P).to_bytes(32, "big")
        write(CASES[7], model=model, document=by_p1, keys={"": b64(off)},
              expected={"error": "MapError", "match": "65 bytes that are not a point on P-256",
                        "why": "the configuration, not the map: a point off the curve is no key, and "
                        "it is refused before verification is attempted"})
        compressed = bytes([2 + (y & 1)]) + x.to_bytes(32, "big")
        write(CASES[8], model=model, document=by_p1, keys={"": b64(compressed)},
              expected={"error": "MapError",
                        "match": "33 bytes and an Ed25519 public key is 32, a P-256 public key (an "
                        "uncompressed SEC1 point) 65",
                        "why": "a P-256 key is given uncompressed; the compressed form is 33 bytes "
                        "and is refused by its length"})
        unknown = {**document, "signature": {"alg": "es384", "key_id": "p1", "value": b64(p1363(r, s))}}
        write(CASES[9], model=model, document=unknown, keys={"": b64(p1.public)},
              expected={"error": "MapError",
                        "match": "only ed25519 and es256 signatures are understood",
                        "why": "an algorithm a library does not implement is refused by name, never "
                        "verified as another"})
        # SEC1's hybrid form carries both coordinates, as the uncompressed one does, under a first
        # byte of 0x06 or 0x07; openssl parses it as the same point.
        hybrid = bytes([6 + (y & 1)]) + p1.public[1:]
        write(CASES[10], model=model, document=by_p1, keys={"": b64(hybrid)},
              expected={"error": "MapError",
                        "match": "a P-256 key is an uncompressed point, whose first byte is 0x04",
                        "why": "SEC1's hybrid form, 65 bytes beginning 0x06 or 0x07, which openssl "
                        "reads as the same point: refused by its first byte, so that every library "
                        "takes one form and none takes it through its cryptography library"})
        form = "with both above zero and below the order of P-256"
        for case, value, what in (
            (CASES[11], p1363(0, s), "r of zero"),
            (CASES[12], p1363(N, s), "r equal to the order of the curve"),
            (CASES[13], p1363(r, 0), "s of zero"),
        ):
            write(case, model=model, document=signed(value), keys={"": b64(p1.public)},
                  expected={"error": "MapError", "match": form,
                            "why": f"{what} is no ECDSA signature: refused by its form, before "
                            "verification is attempted, as one outside the range of r and s is"})
    print("done; the private keys were in a temporary directory and are gone")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

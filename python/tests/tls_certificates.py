"""Ephemeral TLS material for local SDK tests; no private key is committed or printed."""

from __future__ import annotations

import ipaddress
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def create_material(directory: Path) -> dict[str, Path]:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = datetime.now(UTC)

    def authority(label: str) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, label)])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=3))
            .not_valid_after(now + timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(key, hashes.SHA256())
        )
        return key, cert

    ca_key, ca = authority("SDE ephemeral test CA")
    _, other_ca = authority("SDE unrelated ephemeral test CA")
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def server(
        label: str, names: list[x509.GeneralName], *, expired: bool = False
    ) -> x509.Certificate:
        return (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, label)]))
            .issuer_name(ca.subject)
            .public_key(server_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=2))
            .not_valid_after(now - timedelta(days=1) if expired else now + timedelta(days=7))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256())
        )

    names = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address("::1")),
    ]
    certificates = {
        "ca": ca,
        "other_ca": other_ca,
        "server_cert": server("localhost", names),
        "wrong_host_cert": server("wrong.invalid", [x509.DNSName("wrong.invalid")]),
        "expired_cert": server("localhost", names, expired=True),
        "dns_only_cert": server("localhost", [x509.DNSName("localhost")]),
        "ip_only_cert": server(
            "127.0.0.1",
            [
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                x509.IPAddress(ipaddress.ip_address("::1")),
            ],
        ),
    }
    paths: dict[str, Path] = {}
    for name, cert in certificates.items():
        path = directory / (name + ".pem")
        with path.open("xb") as output:
            output.write(cert.public_bytes(serialization.Encoding.PEM))
        paths[name] = path
    private_path = directory / "server_key.pem"
    fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(
            server_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    paths["server_key"] = private_path
    return paths


if __name__ == "__main__":
    print(
        json.dumps({name: str(path) for name, path in create_material(Path(sys.argv[1])).items()})
    )

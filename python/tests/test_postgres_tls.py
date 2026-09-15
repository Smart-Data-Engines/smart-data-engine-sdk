"""Verify literal-IP and DNS certificate identity over real PostgreSQL TLS negotiation.

The endpoint implements only SSLRequest -> S -> TLS and observes StartupMessage before closing.
It is not a database: a failed connect after StartupMessage is the intentional positive witness
that peer verification finished, while a rejected certificate must prevent StartupMessage.
"""

from __future__ import annotations

import socket
import socketserver
import ssl
import struct
import threading
from contextlib import contextmanager
from urllib.parse import quote

import pytest

from sde import EngineError
from sde.engines.postgres import PostgresEngine


@pytest.fixture
def pg_material(tmp_path):
    from tls_certificates import create_material

    return create_material(tmp_path / "pg-tls-material")


def _exact(connection, amount):
    result = b""
    while len(result) < amount:
        piece = connection.recv(amount - len(result))
        if not piece:
            raise EOFError("local fixture connection closed")
        result += piece
    return result


@contextmanager
def pg_endpoint(material, certificate, address="127.0.0.1"):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(material[certificate], material["server_key"])
    observed = {"tcp": 0, "ssl_requests": 0, "startup": [], "tls_errors": []}

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            observed["tcp"] += 1
            self.request.settimeout(2)
            try:
                message = _exact(self.request, 8)
                if message == struct.pack("!II", 8, 80877104):  # libpq GSSENCRequest, if enabled
                    self.request.sendall(b"N")
                    message = _exact(self.request, 8)
                assert message == struct.pack("!II", 8, 80877103)
                observed["ssl_requests"] += 1
                self.request.sendall(b"S")
                with context.wrap_socket(self.request, server_side=True) as secure:
                    size = struct.unpack("!I", _exact(secure, 4))[0]
                    assert 8 <= size <= 65536
                    startup = _exact(secure, size - 4)
                    assert startup[:4] == struct.pack("!I", 196608)
                    observed["startup"].append(startup)
                    # Refuse the rest of startup: this is a certificate witness, not a fake DB.
            except ssl.SSLError as error:
                observed["tls_errors"].append(error.reason)
            except (OSError, EOFError):
                pass

    class Server(socketserver.ThreadingTCPServer):
        address_family = socket.AF_INET6 if ":" in address else socket.AF_INET

    with Server((address, 0), Handler) as server:
        worker = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        worker.start()
        try:
            yield server.server_address[1], observed
        finally:
            server.shutdown()
            worker.join(timeout=5)
            assert not worker.is_alive()


@pytest.mark.parametrize(
    ("host", "certificate", "accepted"),
    [
        ("127.0.0.1", "dns_only_cert", False),
        ("127.0.0.1", "ip_only_cert", True),
        ("localhost", "dns_only_cert", True),
        ("localhost", "ip_only_cert", False),
        ("[::1]", "dns_only_cert", False),
        ("[::1]", "ip_only_cert", True),
    ],
)
def test_verify_full_binds_the_certificate_to_the_configured_host(
    pg_material, host, certificate, accepted
):
    address = "::1" if host == "[::1]" else "127.0.0.1"
    with pg_endpoint(pg_material, certificate, address) as (port, observed):
        dsn = (
            f"postgresql://tls_probe:synthetic@{host}:{port}/tls_probe?sslmode=verify-full"
            f"&sslrootcert={quote(str(pg_material['ca']), safe='')}&connect_timeout=2"
        )
        with pytest.raises(EngineError) as raised:
            PostgresEngine(dsn).connect()
    assert observed["tcp"] >= 1
    assert observed["ssl_requests"] == 1
    assert len(observed["startup"]) == int(accepted)
    if accepted:
        assert b"user\0tls_probe\0" in observed["startup"][0]
        assert observed["tls_errors"] == []
    else:
        # libpq may complete TLS and reject the name afterwards, closing without a TLS alert.
        assert "certificate" in str(raised.value).lower()

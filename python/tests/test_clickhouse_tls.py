"""ClickHouse DSN/TLS and first-handshake boundaries, using only local socket endpoints."""

from __future__ import annotations

import socket
import socketserver
import threading
from contextlib import contextmanager
from typing import Any

import pytest

from sde import EngineError
from sde.engines.clickhouse import ClickHouseEngine


@contextmanager
def first_bytes():
    seen: list[bytes] = []

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.settimeout(1)
            prefix = b""
            while len(prefix) < 5:
                piece = self.request.recv(5 - len(prefix))
                if not piece:
                    return
                prefix += piece
            seen.append(prefix)
            # Consume the rest of an HTTP request before dropping its response. This models the
            # ambiguous remote-close path, not a connect failure before the request was accepted.
            if prefix == b"POST ":
                request = prefix
                while b"\r\n\r\n" not in request:
                    piece = self.request.recv(65536)
                    if not piece:
                        return
                    request += piece
                head, body = request.split(b"\r\n\r\n", 1)
                length = next(
                    (
                        int(line.split(b":", 1)[1])
                        for line in head.split(b"\r\n")
                        if line.lower().startswith(b"content-length:")
                    ),
                    0,
                )
                while len(body) < length:
                    piece = self.request.recv(length - len(body))
                    if not piece:
                        return
                    body += piece

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
        server.daemon_threads = True
        worker = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        worker.start()
        try:
            yield server.server_address[1], seen
        finally:
            server.shutdown()
            worker.join(timeout=5)
            assert not worker.is_alive()


def _refused_connect(dsn: str) -> None:
    with pytest.raises(EngineError), ClickHouseEngine(dsn) as engine:
        engine.connect()


@pytest.mark.parametrize("scheme", ["https", "clickhouses"])
def test_tls_scheme_sends_clienthello_on_a_nonstandard_port(scheme: str) -> None:
    with first_bytes() as (port, seen):
        _refused_connect(f"{scheme}://fixture:canary@127.0.0.1:{port}/default")
    assert seen
    assert all(prefix[:1] == b"\x16" for prefix in seen)
    assert b"POST " not in seen


def test_secure_query_sends_clienthello_on_a_nonstandard_port() -> None:
    with first_bytes() as (port, seen):
        _refused_connect(f"clickhouse://fixture:canary@127.0.0.1:{port}/default?secure=true")
    assert seen
    assert all(prefix[:1] == b"\x16" for prefix in seen)


@pytest.mark.parametrize(
    "suffix", ["verify=false", "verify=tru", "verify=", "secure=TRUE", "unknown=setting"]
)
def test_invalid_transport_settings_refuse_before_any_socket(suffix: str) -> None:
    with first_bytes() as (port, seen):
        _refused_connect(f"https://fixture:canary@127.0.0.1:{port}/default?{suffix}")
    assert seen == []


def test_first_handshake_remote_close_is_not_replayed() -> None:
    with first_bytes() as (port, seen):
        _refused_connect(f"clickhouse://fixture:canary@127.0.0.1:{port}/default")
    assert seen == [b"POST "]


def test_plain_control_reaches_the_http_listener() -> None:
    with first_bytes() as (port, seen):
        _refused_connect(f"http://fixture:canary@127.0.0.1:{port}/default")
    assert seen and all(prefix == b"POST " for prefix in seen)


def _vectors() -> list[dict[str, Any]]:
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "testdata/clickhouse-dsns.json"
    raw = json.loads(path.read_bytes())
    assert raw["protocol"] == 1
    return raw["cases"]


@pytest.mark.parametrize("case", _vectors(), ids=lambda case: case["id"])
def test_shared_connection_vectors_are_pure_and_do_not_echo_secrets(case: dict[str, Any]) -> None:
    from dataclasses import asdict

    from sde.engines._clickhouse_connection import parse_dsn

    if case.get("reject"):
        with pytest.raises(EngineError) as raised:
            parse_dsn(case["dsn"])
        assert "synthetic-secret" not in str(raised.value)
        assert case["dsn"] not in str(raised.value)
    else:
        parsed = parse_dsn(case["dsn"])
        assert asdict(parsed) == case["expected"]
        assert parsed.interface == ("https" if case["expected"]["secure"] else "http")
        assert "synthetic-secret" not in repr(parsed)


@pytest.fixture
def material(tmp_path):
    from tls_certificates import create_material

    return create_material(tmp_path / "tls-material")


class Endpoint:
    def __init__(self) -> None:
        self.connections = 0
        self.requests: list[bytes] = []
        self.tls_errors: list[str] = []
        self.tls_failure = threading.Event()
        self.status = 400
        self.location: str | None = None
        self.hold = False
        self.initialize = False
        self.port = 0
        self.stop = threading.Event()


@contextmanager
def http_endpoint(material=None, *, certificate="server_cert", address="127.0.0.1"):
    import ssl

    endpoint = Endpoint()
    context = None
    if material is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(material[certificate], material["server_key"])

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.settimeout(2)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = self.request.recv(65536)
                if not chunk:
                    return
                data += chunk
            headers, body = data.split(b"\r\n\r\n", 1)
            length = next(
                (
                    int(line.split(b":", 1)[1])
                    for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                ),
                0,
            )
            while len(body) < length:
                chunk = self.request.recv(length - len(body))
                if not chunk:
                    return
                body += chunk
            endpoint.requests.append(headers + b"\r\n\r\n" + body)
            if endpoint.hold:
                endpoint.stop.wait(5)
                return
            redirect = (
                "" if endpoint.location is None else "Location: " + endpoint.location + "\r\n"
            )
            status, payload = endpoint.status, b"no"
            if endpoint.initialize:
                if b"SELECT version(), timezone()" in body:
                    status, payload = 200, b"25.8.0.0\tUTC\n"
                elif b"FROM system.settings" in body or b"SELECT 1 AS check" in body:
                    # Empty Native block, sufficient only for this client's metadata handshake.
                    # This fixture does not model database rows or claim engine compatibility.
                    status, payload = 200, b"\0\0"
                elif body == b"SELECT 7":
                    status, payload = 200, b"7\n"
            response = (
                f"HTTP/1.1 {status} Fixture\r\n{redirect}"
                f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
            )
            self.request.sendall(response.encode() + payload)

    class Server(socketserver.ThreadingTCPServer):
        address_family = socket.AF_INET6 if ":" in address else socket.AF_INET

        def get_request(self):
            raw, address = super().get_request()
            endpoint.connections += 1
            raw.settimeout(2)
            if context is None:
                return raw, address
            try:
                return context.wrap_socket(raw, server_side=True), address
            except ssl.SSLError as error:
                endpoint.tls_errors.append(error.reason)
                endpoint.tls_failure.set()
                raw.close()
                raise

    with Server((address, 0), Handler) as server:
        server.daemon_threads = False
        endpoint.port = server.server_address[1]
        worker = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        worker.start()
        try:
            yield endpoint
        finally:
            endpoint.stop.set()
            server.shutdown()
            worker.join(timeout=5)
            assert not worker.is_alive()


def _tls_dsn(endpoint, ca, *, host="localhost", query="") -> str:
    from urllib.parse import quote

    authority = f"https://fixture:canary@{host}:{endpoint.port}/default"
    return authority + "?ca_cert=" + quote(str(ca), safe="") + query


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_custom_ca_and_dns_or_ip_identity_allow_only_authenticated_application_bytes(
    material, host
):
    address = "::1" if host == "[::1]" else "127.0.0.1"
    with http_endpoint(material, address=address) as endpoint:
        _refused_connect(_tls_dsn(endpoint, material["ca"], host=host))
        # The endpoint intentionally returns HTTP400, after successful certificate verification.
        assert endpoint.connections == 1
        assert endpoint.tls_errors == []
        assert len(endpoint.requests) == 1
        assert endpoint.requests[0].startswith(b"POST ")


@pytest.mark.parametrize("certificate", ["wrong_host_cert", "expired_cert"])
def test_invalid_peer_certificate_reaches_tls_but_never_application_bytes(material, certificate):
    with http_endpoint(material) as control:
        _refused_connect(_tls_dsn(control, material["ca"]))
        assert len(control.requests) == 1
    with http_endpoint(material, certificate=certificate) as endpoint:
        _refused_connect(_tls_dsn(endpoint, material["ca"]))
        assert endpoint.connections == 1
        assert endpoint.tls_failure.wait(1), "The TLS endpoint did not observe a certificate alert"
        assert endpoint.tls_errors
        assert endpoint.requests == []


def test_wrong_ca_cannot_poison_another_clients_trust_or_the_native_default_pool(material):
    from clickhouse_connect.driver.httpclient import HttpClient
    from clickhouse_connect.driver.httputil import all_managers, default_pool_manager

    original_method = HttpClient._init_common_settings
    options = dict(default_pool_manager().connection_pool_kw)
    managers = set(all_managers)
    with http_endpoint(material) as endpoint:
        _refused_connect(_tls_dsn(endpoint, material["ca"]))
        assert len(endpoint.requests) == 1
        _refused_connect(_tls_dsn(endpoint, material["other_ca"]))
        assert endpoint.connections == 2
        assert len(endpoint.requests) == 1
        assert endpoint.tls_failure.wait(1), "The TLS endpoint did not observe a certificate alert"
        assert endpoint.tls_errors
        _refused_connect(_tls_dsn(endpoint, material["ca"]))
        assert len(endpoint.requests) == 2
    assert default_pool_manager().connection_pool_kw == options
    assert HttpClient._init_common_settings is original_method
    assert set(all_managers) == managers


@pytest.mark.parametrize(
    "damage", ["absent", "empty", "malformed", "directory", "oversized", "fifo", "symlink-fifo"]
)
def test_ca_configuration_is_qualified_before_opening_a_socket(tmp_path, damage):
    path = tmp_path / "bad-ca.pem"
    if damage == "empty":
        path.write_bytes(b"")
    elif damage == "malformed":
        path.write_bytes(b"private-ca-content-canary, not PEM")
    elif damage == "directory":
        path.mkdir()
    elif damage == "oversized":
        path.write_bytes(b"#" * (1024 * 1024 + 1))
    elif damage in ("fifo", "symlink-fifo"):
        import os

        fifo = tmp_path / "ca-pipe"
        os.mkfifo(fifo)
        if damage == "symlink-fifo":
            path.symlink_to(fifo)
        else:
            fifo.rename(path)
    with http_endpoint() as endpoint:
        with pytest.raises(EngineError, match="CA") as raised:
            ClickHouseEngine(_tls_dsn(endpoint, path)).connect()
        assert endpoint.connections == 0
        assert endpoint.requests == []
        assert "canary" not in str(raised.value)
        assert "private-ca-content" not in str(raised.value)


def test_initial_https_redirect_cannot_reach_a_plain_http_target(material):
    with http_endpoint() as plaintext, http_endpoint(material) as endpoint:
        _refused_connect(_tls_dsn(endpoint, material["ca"]))
        assert len(endpoint.requests) == 1
        endpoint.status = 302
        endpoint.location = f"http://127.0.0.1:{plaintext.port}/redirected"
        _refused_connect(_tls_dsn(endpoint, material["ca"]))
        assert len(endpoint.requests) == 2
        assert endpoint.tls_errors == []
        assert plaintext.connections == 0
        assert plaintext.requests == []


def test_fractional_handshake_read_timeout_is_used_before_initialization(material):
    import time

    with http_endpoint(material) as endpoint:
        endpoint.hold = True
        started = time.monotonic()
        _refused_connect(
            _tls_dsn(
                endpoint, material["ca"], query="&connect_timeout=0.25&send_receive_timeout=0.125"
            )
        )
        elapsed = time.monotonic() - started
        assert len(endpoint.requests) == 1
        assert 0.08 <= elapsed < 0.8


def test_startup_wrapper_failure_closes_only_its_new_native_pool(material, monkeypatch):
    from clickhouse_connect.driver.httputil import all_managers

    import sde.engines.clickhouse as adapter

    managers = set(all_managers)

    def cannot_wrap(_pool):
        raise RuntimeError("private-startup-detail-canary")

    try:
        with http_endpoint(material) as endpoint:
            monkeypatch.setattr(adapter, "_NoReplayTransport", cannot_wrap)
            with pytest.raises(EngineError) as raised:
                ClickHouseEngine(_tls_dsn(endpoint, material["ca"])).connect()
            assert "private-startup-detail" not in str(raised.value)
            assert endpoint.connections == 0
        assert set(all_managers) == managers
    finally:
        # A red pre-fix run must not contaminate another test with its deliberately exposed leak.
        for pool in set(all_managers) - managers:
            pool.clear()
            all_managers.pop(pool, None)


def test_connected_client_pins_ca_bytes_while_new_client_reads_the_changed_file(material):
    from clickhouse_connect.driver.httputil import all_managers

    before = set(all_managers)
    with http_endpoint(material) as endpoint:
        endpoint.initialize = True
        dsn = _tls_dsn(endpoint, material["ca"])
        with ClickHouseEngine(dsn) as engine:
            assert engine._cx.server_version == "25.8.0.0"
            assert engine._cx.timeout.connect_timeout == 10.0
            assert engine._cx.timeout.read_timeout == 15.0
            initial_connections = endpoint.connections
            material["ca"].write_bytes(material["other_ca"].read_bytes())
            assert engine._cx.command("SELECT 7") == 7
            assert endpoint.connections > initial_connections  # HTTP fixture closes every socket.
            requests = len(endpoint.requests)
            _refused_connect(dsn)
            assert endpoint.tls_failure.wait(1)
            assert len(endpoint.requests) == requests
        assert set(all_managers) == before


@pytest.mark.parametrize(
    "raw",
    [
        r"https://user:canary\@localhost/db",
        r"https://user:canary@localhost/db?ca_cert=/fixture/root\ca.pem",
    ],
)
def test_raw_backslash_refuses_while_percent_encoded_field_value_is_preserved(raw):
    from sde.engines._clickhouse_connection import parse_dsn

    with pytest.raises(EngineError):
        parse_dsn(raw)
    encoded = parse_dsn(raw.replace("\\", "%5C"))
    assert "\\" in encoded.password or "\\" in (encoded.ca_cert or "")


def test_regular_ca_symlink_is_allowed_and_qualified(material, tmp_path):
    alias = tmp_path / "ca-link.pem"
    alias.symlink_to(material["ca"])
    with http_endpoint(material) as endpoint:
        _refused_connect(_tls_dsn(endpoint, alias))
        assert endpoint.connections == 1
        assert endpoint.tls_errors == []
        assert len(endpoint.requests) == 1

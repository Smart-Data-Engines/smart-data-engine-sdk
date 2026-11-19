"""The soak's TCP proxy: what an application sees through it, with no engine behind it.

Each fault the soak injects through the proxy is asserted here as the application's socket sees it:
a reset is ECONNRESET and not an end of stream, a stall moves no byte and keeps the connection, a
refusal refuses. An echo server stands in for the engine.
"""

from __future__ import annotations

import socket
import socketserver
import threading
from collections.abc import Iterator

import pytest
from qualification.soak_proxy import Proxy


class _Echo(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        try:
            self._echo()
        except ConnectionResetError:
            return  # the proxy reset its side: what a reset test asks of it

    def _echo(self) -> None:
        while True:
            data = self.request.recv(65536)
            if not data:
                # The client half-closed: answer once more, then end the stream.
                self.request.sendall(b"eof seen")
                self.request.shutdown(socket.SHUT_WR)
                return
            if data == b"bye":
                self.request.sendall(b"bye")
                self.request.shutdown(socket.SHUT_WR)
                return
            self.request.sendall(data)


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


@pytest.fixture
def echo() -> Iterator[int]:
    server = _Server(("127.0.0.1", 0), _Echo)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def proxy(echo: int) -> Iterator[Proxy]:
    running = Proxy("127.0.0.1", echo)
    running.start()
    try:
        yield running
    finally:
        running.close()


def connect(port: int) -> socket.socket:
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    client.settimeout(5)
    return client


def receive(client: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = client.recv(size - len(data))
        if not chunk:
            break
        data += chunk
    return data


def test_bytes_pass_both_ways(proxy: Proxy) -> None:
    with connect(proxy.port) as client:
        client.sendall(b"ping")
        assert receive(client, 4) == b"ping"
        payload = bytes(range(256)) * 4096
        sender = threading.Thread(target=client.sendall, args=(payload,))
        sender.start()
        assert receive(client, len(payload)) == payload
        sender.join()
    assert proxy.accepted == 1


def test_a_reset_reaches_the_application_as_a_reset(proxy: Proxy) -> None:
    with connect(proxy.port) as client:
        client.sendall(b"ping")
        assert receive(client, 4) == b"ping"
        assert proxy.reset() == 1
        with pytest.raises(ConnectionResetError):
            client.recv(16)
    # The listener is untouched: the next connection works.
    with connect(proxy.port) as client:
        client.sendall(b"again")
        assert receive(client, 5) == b"again"


def test_a_stall_moves_nothing_and_keeps_the_connection(proxy: Proxy) -> None:
    with connect(proxy.port) as client:
        client.sendall(b"before")
        assert receive(client, 6) == b"before"
        proxy.set_mode("stall")
        client.sendall(b"during")
        client.settimeout(0.5)
        with pytest.raises(TimeoutError):
            client.recv(16)
        proxy.set_mode("pass")
        client.settimeout(5)
        assert receive(client, 6) == b"during"


def test_a_refusal_resets_open_connections_and_refuses_new_ones(proxy: Proxy) -> None:
    with connect(proxy.port) as client:
        client.sendall(b"ping")
        assert receive(client, 4) == b"ping"
        proxy.set_mode("refuse")
        with pytest.raises(ConnectionResetError):
            client.recv(16)
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    proxy.set_mode("pass")
    with connect(proxy.port) as client:
        client.sendall(b"back")
        assert receive(client, 4) == b"back"


def test_an_end_of_stream_is_a_half_close_in_both_directions(proxy: Proxy) -> None:
    with connect(proxy.port) as client:
        client.sendall(b"bye")
        assert receive(client, 3) == b"bye"
        assert client.recv(16) == b""
    with connect(proxy.port) as client:
        client.shutdown(socket.SHUT_WR)
        assert receive(client, 8) == b"eof seen"
        assert client.recv(16) == b""


def test_an_engine_that_is_down_is_a_reset() -> None:
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    running = Proxy("127.0.0.1", port)
    running.start()
    try:
        with connect(running.port) as client, pytest.raises(ConnectionResetError):
            client.recv(16)
    finally:
        running.close()

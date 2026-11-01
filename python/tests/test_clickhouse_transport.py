"""Per-client no-replay transport preserves the supplied pool and its identity."""

from __future__ import annotations

from typing import Any

import pytest

from sde import EngineError
from sde.engines.clickhouse import _NoReplayTransport


class Pool:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.timeout = object()

    def request(self, method: str, url: str, **options: Any) -> Any:
        self.calls.append(options)
        return object()


def test_transport_disables_lower_layer_retry_and_keeps_pool_identity() -> None:
    pool = Pool()
    transport = _NoReplayTransport(pool)
    registry = {pool: "last reset"}
    assert registry[transport] == "last reset"
    registry[transport] = "next reset"
    assert len(registry) == 1
    assert _NoReplayTransport(pool) == transport
    assert transport.timeout is pool.timeout
    transport.request("POST", "http://example.invalid", body=b"insert", retries=3, redirect=True)
    assert pool.calls == [{"body": b"insert", "retries": False, "redirect": False}]
    assert not isinstance(pool, _NoReplayTransport)


def test_transport_error_cannot_enter_the_drivers_remote_close_retry_handler() -> None:
    from urllib3.exceptions import ProtocolError

    class Broken(Pool):
        def request(self, method: str, url: str, **options: Any) -> Any:
            self.calls.append(options)
            raise ProtocolError("transport error")

    pool = Broken()
    with pytest.raises(EngineError, match="outcome may be unknown"):
        _NoReplayTransport(pool).request("POST", "http://example.invalid", body=b"insert")
    assert len(pool.calls) == 1


SECRET_URL = "http://reader:hunter2@example.invalid:8123/?query=SELECT+secret"


def test_a_transport_failure_names_its_causes_and_none_of_their_text() -> None:
    """A reconnect failed in CI with "transport failed" and nothing else, so its log could not
    tell a server that had closed the connection from one that refused it or timed out. The
    message names the classes of the chain, outermost first; their text carries the URL."""
    from http.client import RemoteDisconnected

    from urllib3.exceptions import ProtocolError

    class Closed(Pool):
        def request(self, method: str, url: str, **options: Any) -> Any:
            raise ProtocolError(
                f"Connection aborted. {url}",
                RemoteDisconnected("Remote end closed connection without response"),
            )

    with pytest.raises(EngineError) as closed:
        _NoReplayTransport(Closed()).request("POST", SECRET_URL, body=b"insert")
    message = str(closed.value)
    assert "ClickHouse transport failed (ProtocolError <- RemoteDisconnected);" in message
    assert "not replayed" in message
    assert "outcome may be unknown" in message
    for text in ("example.invalid", "hunter2", "secret", "Remote end", "aborted"):
        assert text not in message, text


def test_a_refused_connection_is_named_by_the_error_that_caused_it() -> None:
    """urllib3 raises ``NewConnectionError`` from the socket's own error, so the chain is
    ``__cause__``, not an argument, and a cause that is also the context is named once."""
    import socket

    import urllib3

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    transport = _NoReplayTransport(urllib3.PoolManager())
    with pytest.raises(EngineError) as refused:
        transport.request("GET", f"http://127.0.0.1:{port}/?query=SELECT+secret")
    message = str(refused.value)
    assert "ClickHouse transport failed (NewConnectionError <- ConnectionRefusedError);" in message
    assert str(port) not in message
    assert "secret" not in message


def test_a_chain_that_loops_is_named_once_round() -> None:
    from urllib3.exceptions import ProtocolError

    outer = ProtocolError("outer")
    inner = OSError("inner")
    outer.__cause__ = inner
    inner.__context__ = outer

    class Looping(Pool):
        def request(self, method: str, url: str, **options: Any) -> Any:
            raise outer

    with pytest.raises(EngineError, match=r"transport failed \(ProtocolError <- OSError\);"):
        _NoReplayTransport(Looping()).request("GET", SECRET_URL)


def test_a_connection_the_driver_could_not_open_names_its_classes_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sde.engines.clickhouse as adapter

    def refused(_parameters: Any) -> Any:
        # Raised while handling the reset, without ``from``, as a driver may: the chain is then
        # ``__context__`` alone.
        try:
            raise ConnectionResetError(f"reset by {SECRET_URL}")
        except ConnectionResetError:
            raise RuntimeError(f"driver could not read {SECRET_URL}")  # noqa: B904

    monkeypatch.setattr(adapter, "_connect_client", refused)
    engine = adapter.ClickHouseEngine("clickhouse://reader:hunter2@example.invalid:8123/default")
    with pytest.raises(EngineError) as failed:
        engine.connect()
    message = str(failed.value)
    assert message == (
        "could not connect to ClickHouse with the configured transport "
        "(RuntimeError <- ConnectionResetError)"
    )


@pytest.mark.parametrize("installed", ["0.7.0", "1.7.1", "1.7.2rc1", "1.8.0.dev1"])
def test_unsupported_driver_refuses_before_opening_a_connection(
    installed: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sde.engines.clickhouse as adapter

    monkeypatch.setattr(adapter, "version", lambda _name: installed)
    with pytest.raises(EngineError, match="timestamp and structured"):
        adapter.ClickHouseEngine("clickhouse://localhost:1/default")


@pytest.mark.parametrize("installed", ["1.7.2", "1.7.2.post1", "1.8.0", "2.0.0"])
def test_stable_compatible_driver_versions_are_accepted(installed: str) -> None:
    from sde.engines.clickhouse import _require_driver_version

    _require_driver_version(installed)


def test_distribution_floor_matches_the_pre_connection_refusal() -> None:
    import tomllib
    from pathlib import Path

    from sde.engines.clickhouse import MIN_CLICKHOUSE_CONNECT

    metadata = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert metadata["project"]["optional-dependencies"]["clickhouse"] == [
        f"clickhouse-connect>={MIN_CLICKHOUSE_CONNECT}"
    ]

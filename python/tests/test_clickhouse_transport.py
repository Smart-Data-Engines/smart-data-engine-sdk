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

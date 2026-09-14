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

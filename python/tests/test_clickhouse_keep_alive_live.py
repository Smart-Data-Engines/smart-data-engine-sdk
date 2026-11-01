"""A ClickHouse client has a pool of its own and sends nothing on a connection the server may be
closing.

ClickHouse closes a kept-alive connection the keep-alive timeout it announces after its response.
A request sent while that close is on its way is never read, and without a replay it fails with an
outcome unknown. Clients without a CA of their own shared the driver's default pool, so a client
connecting after a pause took a connection another one had left idle; in CI that was a reconnect
after the watchdog test's ten-second wait, a hair under the server's ten seconds.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

CH_DSN = os.environ.get("SDE_CLICKHOUSE_DSN")

pytestmark = pytest.mark.skipif(
    not CH_DSN, reason="needs a live ClickHouse: its keep-alive timeout is measured here"
)


@pytest.fixture
def engines() -> Iterator[list[Any]]:
    assert CH_DSN
    from sde.engines.clickhouse import ClickHouseEngine

    opened = [ClickHouseEngine(CH_DSN), ClickHouseEngine(CH_DSN)]
    for engine in opened:
        engine.connect()
    yield opened
    for engine in opened:
        engine.close()


def test_each_client_has_a_pool_of_its_own_and_closes_it(engines: list[Any]) -> None:
    from clickhouse_connect.driver import httputil

    first, second = (engine._client.http for engine in engines)
    assert first.pool is not second.pool
    assert httputil.default_pool_manager() not in (first.pool, second.pool)
    assert first.pool in httputil.all_managers
    engines[0].close()
    assert len(first.pool.pools) == 0, "closing the client closes its connections"
    assert first.pool not in httputil.all_managers
    assert engines[1]._cx.query("SELECT 1").result_rows == [(1,)]


def test_an_idle_connection_is_not_reused_once_half_the_servers_keep_alive_has_passed(
    engines: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import sde.engines.clickhouse as adapter

    engine = engines[0]
    transport = engine._client.http
    [(keep_alive,)] = engine._cx.query(
        "SELECT toFloat64(value) FROM system.server_settings WHERE name = 'keep_alive_timeout'"
    ).result_rows
    assert transport._idle_limit == keep_alive / 2, "the limit is read from what the server says"
    cleared: list[int] = []
    clear = transport.pool.clear

    def counted() -> None:
        cleared.append(1)
        clear()

    monkeypatch.setattr(transport.pool, "clear", counted)
    real = adapter._now
    monkeypatch.setattr(adapter, "_now", lambda: real() + keep_alive / 2 - 0.5)
    assert engine._cx.query("SELECT 1").result_rows == [(1,)]
    assert cleared == [], "a connection idle for less is reused"
    monkeypatch.setattr(adapter, "_now", lambda: real() + keep_alive)
    assert engine._cx.query("SELECT 1").result_rows == [(1,)]
    assert cleared == [1], "and one idle for half the keep-alive is not"

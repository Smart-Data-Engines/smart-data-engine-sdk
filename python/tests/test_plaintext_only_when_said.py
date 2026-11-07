"""Plain transport to a host other than the loopback only when the connection string says so.

specs/secure-connections. A default is nobody's decision. For a DSN that says nothing about TLS,
libpq's default, ``prefer``, verifies nobody, and ClickHouse's is plain HTTP: both put credentials
and rows on a network the library knows nothing about. Each case below is decided on the text,
before anything connects: the adapters are constructed and never connected.
"""

from __future__ import annotations

import pytest

import sde
from sde.engines._clickhouse_connection import parse_dsn as clickhouse_dsn
from sde.engines._transport import loopback


@pytest.mark.parametrize(
    "host, expected",
    [
        ("localhost", True), ("LOCALHOST", True), ("127.0.0.1", True), ("127.8.9.10", True),
        ("::1", True), ("[::1]", True), ("db.example.com", False), ("10.0.0.5", False),
        ("::ffff:127.0.0.1", True), ("::ffff:10.0.0.5", False), ("localhost.example.com", False),
        ("0.0.0.0", False),
    ],
)
def test_the_loopback_is_decided_on_the_text(host: str, expected: bool) -> None:
    assert loopback(host) is expected


REFUSED_POSTGRES = [
    "postgresql://app:secret@db.example.com:5432/sde",
    "postgresql://app:secret@10.0.0.5/sde",
    "host=db.example.com dbname=sde user=app",
    "host=127.0.0.1,db.example.com dbname=sde",
    "hostaddr=10.0.0.5 dbname=sde",
]
ACCEPTED_POSTGRES = [
    "postgresql://app:secret@127.0.0.1:55432/sde",
    "postgresql://app:secret@localhost/sde",
    "postgresql://app:secret@[::1]/sde",
    "host=/var/run/postgresql dbname=sde",
    "dbname=sde",
    "postgresql://app:secret@db.example.com/sde?sslmode=verify-full",
    "postgresql://app:secret@db.example.com/sde?sslmode=disable",
    "host=db.example.com dbname=sde sslmode=require",
]


@pytest.mark.parametrize("dsn", REFUSED_POSTGRES)
def test_postgres_refuses_a_remote_host_with_no_tls_policy(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("psycopg")
    from sde.engines.postgres import PostgresEngine

    for name in ("PGSSLMODE", "PGHOST", "PGHOSTADDR"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(sde.EngineError, match="sslmode=verify-full") as refused:
        PostgresEngine(dsn)
    assert "secret" not in str(refused.value)


@pytest.mark.parametrize("dsn", ACCEPTED_POSTGRES)
def test_postgres_accepts_the_loopback_a_socket_and_any_stated_policy(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("psycopg")
    from sde.engines.postgres import PostgresEngine

    for name in ("PGSSLMODE", "PGHOST", "PGHOSTADDR"):
        monkeypatch.delenv(name, raising=False)
    PostgresEngine(dsn)  # constructed, never connected


def test_postgres_reads_the_environment_as_libpq_does(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host from PGHOST is a host; a policy from PGSSLMODE is a policy someone set."""
    pytest.importorskip("psycopg")
    from sde.engines.postgres import PostgresEngine

    for name in ("PGSSLMODE", "PGHOST", "PGHOSTADDR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PGHOST", "db.example.com")
    with pytest.raises(sde.EngineError, match="TLS policy"):
        PostgresEngine("dbname=sde")
    monkeypatch.setenv("PGSSLMODE", "verify-full")
    PostgresEngine("dbname=sde")


@pytest.mark.parametrize(
    "dsn",
    [
        "clickhouse://app:secret@ch.example.com:8123/sde",
        "http://app:secret@ch.example.com/sde",
        "clickhouse://app:secret@10.0.0.5/sde",
    ],
)
def test_clickhouse_refuses_plain_http_to_a_remote_host_unless_it_is_stated(dsn: str) -> None:
    with pytest.raises(sde.EngineError, match="explicit secure=false") as refused:
        clickhouse_dsn(dsn)
    assert "secret" not in str(refused.value)
    stated = clickhouse_dsn(dsn + "?secure=false")
    assert stated.secure is False


@pytest.mark.parametrize(
    "dsn",
    [
        "clickhouse://default:sde@127.0.0.1:58123/sde",
        "http://default@localhost:8123/sde",
        "clickhouse://default@[::1]:8123/sde",
        "https://app:secret@ch.example.com/sde",
        "clickhouses://app:secret@ch.example.com/sde",
        "clickhouse://app:secret@ch.example.com/sde?secure=true",
    ],
)
def test_clickhouse_accepts_the_loopback_and_tls(dsn: str) -> None:
    clickhouse_dsn(dsn)

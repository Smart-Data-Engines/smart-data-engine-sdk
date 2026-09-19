"""Administrative cleanup budgets must not change runtime credentials or TLS intent."""

from urllib.parse import parse_qsl, urlsplit

import pytest
from test_runtime_privileges_live import cleanup_dsn

from sde.engines._clickhouse_connection import parse_dsn


@pytest.mark.parametrize("timeout", [None, "15", "450.25"])
def test_cleanup_keeps_tls_identity_and_existing_longer_budget(timeout: str | None) -> None:
    dsn = "https://user:synthetic@db.example:9443/db?ca_cert=%2Fsecure%2Fca%20cert.pem&connect_timeout=0.5"
    if timeout is not None:
        dsn += "&send_receive_timeout=" + timeout
    before = parse_dsn(dsn)
    changed = cleanup_dsn(dsn)
    after = parse_dsn(changed)
    assert after.host == before.host and after.port == before.port
    assert after.username == before.username and after.password == before.password
    assert after.secure is True and after.ca_cert == before.ca_cert
    assert after.database == before.database and after.connect_timeout == before.connect_timeout
    assert after.send_receive_timeout == max(300.0, float(timeout or 0))
    assert (
        len([k for k, _ in parse_qsl(urlsplit(changed).query) if k == "send_receive_timeout"]) == 1
    )
    assert parse_dsn(dsn) == before

"""A login ClickHouse would answer short, saying nothing, gets an error through this adapter.

ClickHouse ends an answer early without an error when a limit is reached under an overflow mode of
``break``, and a profile's ``limit``, ``offset`` and ``additional_result_filter`` shorten one the
same way. The adapter says ``throw``, and no limit and no filter of the profile's, with every
statement (``WHOLE_ANSWERS``). Each case here first shows its premise on the server it runs
against - the same statement, sent as it is over plain HTTP by this login, comes back short and
with no error - and then what the adapter gets.
"""

from __future__ import annotations

import base64
import os
import secrets
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

from sde import EngineError
from sde.engines.clickhouse import WHOLE_ANSWERS, ClickHouseEngine

ROWS = 20_000
"""The table's rows. Read in blocks of 100, which is what makes a cap bite at a block's edge."""

CAP = 5000
"""A cap on a result that the table is past and the driver's own first statements are not: it
reads the server's settings when it connects, some 1500 rows of them."""

STREAM = "SELECT id FROM {table} FINAL ORDER BY (id) FORMAT TSV"

LIMIT_REACHED = r"Limit for result exceeded|\bcode: 396\b"
"""``TOO_MANY_ROWS_OR_BYTES``: what a cap on a result is under ``throw``."""


def _dsn() -> str:
    dsn = os.environ.get("SDE_CLICKHOUSE_DSN")
    if not dsn:
        pytest.skip("SDE_CLICKHOUSE_DSN is required: this is about a live server's own settings")
    return dsn


class Login:
    """One login with a settings profile of its own, and both ways to ask the server as it."""

    def __init__(self, dsn: str, database: str, user: str, password: str) -> None:
        parts = urlsplit(dsn)
        host = parts.netloc.rsplit("@", 1)[-1]
        self.database, self.user, self._password = database, user, password
        self.dsn = urlunsplit(
            (parts.scheme, f"{user}:{password}@{host}", "/" + database, parts.query, "")
        )
        scheme = "https" if parts.scheme in ("clickhouses", "https") else "http"
        self._http = f"{scheme}://{host}"

    def as_it_is(self, sql: str) -> list[str]:
        """The statement over plain HTTP, with nothing of the adapter's: the server's own answer."""
        request = urllib.request.Request(
            f"{self._http}/?{urlencode({'database': self.database})}",
            data=sql.encode(),
            method="POST",
        )
        token = base64.b64encode(f"{self.user}:{self._password}".encode()).decode()
        request.add_header("Authorization", "Basic " + token)
        with urllib.request.urlopen(request, timeout=30) as response:
            return [line for line in response.read().decode().splitlines() if line]


@contextmanager
def login(settings: str) -> Iterator[tuple[Login, str]]:
    """A database with one table of ``ROWS`` rows, and a login whose profile is ``settings``."""
    dsn = _dsn()
    if urlsplit(dsn).scheme in ("clickhouses", "https"):
        pytest.skip("the premise is shown over plain HTTP, as the test engines are reached")
    name = "sde_whole_" + uuid4().hex[:16]
    user, password = name + "_login", secrets.token_hex(24)
    with ClickHouseEngine(dsn) as root:
        try:
            root._cx.command(f"CREATE DATABASE {name} ENGINE=Atomic")
            root._cx.command(
                f"CREATE TABLE {name}.events (id Int64, value Int32) "
                f"ENGINE = ReplacingMergeTree ORDER BY (id)"
            )
            root._cx.command(
                f"INSERT INTO {name}.events SELECT number, number % 7 FROM numbers({ROWS})"
            )
            root._cx.command(
                f"CREATE USER {user} IDENTIFIED WITH sha256_password BY '{password}' "
                f"SETTINGS max_block_size = 100{', ' + settings if settings else ''}"
            )
            root._cx.command(f"GRANT SELECT ON {name}.events TO {user}")
            root._cx.command(f"GRANT SELECT ON system.settings TO {user}")
            yield Login(dsn, name, user, password), "events"
        finally:
            root._cx.command(f"DROP USER IF EXISTS {user}")
            root._cx.command(f"DROP DATABASE IF EXISTS {name}")


def _all(engine: Any, table: str) -> list[int]:
    return [row["id"] for row in engine.key_range(table, ["id"])]


def test_the_modes_this_adapter_sends_are_the_modes_this_server_has() -> None:
    """Every overflow mode the server lists is one the adapter says ``throw`` for. A release of
    ClickHouse that adds one turns this red on the day CI first runs against it."""
    with ClickHouseEngine(_dsn()) as engine:
        listed = {
            row[0]: row[1]
            for row in engine._cx.query(
                "SELECT name, value FROM system.settings WHERE name LIKE '%overflow_mode%'"
            ).result_rows
        }
    sent = {name for name in WHOLE_ANSWERS if "overflow_mode" in name}
    assert set(listed) == sent
    # And as the adapter's own statement saw them: each one ``throw``.
    assert set(listed.values()) == {"throw"}


def test_a_login_nobody_configured_reads_as_it_did() -> None:
    with login("") as (role, table), ClickHouseEngine(role.dsn) as engine:
        assert len(role.as_it_is(STREAM.format(table=table))) == ROWS
        assert _all(engine, table) == list(range(ROWS))
        assert engine.count(table) == ROWS


def test_a_result_capped_with_break_is_an_error_and_not_the_first_rows() -> None:
    with login(f"max_result_rows = {CAP}, result_overflow_mode = 'break'") as (role, table):
        short = role.as_it_is(STREAM.format(table=table))
        assert 0 < len(short) < ROWS, "the premise: this server cuts the answer and says nothing"
        with ClickHouseEngine(role.dsn) as engine:
            # The server's limit, by its text or, where the driver could not read the text of an
            # error sent in a compressed stream, by its code.
            with pytest.raises(EngineError, match=LIMIT_REACHED):
                _all(engine, table)
            read = 0
            with pytest.raises(EngineError, match=LIMIT_REACHED):
                for chunk in engine.key_stream(table, ["id"], chunk_rows=100):
                    read += len(chunk)
            assert read < ROWS
            # Under the cap nothing changes: a chunk, a row by its key, a count.
            assert len(engine.key_range(table, ["id"], limit=100)) == 100
            assert engine.get(table, {"id": 7}) == {"id": 7, "value": 0}
            assert engine.count(table) == ROWS


@pytest.mark.parametrize(
    ("settings", "refused"),
    [
        (f"max_result_rows = {CAP}, result_overflow_mode = 'break' CONST", "result_overflow_mode"),
        (f"max_result_rows = {CAP}, result_overflow_mode = 'break', readonly = 1",
         "result_overflow_mode"),
        ("limit = 10 CONST", "limit"),
    ],
    ids=["a constrained mode", "a read-only login", "a constrained limit"],
)
def test_a_login_that_cannot_say_throw_is_refused_by_the_settings_name(
    settings: str, refused: str
) -> None:
    """The server would have answered it short, and it may not be told otherwise: the first
    statement is refused, and the error names the setting and none of the server's own text."""
    with login(settings) as (role, table):
        short = role.as_it_is(STREAM.format(table=table))
        assert 0 < len(short) < ROWS, "the premise: this server cuts the answer and says nothing"
        engine = ClickHouseEngine(role.dsn)
        with pytest.raises(EngineError) as caught:
            engine.connect()
        message = str(caught.value)
        assert f"this ClickHouse login's {refused} is not {WHOLE_ANSWERS[refused]!r}" in message
        assert "may not change it" in message
        assert role.user not in message and "DB::Exception" not in message


def test_a_read_only_login_with_a_limit_is_refused_or_read_whole_and_never_short() -> None:
    """Which of the two is the server's: 24.8 lets a read-only login change nothing and refuses
    the first statement, 26.9 lets one change ``limit`` and ``offset`` and answers whole."""
    with login("limit = 10, readonly = 1") as (role, table):
        assert len(role.as_it_is(STREAM.format(table=table))) == 10, "the premise"
        engine = ClickHouseEngine(role.dsn)
        try:
            engine.connect()
        except EngineError as refused:
            assert "this ClickHouse login's limit is not '0'" in str(refused)
            return
        try:
            assert _all(engine, table) == list(range(ROWS))
        finally:
            engine.close()


def test_a_read_only_login_with_the_servers_own_modes_is_not_refused() -> None:
    """The control of the refusals: a value equal to the login's own is accepted from a login
    that may change nothing."""
    with login("readonly = 1") as (role, table), ClickHouseEngine(role.dsn) as engine:
        assert _all(engine, table) == list(range(ROWS))
        assert engine.count(table) == ROWS


@pytest.mark.parametrize(
    "settings",
    ["limit = 10", f"offset = {ROWS - 5}", "additional_result_filter = 'id < 10'"],
    ids=["a limit", "an offset", "a filter on every result"],
)
def test_a_profile_that_shortens_every_answer_does_not_shorten_this_adapters(
    settings: str,
) -> None:
    with login(settings) as (role, table):
        short = role.as_it_is(STREAM.format(table=table))
        assert 0 < len(short) < ROWS, "the premise: this server shortens the answer by itself"
        with ClickHouseEngine(role.dsn) as engine:
            assert _all(engine, table) == list(range(ROWS))
            assert engine.count(table) == ROWS


def test_a_statement_cut_off_by_time_is_an_error_and_not_an_empty_answer() -> None:
    """The mode that answered nothing at all, a count included, and no error."""
    with login("max_execution_time = 0.000001, timeout_overflow_mode = 'break'") as (role, table):
        assert role.as_it_is(STREAM.format(table=table)) == []
        assert role.as_it_is(f"SELECT count() FROM {table} FINAL FORMAT TSV") == []
        engine = ClickHouseEngine(role.dsn)
        try:
            with pytest.raises(EngineError):
                engine.connect()
                engine.count(table)
        finally:
            engine.close()


def test_what_the_driver_would_drop_or_refuse_of_a_setting_does_not_decide_it() -> None:
    """The driver has a process-wide choice of what to do with a setting it thinks a login may not
    send: refuse it, send it, or drop it and say nothing. The settings here do not pass through
    that choice, so an application that set it to ``drop`` still gets the limit as an error."""
    from clickhouse_connect import common

    before = common.get_setting("invalid_setting_action")
    common.set_setting("invalid_setting_action", "drop")
    try:
        with (
            login(f"max_result_rows = {CAP}, result_overflow_mode = 'break'") as (role, table),
            ClickHouseEngine(role.dsn) as engine,
        ):
            assert 0 < len(role.as_it_is(STREAM.format(table=table))) < ROWS, "the premise"
            with pytest.raises(EngineError, match=LIMIT_REACHED):
                _all(engine, table)
    finally:
        common.set_setting("invalid_setting_action", before)

"""What a ClickHouse login's settings do to its answers, and what a statement can say about it.

Plain HTTP and the standard library, so nothing a driver decides is in the way. For each login -
one a profile gave a limit under ``break``, one that may change no setting, one with a limit or a
filter of the profile's on every result - the same statement is sent as it is and then with the
settings the libraries send with every statement (``WHOLE_ANSWERS`` in each adapter), and what came
back is recorded: how many rows, or the server's error.

Against a server whose administrator may create users, such as a throwaway one:

    docker run -d --name probe -p 127.0.0.1:58124:8123 -e CLICKHOUSE_USER=default \
        -e CLICKHOUSE_PASSWORD=probe -e CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1 \
        -e CLICKHOUSE_DB=probe clickhouse/clickhouse-server:24.8-alpine
    python3 probe_login_settings.py http://127.0.0.1:58124 default probe > login-settings-24.8.json
"""

from __future__ import annotations

import base64
import http.client
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE, ADMIN, PASSWORD = sys.argv[1], sys.argv[2], sys.argv[3]
ROWS = 200_000

SAID = {
    **{
        f"{limit}_overflow_mode": "throw"
        for limit in (
            "distinct", "group_by", "join", "read", "result", "set", "sort", "timeout", "transfer",
        )
    },
    "read_overflow_mode_leaf": "throw",
    "timeout_overflow_mode_leaf": "throw",
    "limit": "0",
    "offset": "0",
    "additional_table_filters": "{}",
    "additional_result_filter": "",
}
"""What the adapters send with every statement."""


def ask(sql: str, user: str = ADMIN, password: str = PASSWORD, **settings: str) -> tuple[int, str]:
    """The HTTP status and the body, an error's too."""
    query = urllib.parse.urlencode({"database": "probe", **settings})
    request = urllib.request.Request(f"{BASE}/?{query}", data=sql.encode(), method="POST")
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    request.add_header("Authorization", "Basic " + token)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            try:
                return response.status, response.read().decode()
            except http.client.IncompleteRead as cut:
                # The server ended the body without ending it: no last chunk. Status 0 here.
                return 0, f"{response.status}, then the body was cut after {len(cut.partial)} bytes"
    except urllib.error.HTTPError as refused:
        return refused.code, refused.read().decode()


def admin(sql: str) -> str:
    status, body = ask(sql)
    if status != 200:
        raise SystemExit(f"{sql!r} failed: {body.strip()[:300]}")
    return body


def rows(answer: tuple[int, str]) -> Any:
    """How many lines came back, or the first line of the server's error."""
    status, body = answer
    if status == 0:
        return "CUT " + body
    if status != 200:
        return "ERROR " + body.strip().splitlines()[0][:200]
    return len([line for line in body.splitlines() if line])


def json_answer(answer: tuple[int, str]) -> dict[str, Any]:
    """A ``FORMAT JSON`` answer as a reader of its ``data`` alone would see it, and what else it
    held: the status it came under, and the exception the server put beside the rows."""
    status, body = answer
    if status == 0:
        return {"status": body}
    seen: dict[str, Any] = {"status": status}
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        seen["parses"] = False
        seen["says"] = next(
            (line.strip()[:160] for line in body.splitlines() if "DB::Exception" in line), None
        )
        return seen
    seen["parses"] = True
    seen["rows_in_data"] = len(parsed.get("data", []))
    if "exception" in parsed:
        seen["exception"] = str(parsed["exception"])[:160]
    return seen


LOGINS = {
    "nobody configured it": "",
    "a result capped, break": "max_result_rows = 1000, result_overflow_mode = 'break'",
    "the same, the mode constrained":
        "max_result_rows = 1000, result_overflow_mode = 'break' CONST",
    "the same, read-only": "max_result_rows = 1000, result_overflow_mode = 'break', readonly = 1",
    "read-only, the server's own modes": "readonly = 1",
    "rows to read capped, break": "max_rows_to_read = 50000, read_overflow_mode = 'break'",
    "time capped, break": "max_execution_time = 0.001, timeout_overflow_mode = 'break'",
    "a limit": "limit = 10",
    "a limit, read-only": "limit = 10, readonly = 1",
    "an offset": f"offset = {ROWS - 5}",
    "a filter on every result": "additional_result_filter = 'id < 10'",
    "a result capped, throw": "max_result_rows = 1000",
}

STREAM = "SELECT * FROM probe.t FINAL ORDER BY (id) FORMAT TSV"
COUNT = "SELECT count() FROM probe.t FINAL FORMAT TSV"
AS_JSON = "SELECT id FROM probe.t FINAL ORDER BY (id) FORMAT JSON"


def main() -> int:
    report: dict[str, Any] = {"version": admin("SELECT version()").strip(), "rows": ROWS}
    report["overflow_modes"] = dict(
        line.split("\t")
        for line in admin(
            "SELECT name, value FROM system.settings WHERE name LIKE '%overflow_mode%' "
            "ORDER BY name"
        ).splitlines()
    )
    admin("DROP TABLE IF EXISTS probe.t")
    admin("CREATE TABLE probe.t (id Int64, value Int32) ENGINE = ReplacingMergeTree ORDER BY id")
    # Ten parts, kept as ten: what a cap on rows read cuts depends on how many parts there are.
    admin("SYSTEM STOP MERGES probe.t")
    for start in range(0, ROWS, 20_000):
        admin(f"INSERT INTO probe.t SELECT number, number % 1000 FROM numbers({start}, 20000)")
    report["parts"] = int(
        admin(
            "SELECT count() FROM system.parts WHERE database = 'probe' AND table = 't' AND active"
        )
    )
    # An error raised after an answer has begun: the status has been sent, so it cannot say. A
    # result capped under the server's own mode, ``throw``, read in blocks small enough that the
    # first of them are on their way when the cap is reached; and the same with the server asked
    # to hold the answer until the statement ends.
    began = "SELECT number AS id FROM numbers(20000) FORMAT JSON"
    capped = {"max_result_rows": "5000", "max_block_size": "100"}
    report["an error after the answer began"] = {
        "as the statement is sent": json_answer(ask(began, **capped)),
        "with wait_end_of_query=1": json_answer(ask(began, **capped, wait_end_of_query="1")),
    }
    report["logins"] = {}
    try:
        for label, settings in LOGINS.items():
            admin("DROP USER IF EXISTS probe_login")
            admin(
                "CREATE USER probe_login IDENTIFIED WITH plaintext_password BY 'pw'"
                + (f" SETTINGS {settings}" if settings else "")
            )
            admin("GRANT SELECT ON probe.t TO probe_login")
            me = ("probe_login", "pw")
            report["logins"][label] = {
                "settings": settings,
                "rows as it is": rows(ask(STREAM, *me)),
                "count as it is": ask(COUNT, *me)[1].strip()[:160],
                "rows, saying so": rows(ask(STREAM, *me, **SAID)),
                "count, saying so": ask(COUNT, *me, **SAID)[1].strip()[:160],
                "as JSON, as it is": json_answer(ask(AS_JSON, *me)),
                "as JSON, saying so": json_answer(ask(AS_JSON, *me, **SAID)),
            }
    finally:
        admin("DROP USER IF EXISTS probe_login")
        admin("DROP TABLE IF EXISTS probe.t")
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

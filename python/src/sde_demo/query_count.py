"""Execute the Weather demo's approved COUNT locally; business values never enter receipts.

This is deliberately a closed COUNT(*) grammar over the current source, not a general SQL runner.
The metadata constructor is pure and can be used by the controller without importing drivers.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import sde
from sde.generation import check_map_project
from sde.schema import QUOTE

from .model import model
from .project import DemoRefused, config, credentials, engine, public_keys, write

KIND = "sde-weather-count-query"
FIELDS = {
    "kind",
    "protocol",
    "project_id",
    "name",
    "revision",
    "map_fingerprint",
    "query",
    "digest",
}


def _bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        + "\n"
    ).encode()


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        _bytes({key: item for key, item in value.items() if key != "digest"})
    ).hexdigest()


def _source(placement: sde.PlacementMap, project_id: str) -> sde.Materialization:
    check_map_project(placement, project_id)
    if (
        placement.contract != 4
        or not placement.signed
        or placement.fingerprint is None
        or placement.model_version != model().version
        or set(placement.groups) != {"WeatherReading"}
    ):
        raise DemoRefused("The count demo needs the loaded, signed Weather project map.")
    group = placement.groups["WeatherReading"]
    if len(group.all()) != 1:
        raise DemoRefused("Execute the count demo before staging or after completed cutover.")
    return group.source


def count_sql(sql: Any, *, table: str, dialect: str) -> str:
    """Permit exactly one count over the named source; no expressions, settings or comments."""
    if (
        not isinstance(sql, str)
        or not 1 <= len(sql) <= 8192
        or dialect not in ("postgres", "clickhouse")
    ):
        raise DemoRefused("The demo accepts one bounded COUNT(*) statement on its current source.")
    identifier = re.escape(QUOTE[dialect](table))
    if re.fullmatch(r"[a-z_][a-z0-9_]*", table):
        # PostgreSQL folds unquoted names; quoted names and ClickHouse names remain exact.
        bare = "(?i:" + re.escape(table) + ")" if dialect == "postgres" else re.escape(table)
        quoted_alternative = "|" + re.escape('"' + table + '"') if dialect == "clickhouse" else ""
        identifier = "(?:" + identifier + "|" + bare + quoted_alternative + ")"
    alias_name = r'[A-Za-z_][A-Za-z0-9_]*|"[A-Za-z_][A-Za-z0-9_]*"'
    if dialect == "clickhouse":
        alias_name += r"|`[A-Za-z_][A-Za-z0-9_]*`"
    alias = r"(?:\s+(?i:AS)\s+(?:" + alias_name + r"))?"
    final = r"\s+(?i:FINAL)" if dialect == "clickhouse" else ""
    pattern = (
        r"\s*(?i:SELECT)\s+(?i:COUNT)\s*\(\s*\*\s*\)"
        + alias
        + r"\s+(?i:FROM)\s+"
        + identifier
        + final
        + r"\s*;?\s*"
    )
    # SQL token folding is ASCII. Unicode IGNORECASE would also accept dotless i/long s/Kelvin
    # as another identifier and could execute a count on a different accessible native table.
    if re.fullmatch(pattern, sql, flags=re.ASCII) is None:
        raise DemoRefused(
            "Use SELECT COUNT(*) [AS alias] FROM the exact Weather source "
            "(with FINAL on ClickHouse); other SQL is outside this demo."
        )
    return sql


def _query(value: Any, placement: sde.PlacementMap, project_id: str, dialect: str) -> str:
    source = _source(placement, project_id)
    if not isinstance(value, dict) or not isinstance(value.get("stamp"), dict):
        raise DemoRefused("Supply the approved query record, including its stamp.")
    stamp = value["stamp"]
    if (
        value.get("group") != "WeatherReading"
        or value.get("engine") != source.engine
        or value.get("materialization") != source.id
        or value.get("dialect") != dialect
        or stamp.get("model_version") != placement.model_version
        or type(stamp.get("map_version")) is not int
        or stamp["map_version"] != placement.map_version
        or stamp.get("materialization") != source.id
    ):
        raise DemoRefused("The query does not describe this project's current source and map.")
    return count_sql(
        value.get("sql"), table=source.layout.table_for("WeatherReading"), dialect=dialect
    )


def make_count_request(
    *,
    project_id: str,
    name: str,
    revision: int,
    query: Mapping[str, Any],
    placement: sde.PlacementMap,
    dialect: str,
) -> dict[str, Any]:
    """Prepare metadata for an authenticated handoff; no connection or customer file is used."""
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 128
        or type(revision) is not int
        or revision < 1
    ):
        raise DemoRefused("The count handoff needs a query name and positive integer revision.")
    copied = json.loads(_bytes(dict(query)))
    _query(copied, placement, project_id, dialect)
    document = {
        "kind": KIND,
        "protocol": 1,
        "project_id": project_id,
        "name": name,
        "revision": revision,
        "map_fingerprint": placement.fingerprint,
        "query": copied,
    }
    document["digest"] = _digest(document)
    return document


@dataclass(frozen=True)
class CountRequest:
    name: str
    revision: int
    sql: str
    engine: str
    dialect: str
    digest: str
    map_fingerprint: str


def load_count_request(
    value: Mapping[str, Any],
    *,
    project_id: str,
    placement: sde.PlacementMap,
    dialects: Mapping[str, str],
) -> CountRequest:
    copied = json.loads(_bytes(dict(value)))
    if (
        set(copied) != FIELDS
        or copied["kind"] != KIND
        or type(copied["protocol"]) is not int
        or copied["protocol"] != 1
        or copied["project_id"] != project_id
        or copied["map_fingerprint"] != placement.fingerprint
        or copied["digest"] != _digest(copied)
    ):
        raise DemoRefused(
            "The count handoff is incomplete, changed or belongs to another map/project."
        )
    source = _source(placement, project_id)
    if source.engine not in dialects or dialects[source.engine] not in ("postgres", "clickhouse"):
        raise DemoRefused("The count source has no supported local runtime binding.")
    rebuilt = make_count_request(
        project_id=project_id,
        name=copied["name"],
        revision=copied["revision"],
        query=copied["query"],
        placement=placement,
        dialect=dialects[source.engine],
    )
    if copied != rebuilt:
        raise DemoRefused("The count handoff has inconsistent metadata.")
    return CountRequest(
        copied["name"],
        copied["revision"],
        copied["query"]["sql"],
        source.engine,
        dialects[source.engine],
        copied["digest"],
        str(placement.fingerprint),
    )


def _count(adapter: Any, query: str) -> int:
    if adapter.dialect == "postgres":
        with adapter._cx.transaction(), adapter._cx.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("SET LOCAL statement_timeout = '10000ms'")
            cursor.execute("SET LOCAL lock_timeout = '2000ms'")
            sde.explain(adapter, query)
            cursor.execute(query)
            rows = cursor.fetchall()
    else:
        settings = {"readonly": 1, "max_execution_time": 10, "timeout_overflow_mode": "throw"}
        # The SQL grammar cannot override these settings or call external table functions.
        adapter._cx.query("EXPLAIN PLAN " + query, settings=settings)
        rows = adapter._cx.query(query, settings=settings).result_rows
    if len(rows) != 1 or len(rows[0]) != 1 or type(rows[0][0]) is not int or rows[0][0] < 0:
        raise DemoRefused("The count result is not one exact nonnegative integer.")
    return rows[0][0]


def run_count_query(root: Path, document: Mapping[str, Any]) -> dict[str, Any]:
    from .verification import load_completed_runs, verify_runs

    settings, logical = config(root), model()
    keys = public_keys(settings["public_keys"])

    def current() -> sde.PlacementMap:
        return sde.load_local_map(
            root / "state", model=logical, project_id=settings["project_id"], public_key=keys
        )

    placement = current()
    request = load_count_request(
        document,
        project_id=settings["project_id"],
        placement=placement,
        dialects={name: item["dialect"] for name, item in settings["engines"].items()},
    )
    ids = sorted(path.name for path in (root / "runs").iterdir())
    runs = load_completed_runs(root, ids)
    if not runs:
        raise DemoRefused("Finish a demo workload before running its count oracle.")
    verify_runs(root, ids)
    if current().fingerprint != placement.fingerprint:
        raise DemoRefused("The local map changed during data verification; retry after cutover.")
    dsns = credentials(root, "runtime", settings["engines"])
    adapter = engine(request.dialect, dsns[request.engine])
    try:
        adapter.connect()
        with sde.Session(
            logical, placement, {request.engine: adapter}, project_id=settings["project_id"]
        ):
            total = _count(adapter, request.sql)
        if total != sum(run.expected_rows for run in runs):
            raise DemoRefused("The local SQL result differs from the completed synthetic runs.")
    finally:
        adapter.close()
    if (
        current().fingerprint != placement.fingerprint
        or ids != sorted(path.name for path in (root / "runs").iterdir())
        or load_completed_runs(root, ids) != runs
    ):
        raise DemoRefused(
            "The map or run catalog changed during the query; retry after it settles."
        )
    identity = uuid4().hex
    receipt = {
        "protocol": 1,
        "kind": "sde-weather-count-observation",
        "execution_id": identity,
        "project_id": settings["project_id"],
        "map_fingerprint": request.map_fingerprint,
        "query_name": request.name,
        "query_revision": request.revision,
        "query_digest": request.digest,
        "verified": True,
        "run_ids": ids,
        "observed_at": datetime.now(UTC).isoformat(),
    }
    # Business values exist only in this private local result file, never in the exported receipt.
    write(root / "query-results" / identity / "result.json", {"count": total, "receipt": receipt})
    write(root / "query-results" / identity / "receipt.json", receipt)
    return receipt

"""The pause after a settle, both sides read beside each other, on PostgreSQL and ClickHouse.

``sde.reconcile.reconcile_since`` reads the rows that carry the generation on both sides, each by
pages in its engine's order, and looks a row up by its key only where the other side did not show
it beside the same chunk. This file holds it where the engines' orders differ from each other and
from this process's - a UUID key, which ClickHouse orders otherwise than PostgreSQL and Python, and
a key of a UUID and an instant - and where they agree, a key of one integer. Both directions each.

The tables are the test's own, with the write generation as the library keeps it and without the
constraints of a fence: the rows are written here under the generation each case needs, as a
fan-out, a lost fan-out and a settle would have left them.
"""

from __future__ import annotations

import datetime as dt
import os
import random
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from typing import Any

import pytest

import sde
from sde.engines.clickhouse import ClickHouseEngine
from sde.engines.postgres import PostgresEngine
from sde.generation import EPOCH_COLUMN
from sde.inspection import InspectionContext
from sde.reconcile import compare_stamped, reconcile_since
from sde.testing.loader import model_from_neutral

POSTGRES = os.environ.get("SDE_POSTGRES_DSN")
CLICKHOUSE = os.environ.get("SDE_CLICKHOUSE_DSN")

pytestmark = pytest.mark.skipif(
    not (POSTGRES and CLICKHOUSE),
    reason="set SDE_POSTGRES_DSN and SDE_CLICKHOUSE_DSN to run the pause on both engines",
)

PROJECT = "1" * 32
OLD, G, M = 1, 2, 3
"""A generation from before the advance, the map's since it, and the cutover's own."""

KEYS: dict[str, list[tuple[str, str, str, str]]] = {
    # (field, neutral type, PostgreSQL type, ClickHouse type), the key's columns first.
    "integer": [("id", "int64", "bigint", "Int64")],
    "uuid": [("id", "uuid", "uuid", "UUID")],
    "uuid and instant": [
        ("id", "uuid", "uuid", "UUID"),
        ("at", "timestamptz", "timestamptz", "DateTime64(6, 'UTC')"),
    ],
}
VALUE = ("value", "int32", "integer", "Int32")
DIRECTIONS = [("postgres", "clickhouse"), ("clickhouse", "postgres")]


def key_of(kind: str, number: int, draw: random.Random) -> dict[str, Any]:
    if kind == "integer":
        return {"id": number}
    key: dict[str, Any] = {"id": uuid.UUID(int=draw.getrandbits(128), version=4)}
    if kind == "uuid and instant":
        key["at"] = dt.datetime(2026, 11, 19, 12, 0, 0, number % 1_000_000, tzinfo=dt.UTC)
    return key


@contextmanager
def engines() -> Iterator[dict[str, Any]]:
    assert POSTGRES and CLICKHOUSE
    with PostgresEngine(POSTGRES) as pg, ClickHouseEngine(CLICKHOUSE) as ch:
        yield {"postgres": pg, "clickhouse": ch}


@contextmanager
def pair(kind: str, source: str, target: str) -> Iterator[tuple[InspectionContext, Any, Any]]:
    """A group whose source is in one engine and whose copy is in the other, with tables of its
    own and nothing in them."""
    columns = [*KEYS[kind], VALUE]
    names = {"source": f"pause_{uuid.uuid4().hex[:12]}", "copy": f"pause_{uuid.uuid4().hex[:12]}"}
    sde.clear_registry()
    model = model_from_neutral(
        {
            "entities": [
                {
                    "name": "Event",
                    "fields": [
                        {"name": field, "type": neutral} for field, neutral, _, _ in columns
                    ],
                    "key": [field for field, _, _, _ in KEYS[kind]],
                }
            ]
        }
    )

    def material(engine: str, role: str) -> dict[str, Any]:
        types = {field: (pg if engine == "postgres" else ch) for field, _, pg, ch in columns}
        return {
            "id": engine,
            "engine": engine,
            "layout": {"tables": {"Event": names[role]}, "columns": {"Event": types}},
        }

    placement = sde.load_map(
        {
            "contract": 4,
            "project_id": PROJECT,
            "model_version": model.version,
            "map_version": 2,
            "groups": {
                "Event": {
                    "write_epoch": G,
                    "source": material(source, "source"),
                    "derived": [{**material(target, "copy"), "lag_budget_ms": 1000}],
                    "also_write": [target],
                }
            },
        },
        model=model,
    )
    key = ", ".join(field for field, _, _, _ in KEYS[kind])
    with engines() as found, ExitStack() as cleanup:
        for engine, role in ((source, "source"), (target, "copy")):
            name = names[role]
            if engine == "postgres":
                pg = found["postgres"]
                body = ", ".join(f'"{field}" {pg_type}' for field, _, pg_type, _ in columns)
                with pg._cx.cursor() as cur:
                    cur.execute(
                        f'CREATE TABLE "{name}" ({body}, "{EPOCH_COLUMN}" bigint NOT NULL '
                        f"DEFAULT 0, PRIMARY KEY ({key}))"
                    )
                cleanup.callback(drop_postgres, pg, name)
            else:
                ch = found["clickhouse"]
                body = ", ".join(f"`{field}` {ch_type}" for field, _, _, ch_type in columns)
                ch._cx.command(
                    f"CREATE TABLE `{name}` ({body}, `{EPOCH_COLUMN}` Int64 DEFAULT 0) "
                    f"ENGINE = ReplacingMergeTree ORDER BY ({key})"
                )
                cleanup.callback(ch._cx.command, f"DROP TABLE IF EXISTS `{name}`")
        view = InspectionContext(model, placement, found, PROJECT)
        yield view, found[source], found[target]


def drop_postgres(engine: Any, name: str) -> None:
    with engine._cx.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{name}"')


def table_of(view: InspectionContext, role: str) -> str:
    group = view.placement.placement_of("Event")
    material = group.source if role == "source" else group.also_write[0]
    return str(material.layout.tables["Event"])


def contents(engine: Any, table: str, key: list[str]) -> dict[tuple[Any, ...], int]:
    return {tuple(row[c] for c in key): row["value"] for row in engine.key_range(table, key)}


def counted(log: list[str]) -> Callable[..., Any]:
    def call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        log.append(getattr(function, "__name__", ""))
        return function(*args, **kwargs)

    return call


@pytest.mark.parametrize(("source", "target"), DIRECTIONS, ids=["pg-to-ch", "ch-to-pg"])
@pytest.mark.parametrize("kind", list(KEYS))
def test_every_row_since_is_compared_and_written_once_whatever_order_each_engine_keeps(
    kind: str, source: str, target: str
) -> None:
    """Six hundred rows written since the advance on both sides, three hundred from before it,
    and what a lost fan-out, a write the copy holds otherwise and a settle leave behind: the copy
    ends the source's equal, each difference written once, and the comparison under the barriers
    finds nothing."""
    draw = random.Random(f"{kind}/{source}")
    key = [field for field, _, _, _ in KEYS[kind]]
    keys = [key_of(kind, number, draw) for number in range(1, 1001)]
    with pair(kind, source, target) as (view, left, right):
        names = table_of(view, "source"), table_of(view, "copy")
        before = [{**k, "value": n, EPOCH_COLUMN: OLD} for n, k in enumerate(keys[:300])]
        since = [{**k, "value": n, EPOCH_COLUMN: G} for n, k in enumerate(keys[300:900], 300)]
        settled = [{**k, "value": n, EPOCH_COLUMN: OLD} for n, k in enumerate(keys[900:], 900)]
        left.copy_in(names[0], before + since + settled)
        lost = since[::120]  # five rows whose fan-out did not arrive
        otherwise = since[7::120]  # five the copy holds with another value
        on_copy = [row for row in since if row not in lost and row not in otherwise]
        on_copy += [{**row, "value": -1} for row in otherwise]
        # A settle wrote the copy's rows of these under the map's generation; three of them a
        # write of the application's crossed, and the copy holds another value.
        on_copy += [{**row, EPOCH_COLUMN: G} for row in settled[3:]]
        on_copy += [{**row, "value": -2, EPOCH_COLUMN: G} for row in settled[:3]]
        right.copy_in(names[1], before + on_copy)
        log: list[str] = []
        done = reconcile_since(view, "Event", since=G, epoch=M, chunk_rows=50, call=counted(log))
        assert done.settled and not done.surplus
        assert done.written == len(lost) + len(otherwise) + 3
        report = compare_stamped(view, "Event", epoch=M, reconciled=done, chunk_rows=50)
        assert report.matched and report.rows_source == report.rows_target == 1000
        assert contents(right, names[1], key) == contents(left, names[0], key)


@pytest.mark.parametrize(("source", "target"), DIRECTIONS, ids=["pg-to-ch", "ch-to-pg"])
def test_a_pause_whose_sides_agree_looks_up_no_key(source: str, target: str) -> None:
    """Where both engines order the key as this process does and the fan-out arrived for every
    row, the pause reads the two sides' pages and nothing else."""
    rows = [{"id": n, "value": n, EPOCH_COLUMN: G if n > 300 else OLD} for n in range(1, 901)]
    with pair("integer", source, target) as (view, left, right):
        left.copy_in(table_of(view, "source"), rows)
        right.copy_in(table_of(view, "copy"), rows)
        log: list[str] = []
        done = reconcile_since(view, "Event", since=G, epoch=M, chunk_rows=50, call=counted(log))
        assert done.settled and done.written == 0
        # Twelve pages of each side and one that answers none.
        assert log == ["stamped_range"] * 26 and (done.chunks, done.rows) == (24, 1200)

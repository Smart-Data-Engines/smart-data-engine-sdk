"""A copy's table is one the library created and marked (format contract §7r), on live engines.

Found by an independent review of ``sde-operator`` on 20 November 2026: ``prepare_schema`` created
a copy's tables with ``CREATE TABLE IF NOT EXISTS``, so a map naming a copy like a table the client
already had, of the same shape, made that table the copy - and the operator then wrote over it,
emptied it and refilled it. The controls: a fresh copy is created and marked and taken again on a
second run, and a source the client already has is still taken as the source.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from test_runtime_privileges_live import runtime_roles

import sde
from sde.errors import MigrationRefused
from sde.ownership import copy_marker

PROJECT = "2" * 32
SHAPE = {
    "postgres": {"id": "bigint", "value": "integer"},
    "clickhouse": {"id": "Int64", "value": "Int32"},
}


@contextmanager
def provisioning(source: str) -> Iterator[tuple[Any, ...]]:
    """A contract-4 map of one group, its source in ``source`` and its copy in the other engine."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    copy = "clickhouse" if source == "postgres" else "postgres"
    with runtime_roles("postgres") as pg, runtime_roles("clickhouse") as ch:
        roles = {"postgres": pg, "clickhouse": ch}
        operators = {name: value.operator for name, value in roles.items()}
        sde.clear_registry()

        @sde.entity
        class Event:
            id: int
            value: sde.Int32

        model = sde.build_model(Event)
        key = Ed25519PrivateKey.generate()

        def material(name: str, identity: str) -> dict[str, Any]:
            return {
                "engine": name,
                "id": identity,
                "layout": {
                    "tables": {"Event": identity + "_events"},
                    "columns": {"Event": SHAPE[name]},
                },
            }

        body: dict[str, Any] = {
            "contract": 4,
            "project_id": PROJECT,
            "model_version": model.version,
            "map_version": 1,
            "groups": {
                "Event": {
                    "write_epoch": 1,
                    "source": material(source, "source"),
                    "derived": [{**material(copy, "copy"), "lag_budget_ms": 60000}],
                    "also_write": ["copy"],
                }
            },
        }
        body["signature"] = {
            "alg": "ed25519",
            "value": base64.b64encode(key.sign(sde.canonical_bytes(body))).decode(),
        }
        parsed = sde.load_map(body, model=model, public_key=key.public_key().public_bytes_raw())
        yield model, parsed, operators, copy


def _create(operator: Any, table: str, dialect: str) -> None:
    """A table of the client's, of the copy's name and shape, holding one row."""
    if dialect == "postgres":
        operator._cx.execute(f'CREATE TABLE "{table}" (id bigint PRIMARY KEY, value integer)')
        operator._cx.execute(f'INSERT INTO "{table}" VALUES (7, 70)')
        operator._cx.commit()
    else:
        operator._cx.command(
            f"CREATE TABLE `{table}` (id Int64, value Int32) "
            "ENGINE = ReplacingMergeTree ORDER BY (id)"
        )
        operator._cx.command(f"INSERT INTO `{table}` VALUES (7, 70)")


def _rows(operator: Any, table: str, dialect: str) -> list[tuple[Any, ...]]:
    if dialect == "postgres":
        with operator._cx.cursor() as cursor:
            cursor.execute(f'SELECT id, value FROM "{table}" ORDER BY id')
            return [tuple(row) for row in cursor.fetchall()]
    rows = operator._cx.query(f"SELECT id, value FROM `{table}` FINAL ORDER BY id").result_rows
    return [tuple(row) for row in rows]


def _columns(operator: Any, table: str, dialect: str) -> list[str]:
    if dialect == "postgres":
        with operator._cx.cursor() as cursor:
            cursor.execute(
                "SELECT attname FROM pg_attribute WHERE attrelid = to_regclass(%s) "
                "AND attnum > 0 AND NOT attisdropped ORDER BY attname",
                [f'"{table}"'],
            )
            return [str(row[0]) for row in cursor.fetchall()]
    rows = operator._cx.query(
        "SELECT name FROM system.columns WHERE database = currentDatabase() "
        "AND table = {t:String} ORDER BY name",
        parameters={"t": table},
    ).result_rows
    return [str(row[0]) for row in rows]


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_a_copy_s_table_is_created_marked_and_taken_again(source: str) -> None:
    with provisioning(source) as (model, parsed, operators, copy):
        sde.prepare_schema(model, parsed, operators, project_id=PROJECT)
        found = operators[copy]._mark("copy_events")
        assert found is not None
        assert found[1] == copy_marker(PROJECT, "Event", "copy_events")
        # A second run finds its own mark and takes the table again, as provisioning always did.
        sde.prepare_schema(model, parsed, operators, project_id=PROJECT)


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_a_table_of_a_copy_s_name_the_client_has_is_refused_and_left_as_it_was(
    source: str,
) -> None:
    with provisioning(source) as (model, parsed, operators, copy):
        _create(operators[copy], "copy_events", copy)
        before = _columns(operators[copy], "copy_events", copy)
        with pytest.raises(MigrationRefused, match="not one this library created for it"):
            sde.prepare_schema(model, parsed, operators, project_id=PROJECT)
        # Nothing was written to it: its row, and no generation column or fence of ours.
        assert _rows(operators[copy], "copy_events", copy) == [(7, 70)]
        assert _columns(operators[copy], "copy_events", copy) == before
        assert "__sde_write_epoch" not in before


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_a_source_the_client_has_is_still_taken_as_the_source(source: str) -> None:
    """The control: only a copy's table needs the mark. A source is often the client's own table
    from before SDE, and provisioning takes it as it always did."""
    with provisioning(source) as (model, parsed, operators, _copy):
        _create(operators[source], "source_events", source)
        sde.prepare_schema(model, parsed, operators, project_id=PROJECT)
        assert _rows(operators[source], "source_events", source) == [(7, 70)]

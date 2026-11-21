"""A session reads the marks of its map's tables when it opens (format contract §7r), live.

The two independent reviews of copy ownership (20 November 2026) wrote over another group's rows
through a session that read no mark: a marked copy one map named as a group's source, and two
engine names bound to one database, where `prepare_schema` marked a copy the same map took as
another group's source. A session of a map with write generations now refuses, before it writes:
a copy's table without its own mark, a source's table with a copy's mark, and a copy's table that
another name of the map reaches. `prepare_schema` refuses the same, before its first statement and
after its last. The control: a map provisioned as `prepare_schema` provisions it opens.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from test_runtime_privileges_live import runtime_roles

import sde
from sde.errors import MigrationRefused

PROJECT = "3" * 32
SHAPE = {
    "postgres": {"id": "bigint", "value": "integer"},
    "clickhouse": {"id": "Int64", "value": "Int32"},
}


@contextmanager
def two_groups(dialect: str) -> Iterator[tuple[Any, Any]]:
    """Two groups of one shape, Account and Event, and an operator login on ``dialect``."""
    with runtime_roles(dialect) as roles:
        sde.clear_registry()

        @sde.entity
        class Account:
            id: int
            value: sde.Int32

        @sde.entity
        class Event:
            id: int
            value: sde.Int32

        yield sde.build_model(Account, Event), roles.operator


def _map(model: Any, dialect: str, groups: dict[str, Any], version: int = 1) -> Any:
    """A contract-4 map: per group, a source and optional copy as (engine, table) pairs."""

    def material(engine: str, ident: str, entity: str, table: str) -> dict[str, Any]:
        layout = {"tables": {entity: table}, "columns": {entity: SHAPE[dialect]}}
        return {"engine": engine, "id": ident, "layout": layout}

    body: dict[str, Any] = {
        "contract": 4,
        "project_id": PROJECT,
        "model_version": model.version,
        "map_version": version,
        "groups": {},
    }
    for group, (source, copies) in groups.items():
        placed: dict[str, Any] = {
            "write_epoch": 1,
            "source": material(source[0], group + "@source", group, source[1]),
        }
        if copies is not None:
            copies = copies if isinstance(copies, list) else [copies]
            placed["derived"] = [
                {**material(engine, f"{group}@copy{n}", group, table), "lag_budget_ms": 60000}
                for n, (engine, table) in enumerate(copies)
            ]
            placed["also_write"] = [f"{group}@copy{n}" for n in range(len(copies))]
        body["groups"][group] = placed
    return sde.load_map(body, model=model)


def _unmark(operator: Any, dialect: str, table: str) -> None:
    if dialect == "postgres":
        operator._cx.execute(f'COMMENT ON TABLE "{table}" IS NULL')
    else:
        operator._cx.command(f"ALTER TABLE `{table}` MODIFY COMMENT ''")


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_a_map_provisioned_as_prepare_schema_provisions_it_opens(dialect: str) -> None:
    """The control of every refusal below."""
    with two_groups(dialect) as (model, operator):
        engines = {"e": operator}
        placement = _map(
            model, dialect,
            {"Account": (("e", "acct"), ("e", "acct_copy")), "Event": (("e", "ev"), None)},
        )
        sde.prepare_schema(model, placement, engines, project_id=PROJECT)
        sde.Session(model, placement, engines, project_id=PROJECT)


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_a_session_refuses_a_copy_without_its_mark(dialect: str) -> None:
    with two_groups(dialect) as (model, operator):
        engines = {"e": operator}
        placement = _map(
            model, dialect,
            {"Account": (("e", "acct"), ("e", "acct_copy")), "Event": (("e", "ev"), None)},
        )
        sde.prepare_schema(model, placement, engines, project_id=PROJECT)
        _unmark(operator, dialect, "acct_copy")
        with pytest.raises(MigrationRefused, match="is not one this library created for it"):
            sde.Session(model, placement, engines, project_id=PROJECT)


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_a_marked_copy_named_as_another_group_s_source_is_refused_before_a_write(
    dialect: str,
) -> None:
    """The second review's n2: the next map names Account's marked copy as Event's source."""
    with two_groups(dialect) as (model, operator):
        engines = {"e": operator}
        first = _map(
            model, dialect,
            {"Account": (("e", "acct"), ("e", "shared")), "Event": (("e", "ev"), None)},
        )
        sde.prepare_schema(model, first, engines, project_id=PROJECT)
        second = _map(
            model, dialect,
            {"Account": (("e", "acct"), None), "Event": (("e", "shared"), None)},
            version=2,
        )
        with pytest.raises(MigrationRefused, match="carries a copy's mark"):
            sde.prepare_schema(model, second, engines, project_id=PROJECT)
        with pytest.raises(MigrationRefused, match="carries a copy's mark"):
            sde.Session(model, second, engines, project_id=PROJECT)


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_two_engine_names_bound_to_one_database_are_refused(dialect: str) -> None:
    """The second review's n1: Account's copy and Event's source are one table by two engine
    names. `prepare_schema` creates and marks the copy, takes the source, and refuses after its
    last statement; a session refuses before its first write."""
    with two_groups(dialect) as (model, operator):
        engines = {"a": operator, "b": operator}
        placement = _map(
            model, dialect,
            {"Account": (("a", "acct"), ("b", "shared")), "Event": (("a", "shared"), None)},
        )
        with pytest.raises(MigrationRefused, match="carries a copy's mark"):
            sde.prepare_schema(model, placement, engines, project_id=PROJECT)
        with pytest.raises(MigrationRefused, match="carries a copy's mark"):
            sde.Session(model, placement, engines, project_id=PROJECT)


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_a_copy_another_engine_name_reaches_is_refused(dialect: str) -> None:
    """Two copies of one group naming one table through two engine names bound to one database.
    The second finds the first's mark, which is right for it - same group, same name - so only the
    identity of the table each name reaches tells them apart: `prepare_schema` refuses after its
    last statement, a session before its first write."""
    with two_groups(dialect) as (model, operator):
        engines = {"a": operator, "b": operator}
        placement = _map(
            model, dialect,
            {"Account": (("a", "acct"), None),
             "Event": (("a", "ev"), [("a", "ev_copy"), ("b", "ev_copy")])},
        )
        with pytest.raises(MigrationRefused, match="two names of the map reach this one"):
            sde.prepare_schema(model, placement, engines, project_id=PROJECT)
        with pytest.raises(MigrationRefused, match="two names of the map reach this one"):
            sde.Session(model, placement, engines, project_id=PROJECT)


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
@pytest.mark.parametrize("names", ["two engine names", "one engine name"])
def test_two_groups_sources_reaching_one_table_are_refused(dialect: str, names: str) -> None:
    """Account's and Event's sources are one table: a session would write both groups' rows into
    it, and an erasure of one group's subject would delete the other's rows. ``load_map`` cannot
    tell two engine names bound to one database apart, and names no rule for two sources of one
    name; `prepare_schema` creates the table once for both and refuses after its last statement,
    a session before its first write."""
    with two_groups(dialect) as (model, operator):
        engines = {"a": operator, "b": operator}
        other = "b" if names == "two engine names" else "a"
        placement = _map(
            model, dialect,
            {"Account": (("a", "shared"), None), "Event": ((other, "shared"), None)},
        )
        refused = "a source's table holds its group alone, and two names of the map reach this one"
        with pytest.raises(MigrationRefused, match=refused):
            sde.prepare_schema(model, placement, engines, project_id=PROJECT)
        with pytest.raises(MigrationRefused, match=refused):
            sde.Session(model, placement, engines, project_id=PROJECT)

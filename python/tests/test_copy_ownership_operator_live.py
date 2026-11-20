"""Every operator path that writes to a copy reads its creator's mark first (format contract §7r).

A copy without its creator's mark - here, the fixture's copy with its mark taken off, as one
provisioned before copies were marked - is refused by a backfill, a settle, a cutover and an erasure
before any of them writes to it, and so is a copy marked for another group.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_local_cutover_live import PROJECT, copy_rows, fixture

import sde
from sde.erasure import ErasureRequest
from sde.errors import MigrationRefused
from sde.local_cutover import refuse_another_name
from sde.ownership import copy_marker

REFUSED = "not one this library created for it"


def _unmark(roles: Any, target: str) -> None:
    """The copy's table as one provisioned before copies were marked: without its comment."""
    operator = roles[target].operator
    if target == "postgres":
        operator._cx.execute('COMMENT ON TABLE "copy_events" IS NULL')
        operator._cx.commit()
    else:
        operator._cx.command("ALTER TABLE `copy_events` MODIFY COMMENT ''")


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_each_path_that_writes_to_a_copy_refuses_one_without_its_mark(
    source: str, tmp_path: Path
) -> None:
    with fixture(source, tmp_path) as (executor, plan, _old, roles, _source, target, model, _key):
        _unmark(roles, target)
        before = copy_rows(roles, target)
        with pytest.raises(MigrationRefused, match=REFUSED):
            executor.backfill()
        with pytest.raises(MigrationRefused, match=REFUSED):
            executor.settle()
        with pytest.raises(MigrationRefused, match=REFUSED):
            executor.execute(plan)
        request = ErasureRequest.build(model, "Event", [{"id": 1}])
        with pytest.raises(MigrationRefused, match=REFUSED):
            executor.erase(request, execute=True)
        # Nothing reached the copy, and the project holds no unfinished operation.
        assert copy_rows(roles, target) == before
        assert executor.store.read()["execution"] is None


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_a_copy_marked_for_another_group_is_refused(
    source: str, tmp_path: Path
) -> None:
    with fixture(source, tmp_path) as (executor, _plan, _old, roles, _source, target, _model, _key):
        other = copy_marker(PROJECT, "Payment", "copy_events")
        operator = roles[target].operator
        if target == "postgres":
            operator._cx.execute(f"COMMENT ON TABLE \"copy_events\" IS '{other}'")
            operator._cx.commit()
        else:
            operator._cx.command(f"ALTER TABLE `copy_events` MODIFY COMMENT '{other}'")
        with pytest.raises(MigrationRefused, match=REFUSED):
            executor.backfill()


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_a_copy_another_name_of_the_map_reaches_is_refused(source: str, tmp_path: Path) -> None:
    """Two engine names bound to one database reach one table by two names, which ``load_map``
    cannot see: the operator looks every other name of the map up through its binding (§7r)."""
    with fixture(source, tmp_path) as (executor, _plan, _old, _roles, _source, target, model, _key):
        native = executor.native[target]
        identity = native.identity("copy_events")
        assert native.object_keys("copy_events") == {identity.physical_key}
        assert native.object_keys("no_table_of_this_name") == frozenset()
        columns = (
            {"id": "bigint", "value": "integer"}
            if target == "postgres"
            else {"id": "Int64", "value": "Int32"}
        )

        def placed(source_table: str) -> Any:
            def material(engine: str, ident: str, table: str) -> dict[str, Any]:
                layout = {"tables": {"Event": table}, "columns": {"Event": columns}}
                return {"engine": engine, "id": ident, "layout": layout}

            return sde.load_map(
                {
                    "contract": 4,
                    "project_id": PROJECT,
                    "model_version": model.version,
                    "map_version": 1,
                    "groups": {
                        "Event": {
                            "write_epoch": 1,
                            "source": material("primary", "source", source_table),
                            "derived": [
                                {**material("replica", "copy", "copy_events"),
                                 "lag_budget_ms": 60000}
                            ],
                            "also_write": ["copy"],
                        }
                    },
                },
                model=model,
            )

        both = {"primary": native, "replica": native}
        with pytest.raises(MigrationRefused, match="two names of the map reach this one"):
            refuse_another_name(placed("copy_events"), both, "Event", "copy", "copy_events",
                                identity)
        # The control: the other name reaches another table, or none, through the same binding.
        refuse_another_name(placed("other_events"), both, "Event", "copy", "copy_events", identity)
        # And every path that writes to a copy asks it, through the check of the copy's mark.
        executor.native.update(both)
        try:
            state = executor.store.read()
            with pytest.raises(MigrationRefused, match="two names of the map reach this one"):
                executor._copy_owned(placed("copy_events"), "copy", "Event", "copy_events", state)
            executor._copy_owned(placed("other_events"), "copy", "Event", "copy_events", state)
        finally:
            for name in both:
                executor.native.pop(name)

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
from sde import local_cutover
from sde.engines._ownership import copy_owned
from sde.erasure import ErasureRequest
from sde.errors import MigrationRefused
from sde.local_cutover import map_reach, refuse_another_name
from sde.ownership import copy_marker, source_marker

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
        aliased, other = placed("copy_events"), placed("other_events")
        with pytest.raises(MigrationRefused, match="two names of the map reach this one"):
            refuse_another_name(map_reach(aliased, both), "Event", "copy", "copy_events", identity)
        # The control: the other name reaches another table, or none, through the same binding.
        refuse_another_name(map_reach(other, both), "Event", "copy", "copy_events", identity)
        # And the check of the copy's mark asks it.
        executor.native.update(both)
        try:
            state = executor.store.read()
            with pytest.raises(MigrationRefused, match="two names of the map reach this one"):
                executor._copy_owned(
                    aliased, "copy", "Event", "copy_events", state,
                    map_reach(aliased, executor.native),
                )
            executor._copy_owned(
                other, "copy", "Event", "copy_events", state, map_reach(other, executor.native)
            )
        finally:
            for name in both:
                executor.native.pop(name)


class _Aliasing:
    """A binding whose source table's name also reaches ``key``: another name for the copy."""

    def __init__(self, real: Any, key: tuple[str, str, str, str]) -> None:
        self._real, self._key = real, key

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def object_keys_many(self, tables: Any) -> dict[str, frozenset[Any]]:
        found = self._real.object_keys_many(tables)
        return {
            table: keys | {self._key} if table == "source_events" else keys
            for table, keys in found.items()
        }


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_each_path_that_writes_to_a_copy_refuses_one_another_name_reaches(
    source: str, tmp_path: Path
) -> None:
    """Each path looks every name of the map up once and asks it of each copy it writes to."""
    with fixture(source, tmp_path) as (executor, plan, _old, roles, _src, target, model, _key):
        key = executor.native[target].identity("copy_events").physical_key
        real = executor.native[source]
        executor.native[source] = _Aliasing(real, key)
        try:
            before = copy_rows(roles, target)
            request = ErasureRequest.build(model, "Event", [{"id": 1}])
            for run in (
                executor.backfill,
                executor.settle,
                lambda: executor.execute(plan),
                lambda: executor.erase(request, execute=True),
            ):
                with pytest.raises(MigrationRefused, match="two names of the map reach this one"):
                    run()
            assert copy_rows(roles, target) == before
            assert executor.store.read()["execution"] is None
        finally:
            executor.native[source] = real


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_a_cutover_marks_the_copy_it_makes_the_source_and_it_is_never_a_copy_again(
    source: str, tmp_path: Path
) -> None:
    """A copy's mark left on the source would let a later map name it as a copy, and the operator
    empty the application's rows from it: the cutover writes a source's mark in its place."""
    with fixture(source, tmp_path) as (executor, plan, _old, _roles, _src, target, _model, _key):
        assert executor.execute(plan).as_record()["outcome"] == "success"
        native = executor.native[target]
        found = native_staging_marker(native, "copy_events")
        assert found == source_marker(PROJECT, "Event", "copy_events")
        with pytest.raises(MigrationRefused, match="carries a source's mark"):
            copy_owned(native, project_id=PROJECT, group="Event", table="copy_events", stages={})


def native_staging_marker(native: Any, table: str) -> str | None:
    from sde.engines._staging import native_staging

    found = native_staging(native).marker(table)
    return None if found is None else found[1]


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
@pytest.mark.parametrize("other", ["another group", "another entity of the group"])
def test_an_erasure_refuses_a_source_another_name_reaches(
    source: str, other: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One group's source named as another group's table - two engine names bound to one database
    - or as another entity's of its own group would have an erasure delete the other's rows: the
    erasure refuses a source's table another name of the map reaches, before anything is read or
    deleted (§7r). The other name is added to what the map's names reach, as a binding would show
    it; the control is the same erasure without it."""
    with fixture(source, tmp_path) as (executor, _plan, _old, roles, _src, target, model, _key):
        real = local_cutover.map_reach

        def shared(current: Any, natives: Any) -> Any:
            reach = real(current, natives)
            spot = current.groups["Event"]
            table = spot.source.layout.tables["Event"]
            for names in reach.values():
                if any((n[0], n[1], n[3]) == ("Event", spot.source.id, table) for n in names):
                    names.append(
                        ("Payment", "Payment@source", "Payment", "payments", "other")
                        if other == "another group"
                        else ("Event", spot.source.id, "Payment", table, spot.source.engine)
                    )
            return reach

        request = ErasureRequest.build(model, "Event", [{"id": 1}])
        before = copy_rows(roles, target)
        monkeypatch.setattr(local_cutover, "map_reach", shared)
        refused = "a source's table holds one entity of its group alone"
        with pytest.raises(MigrationRefused, match=refused):
            executor.erase(request, execute=True)
        assert copy_rows(roles, target) == before
        assert executor.store.read()["execution"] is None
        monkeypatch.setattr(local_cutover, "map_reach", real)
        executor.erase(request, execute=False)

"""Every operator path that writes to a copy reads its creator's mark first (format contract §7r).

A copy provisioned before copies were marked - here, the fixture's copy with its mark taken off -
is refused by a backfill, a settle, a cutover and an erasure before any of them writes to it, and
``adopt-copy`` marks it once the client confirms the listing. The control: the same copy, marked,
is backfilled.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_local_cutover_live import PROJECT, copy_rows, fixture

from sde.erasure import ErasureRequest
from sde.errors import MigrationRefused
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
def test_adopt_copy_marks_exactly_the_tables_it_listed(source: str, tmp_path: Path) -> None:
    with fixture(source, tmp_path) as (executor, _plan, _old, roles, _source, target, _model, _key):
        _unmark(roles, target)
        listed = executor.adopt_copy("Event")
        assert listed["adopted"] is False
        assert [row["table"] for row in listed["tables"]] == ["copy_events"]
        assert [row["marked"] for row in listed["tables"]] == [False]
        # A digest of another listing marks nothing.
        with pytest.raises(MigrationRefused, match="not the ones listed"):
            executor.adopt_copy("Event", confirm="0" * 64)
        assert roles[target].operator._mark("copy_events")[1] in ("", None)
        adopted = executor.adopt_copy("Event", confirm=listed["confirm"])
        assert adopted["adopted"] is True
        assert roles[target].operator._mark("copy_events")[1] == copy_marker(
            PROJECT, "Event", "copy_events"
        )
        # The control: the adopted copy is backfilled like one provisioned marked.
        progress = executor.backfill()
        assert progress and all(p.as_record() for p in progress)


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_a_copy_marked_for_another_group_is_refused_and_not_adopted(
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
        with pytest.raises(MigrationRefused, match="carries another mark"):
            executor.adopt_copy("Event")

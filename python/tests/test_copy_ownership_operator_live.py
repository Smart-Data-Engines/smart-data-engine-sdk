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

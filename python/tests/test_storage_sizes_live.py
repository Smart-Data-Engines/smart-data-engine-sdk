"""What a runtime login can read of its own tables' size, on real engines.

The planner's migration risk, the volume drift and every move's copy estimate were built on the
size of a group, and the library never measured it. These tests pin the catalogue read the library
now makes, with the least-privilege logins the SDK's own runtime grants create: PostgreSQL needs
nothing more; ClickHouse needs one column grant on ``system.parts`` and then sees only the tables it
may use. Numbers only - no row value is read.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_runtime_privileges_live import runtime_roles

from sde.engines.clickhouse import STORAGE_COLUMNS
from sde.errors import EngineError


def _tables(roles: Any) -> None:
    if roles.operator.dialect == "postgres":
        roles.command('CREATE TABLE "filled" (k bigint PRIMARY KEY, v bigint)')
        roles.command('CREATE INDEX "filled_v" ON "filled" (v)')
        roles.command('INSERT INTO "filled" SELECT i, i % 97 FROM generate_series(1, 5000) i')
        roles.command('CREATE TABLE "empty" (k bigint PRIMARY KEY)')
    else:
        roles.command(
            "CREATE TABLE `filled` (k Int64, v Int64, INDEX filled_v v TYPE minmax GRANULARITY 1) "
            "ENGINE = MergeTree ORDER BY k"
        )
        roles.command("INSERT INTO `filled` SELECT number, number % 97 FROM numbers(5000)")
        roles.command("CREATE TABLE `empty` (k Int64) ENGINE = MergeTree ORDER BY k")
    roles.grant("filled")
    roles.grant("empty")


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_a_runtime_login_reads_its_tables_sizes_and_nothing_of_a_missing_one(dialect: str) -> None:
    with runtime_roles(dialect) as roles:
        _tables(roles)
        if dialect == "clickhouse":
            # Measured on 24.8: a login with table grants alone is refused system.parts. The
            # refusal is an error here; the session turns it into an unknown size.
            with pytest.raises(EngineError, match="storage sizes could not be read"):
                roles.runtime.storage_sizes(["filled"])
            roles.command(
                f"GRANT SELECT({', '.join(STORAGE_COLUMNS)}) ON system.parts TO `{roles.username}`"
            )
        runtime = roles.runtime.storage_sizes(["filled", "empty", "missing"])
        admin = roles.operator.storage_sizes(["filled", "empty", "missing"])
        assert runtime == admin, "the runtime login reads what the administrator reads"
        assert set(runtime) == {"filled", "empty"}, "a missing table is absent, not zero"
        total, secondary = runtime["filled"]
        assert total > secondary > 0, "the secondary index is part of the total, and not all of it"
        empty_total, empty_secondary = runtime["empty"]
        assert empty_secondary == 0
        if dialect == "clickhouse":
            assert empty_total == 0, "an empty MergeTree has no parts"
        assert roles.runtime.storage_sizes([]) == {}


def test_the_clickhouse_grant_shows_only_the_logins_own_tables() -> None:
    """The column grant on system.parts is not a window onto other databases."""
    with runtime_roles("clickhouse") as roles, runtime_roles("clickhouse") as other:
        _tables(roles)
        _tables(other)
        roles.command(
            f"GRANT SELECT({', '.join(STORAGE_COLUMNS)}) ON system.parts TO `{roles.username}`"
        )
        # The other namespace's tables carry the same names; the login's own database is what
        # `currentDatabase()` names, and system.tables shows it only what it has grants on.
        mine = roles.runtime.storage_sizes(["filled", "empty"])
        rows = roles.runtime._cx.query(
            "SELECT DISTINCT database FROM system.parts WHERE active"
        ).result_rows
        assert {row[0] for row in rows} == {roles.namespace}
        assert set(mine) == {"filled", "empty"}

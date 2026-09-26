"""Qualify and change actual runtime grants before the local executor may repair a copy."""

from __future__ import annotations

from typing import Any

import pytest
from test_runtime_privileges_live import document
from test_runtime_privileges_live import roles as roles

import sde
from sde.engines._operator import NativeOperator
from sde.placement import WATERMARK_TABLE


def prepare(roles: Any) -> NativeOperator:
    model, placement = document(roles)
    sde.prepare_schema(model, placement, {"db": roles.operator}, project_id="1" * 32)
    roles.grant("events")
    roles.grant(WATERMARK_TABLE)
    return NativeOperator(roles.operator, [roles.runtime])


def test_qualified_runtime_access_is_removed_and_restored(roles: Any) -> None:
    native = prepare(roles)
    names = native.qualify(["events"], ["events", WATERMARK_TABLE])
    assert names == (roles.username,)
    identity = native.identity("events")
    assert identity.physical_key == native.identity("events", engine=roles.runtime).physical_key
    roles.runtime.insert("events", {"id": 1, sde.WRITE_EPOCH_COLUMN: 1})
    native.access([identity], enabled=False)
    with pytest.raises(sde.EngineError):
        roles.runtime.get("events", {"id": 1})
    assert roles.operator.get("events", {"id": 1}) is not None
    native.access([identity], enabled=True)
    assert roles.runtime.get("events", {"id": 1}) is not None
    native.truncate([identity])
    assert roles.operator.count("events") == 0


def test_runtime_with_extra_mutation_privileges_is_refused(roles: Any) -> None:
    native = prepare(roles)
    quote = native.quote
    privilege = "UPDATE" if native.dialect == "postgres" else "ALTER ADD COLUMN"
    roles.command(
        f"GRANT {privilege} ON {quote(roles.namespace)}.{quote('events')} "
        f"TO {quote(roles.username)}"
    )
    with pytest.raises(sde.MigrationRefused, match="runtime"):
        native.qualify(["events"], ["events", WATERMARK_TABLE])
    assert roles.operator.write_fence("events", project_id="1" * 32).state().holds == ()


def test_public_or_wildcard_grants_are_not_mistaken_for_isolated_access(roles: Any) -> None:
    native = prepare(roles)
    quote = native.quote
    if native.dialect == "postgres":
        roles.command(f"GRANT SELECT ON {quote(roles.namespace)}.{quote('events')} TO PUBLIC")
    else:
        roles.command(f"GRANT SELECT ON {quote(roles.namespace)}.* TO {quote(roles.username)}")
    with pytest.raises(sde.MigrationRefused):
        native.qualify(["events"], ["events", WATERMARK_TABLE])
    assert roles.operator.write_fence("events", project_id="1" * 32).state().holds == ()


def test_select_denial_cannot_hide_a_leftover_insert_grant(roles: Any) -> None:
    native = prepare(roles)
    native.qualify(["events"], ["events", WATERMARK_TABLE])
    identity = native.identity("events")
    original = native.command

    def incomplete(statement: str) -> None:
        if statement.startswith("REVOKE"):
            statement = statement.replace("SELECT, INSERT", "SELECT")
        original(statement)

    native.command = incomplete
    with pytest.raises(sde.MigrationRefused, match="SELECT/INSERT grants"):
        native.access([identity], enabled=False)


def test_an_undeclared_reader_is_refused_before_any_barrier(roles: Any) -> None:
    native = prepare(roles)
    quote = native.quote
    outsider = roles.username + "_other"
    roles.command(f"CREATE ROLE {quote(outsider)}")
    try:
        roles.command(
            f"GRANT SELECT ON {quote(roles.namespace)}.{quote('events')} TO {quote(outsider)}"
        )
        with pytest.raises(sde.MigrationRefused, match="undeclared"):
            native.qualify(["events"], ["events", WATERMARK_TABLE])
        assert roles.operator.write_fence("events", project_id="1" * 32).state().holds == ()
    finally:
        roles.command(
            f"REVOKE SELECT ON {quote(roles.namespace)}.{quote('events')} FROM {quote(outsider)}"
        )
        roles.command(f"DROP ROLE {quote(outsider)}")


def test_failed_probe_is_not_evidence_of_access_denial(roles: Any) -> None:
    native = prepare(roles)
    native.qualify(["events"], ["events", WATERMARK_TABLE])
    identity = native.identity("events")
    roles.runtime.close()
    with pytest.raises(sde.EngineError, match="probe"):
        native.access([identity], enabled=False)


def test_the_storage_measurement_grant_is_admitted_on_clickhouse() -> None:
    """The one grant in `system` besides settings: the columns a size measurement reads."""
    from test_runtime_privileges_live import runtime_roles

    from sde.engines.clickhouse import STORAGE_COLUMNS

    with runtime_roles("clickhouse") as held:
        native = prepare(held)
        held.command(
            f"GRANT SELECT({', '.join(STORAGE_COLUMNS)}) ON system.parts TO `{held.username}`"
        )
        assert native.qualify(["events"], ["events", WATERMARK_TABLE]) == (held.username,)


@pytest.mark.parametrize(
    ("privilege", "suffix"),
    [
        ("SELECT(name) ON system.parts", ""),  # a column the measurement does not read
        ("SELECT ON system.parts", ""),  # the whole table
        ("SELECT(database, table) ON system.tables", ""),  # another system table
        ("SELECT(bytes_on_disk) ON system.parts", " WITH GRANT OPTION"),
    ],
)
def test_any_other_system_grant_is_still_refused(privilege: str, suffix: str) -> None:
    from test_runtime_privileges_live import runtime_roles

    with runtime_roles("clickhouse") as held:
        native = prepare(held)
        held.command(f"GRANT {privilege} TO `{held.username}`{suffix}")
        with pytest.raises(sde.MigrationRefused, match="runtime"):
            native.qualify(["events"], ["events", WATERMARK_TABLE])

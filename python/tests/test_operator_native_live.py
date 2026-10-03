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


def test_a_clickhouse_administrator_beside_the_operator_is_trusted() -> None:
    """Finding 2 of the general test: the operator was refused beside the server's administrator.

    A user with a direct global ACCESS MANAGEMENT grants itself anything, so refusing it protects
    nothing - this protocol does not revoke administrative powers, and a PostgreSQL superuser,
    who appears in no ACL, was never refused. This one also holds SELECT and INSERT on every
    table, as the administrator in the general test did.
    """
    from test_runtime_privileges_live import outsider, runtime_roles

    with runtime_roles("clickhouse") as held:
        native = prepare(held)
        with outsider(
            "clickhouse",
            held.namespace + "_dba",
            "GRANT ACCESS MANAGEMENT ON *.* TO {who}",
            "GRANT SELECT, INSERT ON *.* TO {who}",
        ):
            assert native.qualify(["events"], ["events", WATERMARK_TABLE]) == (held.username,)


def test_a_global_reader_refuses_a_cutover_and_is_named() -> None:
    """Global SELECT without ACCESS MANAGEMENT - a monitoring or backup login - still refuses.

    It would keep reading the retired source after a cutover, and nothing here can revoke what
    it holds on every database. The refusal names it and the grant, so that it can be acted on.
    """
    from test_runtime_privileges_live import outsider, runtime_roles

    with runtime_roles("clickhouse") as held:
        native = prepare(held)
        name = held.namespace + "_monitor"
        with (
            outsider("clickhouse", name, "GRANT SELECT ON *.* TO {who}"),
            pytest.raises(sde.MigrationRefused, match="undeclared") as refused,
        ):
            native.qualify(["events"], ["events", WATERMARK_TABLE])
        assert f"user {name} (SELECT ON *.*)" in str(refused.value), refused.value
        assert held.operator.write_fence("events", project_id="1" * 32).state().holds == ()


def test_administration_through_a_role_is_not_trusted() -> None:
    """Only a user's own grant makes an administrator; a role's covering grant still refuses."""
    from test_runtime_privileges_live import outsider, runtime_roles

    with runtime_roles("clickhouse") as held:
        native = prepare(held)
        name = held.namespace + "_admins"
        with (
            outsider("clickhouse", name, "GRANT ACCESS MANAGEMENT ON *.* TO {who}", role=True),
            pytest.raises(sde.MigrationRefused, match="undeclared") as refused,
        ):
            native.qualify(["events"], ["events", WATERMARK_TABLE])
        assert f"role {name} (ACCESS MANAGEMENT ON *.*)" in str(refused.value), refused.value


def test_an_index_build_qualifies_its_logins_without_the_reader_rule(roles: Any) -> None:
    """``readers=False``: no rule for another grantee, the same rules for the runtime logins."""
    from test_runtime_privileges_live import outsider

    native = prepare(roles)
    quote = native.quote
    table = f"{quote(roles.namespace)}.{quote('events')}"
    with outsider(native.dialect, roles.namespace + "_other", f"GRANT SELECT ON {table} TO {{who}}",
                  role=True):
        with pytest.raises(sde.MigrationRefused, match="undeclared"):
            native.qualify(["events"], ["events", WATERMARK_TABLE])
        assert native.qualify(["events"], ["events", WATERMARK_TABLE], readers=False) == (
            roles.username,
        )
        privilege = "UPDATE" if native.dialect == "postgres" else "ALTER ADD COLUMN"
        roles.command(f"GRANT {privilege} ON {table} TO {quote(roles.username)}")
        with pytest.raises(sde.MigrationRefused, match="runtime"):
            native.qualify(["events"], ["events", WATERMARK_TABLE], readers=False)


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


def test_the_documented_index_verification_grant_is_admitted_on_clickhouse() -> None:
    """docs/runtime-roles.md offers this grant so a session can verify declared skipping indexes.

    It shows a login the indexes of its own tables only (measured on 24.8), and the qualification
    used to refuse it - a customer who followed the page could not stage or cut over.
    """
    from test_runtime_privileges_live import runtime_roles

    with runtime_roles("clickhouse") as held:
        native = prepare(held)
        held.command(f"GRANT SELECT ON system.data_skipping_indices TO `{held.username}`")
        assert native.qualify(["events"], ["events", WATERMARK_TABLE]) == (held.username,)

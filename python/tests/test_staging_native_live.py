"""Stage-owned CREATE is recoverable and cannot adopt an unrelated native object."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from test_runtime_privileges_live import roles as roles
from test_runtime_privileges_live import runtime_roles

import sde
from sde.engines._operator import NativeOperator
from sde.engines._staging import NativeStaging, creation_marker

PROJECT, STAGE = "1" * 32, "5" * 32


@pytest.fixture
def postgres_roles() -> Iterator[Any]:
    with runtime_roles("postgres") as roles:
        yield roles


def setup(roles: Any, *, indexes: bool = False) -> tuple[NativeStaging, sde.PhysicalLayout, str]:
    table = sde.staging_table_name(STAGE, 1)
    layout = sde.PhysicalLayout(
        tables={"Event": table},
        columns={
            "Event": {
                "id": "bigint" if roles.operator.dialect == "postgres" else "Int64",
                "value": "text" if roles.operator.dialect == "postgres" else "String",
            }
        },
        indexes=({"name": "staged_value_idx", "entity": "Event", "columns": ["value"]},)
        if indexes
        else (),
    )
    creator = NativeStaging(NativeOperator(roles.operator, [roles.runtime]))
    return creator, layout, creation_marker(PROJECT, STAGE, table)


def test_owned_creation_repeats_with_the_same_native_identity(roles: Any) -> None:
    creator, layout, marker = setup(roles)
    creator.preflight(layout, {"Event": ["id"]})
    first = creator.create_table(
        entity="Event", layout=layout, keys={"Event": ["id"]}, marker=marker
    )
    roles.operator.validate_schema(layout)
    assert creator.marker(first.name) == (first.object, marker)
    second = NativeStaging(NativeOperator(roles.operator, [roles.runtime])).create_table(
        entity="Event", layout=layout, keys={"Event": ["id"]}, marker=marker
    )
    assert second == first
    with pytest.raises(sde.MigrationRefused, match="already exists"):
        creator.preflight(layout, {"Event": ["id"]})


def test_a_foreign_table_is_not_adopted_or_marked(roles: Any) -> None:
    creator, layout, marker = setup(roles)
    roles.operator.ensure_schema(layout, keys={"Event": ["id"]})
    table = layout.tables["Event"]
    roles.operator.insert(table, {"id": 1, "value": "foreign object"})
    identity, original_marker = creator.marker(table)
    with pytest.raises(sde.MigrationRefused, match="creation marker"):
        creator.create_table(entity="Event", layout=layout, keys={"Event": ["id"]}, marker=marker)
    assert creator.marker(table) == (identity, original_marker)
    assert roles.operator.get(table, {"id": 1}) == {"id": 1, "value": "foreign object"}


def test_a_different_stage_marker_does_not_authorize_recovery(roles: Any) -> None:
    creator, layout, marker = setup(roles)
    creator.create_table(entity="Event", layout=layout, keys={"Event": ["id"]}, marker=marker)
    other = creation_marker(PROJECT, "6" * 32, layout.tables["Event"])
    with pytest.raises(sde.MigrationRefused, match="creation marker"):
        creator.create_table(entity="Event", layout=layout, keys={"Event": ["id"]}, marker=other)


def test_invalid_marker_is_refused_before_creating_anything(roles: Any) -> None:
    creator, layout, _marker = setup(roles)
    with pytest.raises(sde.MigrationRefused, match="protocol metadata"):
        creator.create_table(
            entity="Event", layout=layout, keys={"Event": ["id"]}, marker="not a stage marker'"
        )
    assert creator.marker(layout.tables["Event"]) is None


def test_postgres_indexes_are_bound_to_the_owned_table(postgres_roles: Any) -> None:
    roles = postgres_roles
    creator, layout, marker = setup(roles, indexes=True)
    creator.preflight(layout, {"Event": ["id"]})
    created = creator.create_table(
        entity="Event", layout=layout, keys={"Event": ["id"]}, marker=marker
    )
    creator.create_indexes(layout)
    first = creator._index("staged_value_idx")
    assert first[0][0] == created.object
    creator.create_indexes(layout)
    assert creator._index("staged_value_idx") == first


def test_index_on_another_table_is_not_a_successful_stage(postgres_roles: Any) -> None:
    roles = postgres_roles
    creator, layout, marker = setup(roles, indexes=True)
    creator.create_table(entity="Event", layout=layout, keys={"Event": ["id"]}, marker=marker)
    roles.command("CREATE TABLE unrelated (id bigint PRIMARY KEY, value text)")
    roles.command("CREATE INDEX staged_value_idx ON unrelated (value)")
    with pytest.raises(sde.MigrationRefused, match="owned table"):
        creator.create_indexes(layout)


def test_covering_index_is_not_the_requested_two_key_index(postgres_roles: Any) -> None:
    from dataclasses import replace

    creator, original, marker = setup(postgres_roles)
    layout = replace(
        original,
        indexes=({"name": "staged_value_idx", "entity": "Event", "columns": ["id", "value"]},),
    )
    creator.create_table(entity="Event", layout=layout, keys={"Event": ["id"]}, marker=marker)
    postgres_roles.command(
        f'CREATE INDEX staged_value_idx ON "{layout.tables["Event"]}" (id) INCLUDE (value)'
    )
    with pytest.raises(sde.MigrationRefused, match="owned table"):
        creator.create_indexes(layout)

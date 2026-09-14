"""A query plan must refuse before I/O and bind every positional value to its own predicate."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import Any
from uuid import UUID

import pytest

from sde.query import QueryRefused, Range, ReadColumn, plan_read, query_value, read_sql


def sql(plan: Any, dialect: str = "postgres", *, count: bool = False) -> tuple[str, list[Any]]:
    parameters = []

    def bind(value: Any) -> str:
        parameters.append(value)
        return "%s" if dialect == "postgres" else f"%(q{len(parameters)})s"

    return read_sql("events", plan, dialect=dialect, parameter=bind, count=count), parameters


def test_three_column_cursor_binds_each_prefix_occurrence_in_statement_order() -> None:
    columns = tuple(ReadColumn(name, "int64") for name in ("a", "b", "c"))
    query = plan_read(columns, ["a", "b", "c"], after={"c": 3, "a": 1, "b": 2})
    statement, parameters = sql(query)
    assert parameters == [1, 1, 2, 1, 2, 3, 101]
    assert statement.count("%s") == len(parameters)
    assert statement.endswith(
        'ORDER BY "a" ASC NULLS LAST, "b" ASC NULLS LAST, "c" ASC NULLS LAST LIMIT %s'
    )


@pytest.mark.parametrize("descending", [False, True])
def test_null_sort_cursor_keeps_only_equal_null_prefix_then_primary_key(descending: bool) -> None:
    query = plan_read(
        [ReadColumn("id", "int64"), ReadColumn("label", "string")],
        ["id"],
        order_by="label",
        descending=descending,
        after={"label": None, "id": 7},
    )
    statement, parameters = sql(query)
    assert '(("label" COLLATE "C") IS NULL AND ' in statement
    assert ('"id" < %s' if descending else '"id" > %s') in statement
    assert parameters == [7, 101]


def test_uuid_and_text_order_have_explicit_portable_expressions() -> None:
    key = UUID("00000000-0000-0001-0000-000000000000")
    query = plan_read(
        [ReadColumn("id", "uuid"), ReadColumn("tag", "string")],
        ["id"],
        where={"tag": "é"},
        after={"id": key},
    )
    statement, params = sql(query, "clickhouse")
    tick = chr(96)
    assert f"FROM {tick}events{tick} FINAL" in statement
    assert f"toString({tick}id{tick})" in statement and str(key) in params
    pg_statement, pg_params = sql(query)
    assert '("tag" COLLATE "C") = %s' in pg_statement and key in pg_params


def test_timestamp_without_zone_predicate_is_utc_naive_and_aware_type_stays_aware() -> None:
    supplied = "2026-09-14T12:00:00.123456+02:00"
    assert query_value(ReadColumn("at", "timestamp"), supplied) == datetime(
        2026, 9, 14, 10, microsecond=123456
    )
    assert query_value(ReadColumn("at", "timestamptz"), supplied) == datetime(
        2026, 9, 14, 10, microsecond=123456, tzinfo=UTC
    )
    with pytest.raises(QueryRefused):
        query_value(ReadColumn("at", "timestamp"), "2026-09-14T12:00:00.1234567Z")


def test_decimal_comparison_preserves_a_finer_bound_and_ignores_global_context() -> None:
    with localcontext() as context:
        context.prec = 3
        query = plan_read(
            [ReadColumn("id", "int64"), ReadColumn("amount", "decimal(12,2)")],
            ["id"],
            bounds=Range("amount", low=Decimal("123456.005")),
        )
        statement, parameters = sql(query, "clickhouse")
    assert "Nullable(Decimal(76,3))" in statement
    assert parameters == ["123456.005", 101]
    with pytest.raises(QueryRefused, match="76 digits"):
        query_value(ReadColumn("amount", "decimal(12,2)"), Decimal("1e999999"))


@pytest.mark.parametrize(
    "options",
    [
        {"limit": 0},
        {"limit": 1001},
        {"limit": True},
        {"descending": "yes"},
        {"after": {"label": "a"}},
        {"bounds": Range("label", "a", "z")},
        {"bounds": Range("id")},
        {"where": {"missing": 3}},
        {"order_by": "missing"},
    ],
)
def test_invalid_request_refuses_during_planning(options: dict[str, Any]) -> None:
    with pytest.raises(QueryRefused):
        plan_read([ReadColumn("id", "int64"), ReadColumn("label", "string")], ["id"], **options)


def test_count_does_not_require_a_paginated_order_for_a_float_key() -> None:
    query = plan_read([ReadColumn("id", "float64")], ["id"], paginate=False)
    statement, parameters = sql(query, count=True)
    assert statement == 'SELECT CAST(count(*) AS text) AS sde_count FROM "events"'
    assert parameters == []


def test_nullable_generation_does_not_reinterpret_existing_explicit_or_auto_maps() -> None:
    import sde

    sde.clear_registry()

    @sde.entity
    class Event:
        id: int
        note: str | None

    model = sde.build_model(Event)
    group = sde.colocation_groups(model)[0]
    generated = sde.default_layout(model, group, dialect="clickhouse")
    assert generated.columns["Event"] == {"id": "Int64", "note": "Nullable(String)"}
    explicit = {
        "tables": {"Event": "event"},
        "columns": {"Event": {"id": "Int64", "note": "String"}},
    }
    for layout, expected in [
        (explicit, explicit["columns"]["Event"]),
        ({"auto": True}, {"id": "bigint", "note": "text"}),
    ]:
        placement = sde.load_map(
            {
                "contract": 3,
                "model_version": model.version,
                "map_version": 1,
                "groups": {"Event": {"source": {"id": "source", "engine": "db", "layout": layout}}},
            },
            model=model,
        )
        assert dict(placement.groups["Event"].source.layout.columns["Event"]) == expected


def test_count_does_not_inherit_page_key_width_and_wide_decimal_cursor_refuses() -> None:
    columns = tuple(ReadColumn(f"k{i}", "int64") for i in range(33))
    query = plan_read(columns, [column.name for column in columns], paginate=False)
    assert sql(query, count=True)[1] == []
    with pytest.raises(QueryRefused, match="76 digits"):
        plan_read([ReadColumn("id", "decimal(77,0)")], ["id"])

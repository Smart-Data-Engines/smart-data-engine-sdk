"""Logical pages use native filtering/order and preserve every tie across page boundaries."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

import pytest
from test_runtime_privileges_live import runtime_roles

import sde

IDS = [
    UUID("00000000-0000-0001-0000-000000000000"),
    UUID("00000000-0000-0000-ffff-ffffffffffff"),
    UUID("ffffffff-ffff-ffff-0000-000000000000"),
    UUID("00000000-0000-0000-0000-000000000001"),
    UUID("00000000-0000-0000-0000-000000000002"),
    UUID("00000000-0000-0000-0000-000000000003"),
]
BASE = datetime(2026, 9, 14, microsecond=123456, tzinfo=UTC)


@contextmanager
def fixture(dialect: str, *, hashed: bool = False) -> Iterator[tuple[Any, ...]]:
    with runtime_roles(dialect) as role:
        sde.clear_registry()

        @sde.entity
        class Event:
            id: UUID
            label: str | None
            at: datetime
            amount: Annotated[Decimal, sde.precision(12, 2)]

        model = sde.build_model(Event)
        names = None
        if hashed:
            model, names = sde.hash_identifiers(model, b"q" * 32)
        group = sde.colocation_groups(model)[0]
        entity = model.entities[0].name
        layout = sde.default_layout(model, group, dialect=dialect)
        raw_layout = {
            "tables": dict(layout.tables),
            "columns": {k: dict(v) for k, v in layout.columns.items()},
        }
        placement = sde.load_map(
            {
                "contract": 3,
                "model_version": model.version,
                "map_version": 1,
                "groups": {
                    group.name: {"source": {"id": "source", "engine": "db", "layout": raw_layout}}
                },
            },
            model=model,
        )
        role.operator.ensure_schema(layout, keys={entity: model.entities[0].key})
        role.grant(layout.table_for(entity))
        recorder = sde.Recorder(model.version)
        session = sde.Session(
            model, placement, {"db": role.runtime}, recorder=recorder, names=names
        )
        labels = ["z", None, "é", "a", None, "a"]
        rows = [
            {
                "id": key,
                "label": labels[i],
                "at": BASE + timedelta(microseconds=i // 2),
                "amount": Decimal(i) + Decimal("0.25"),
            }
            for i, key in enumerate(IDS)
        ]
        session.save_many("Event", rows)
        recorder.roll()
        yield session, role, rows, recorder


def pages(session: sde.Session, **options: Any) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    after = None
    for _ in range(20):
        page = session.scan("Event", after=after, **options)
        assert len(page.rows) <= options.get("limit", 100)
        output.extend(page.rows)
        if page.next_after is None:
            return output
        after = page.next_after
    raise AssertionError("pagination did not terminate")


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
@pytest.mark.parametrize("size", [1, 2, 3, 6])
def test_uuid_pages_use_one_portable_order_and_never_drop_the_sentinel(
    dialect: str,
    size: int,
) -> None:
    with fixture(dialect) as (session, _role, rows, _recorder):
        assert pages(session, limit=size) == sorted(rows, key=lambda row: row["id"])
        assert pages(session, limit=size, descending=True) == sorted(
            rows, key=lambda row: row["id"], reverse=True
        )


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
@pytest.mark.parametrize("hashed", [False, True])
def test_nullable_sort_ties_keep_nulls_last_in_both_directions(dialect: str, hashed: bool) -> None:
    with fixture(dialect, hashed=hashed) as (session, _role, rows, _recorder):
        for descending in [False, True]:
            present = sorted(
                [row for row in rows if row["label"] is not None],
                key=lambda row: (row["label"], row["id"]),
                reverse=descending,
            )
            absent = sorted(
                [row for row in rows if row["label"] is None],
                key=lambda row: row["id"],
                reverse=descending,
            )
            assert (
                pages(session, order_by="label", descending=descending, limit=1) == present + absent
            )
        assert session.count("Event", where={"label": None}) == 2


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_half_open_time_range_with_equality_and_microsecond_ties(dialect: str) -> None:
    with fixture(dialect) as (session, _role, rows, recorder):
        wanted = [
            row
            for row in rows
            if row["label"] == "a" and BASE <= row["at"] < BASE + timedelta(microseconds=3)
        ]
        bounds = sde.Range("at", BASE, BASE + timedelta(microseconds=3))
        assert pages(
            session, where={"label": "a"}, bounds=bounds, order_by="at", limit=1
        ) == sorted(wanted, key=lambda row: (row["at"], row["id"]))
        assert session.count("Event", where={"label": "a"}, bounds=bounds) == len(wanted)
        window = recorder.roll()
        assert window is not None
        assert {stat.kind for stat in window.shapes} == {"range_read", "aggregate"}
        assert next(stat for stat in window.shapes if stat.kind == "aggregate").rows == 1


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_decimal_bounds_are_not_rounded_to_the_stored_scale(dialect: str) -> None:
    with fixture(dialect) as (session, _role, rows, _recorder):
        expected = [row for row in rows if Decimal("1.249") <= row["amount"] < Decimal("3.251")]
        result = pages(
            session,
            bounds=sde.Range("amount", Decimal("1.249"), Decimal("3.251")),
            order_by="amount",
            limit=1,
        )
        assert result == sorted(expected, key=lambda row: (row["amount"], row["id"]))
        assert (
            session.count("Event", bounds=sde.Range("amount", Decimal("1.249"), Decimal("3.251")))
            == 3
        )


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_numeric_summary_is_exact_on_empty_and_filtered_decimal_data(dialect: str) -> None:
    with fixture(dialect) as (session, _role, _rows, recorder):
        result = session.summarize("Event", "amount", where={"label": "a"}, mean_scale=3)
        assert result == sde.NumericSummary(
            2, 2, Decimal("3.25"), Decimal("5.25"), Decimal("8.50"), Decimal("4.250")
        )
        empty = session.summarize("Event", "amount", where={"label": "absent"})
        assert empty == sde.NumericSummary(0, 0, None, None, None, None)
        window = recorder.roll()
        assert window is not None
        assert window.shapes[0].rows == 2
        assert "8.50" not in str(window)


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_summary_casts_int64_before_summing_and_keeps_large_results(dialect: str) -> None:
    with runtime_roles(dialect) as role:
        sde.clear_registry()

        @sde.entity
        class Number:
            id: int
            value: int | None

        model = sde.build_model(Number)
        layout = sde.default_layout(model, sde.colocation_groups(model)[0], dialect=dialect)
        raw = {
            "tables": dict(layout.tables),
            "columns": {key: dict(value) for key, value in layout.columns.items()},
        }
        placement = sde.load_map(
            {
                "contract": 3,
                "model_version": model.version,
                "map_version": 1,
                "groups": {"Number": {"source": {"id": "source", "engine": "db", "layout": raw}}},
            },
            model=model,
        )
        role.operator.ensure_schema(layout, keys={"Number": ["id"]})
        role.grant("number")
        session = sde.Session(model, placement, {"db": role.runtime})
        session.save_many(
            "Number",
            [
                {"id": 1, "value": 2**63 - 1},
                {"id": 2, "value": 2**63 - 1},
                {"id": 3, "value": None},
            ],
        )
        result = session.summarize("Number", "value")
        assert result.total == 2**64 - 2
        assert result.mean == Decimal("9223372036854775807.000000")
        assert result.count == 3 and result.non_null_count == 2
        empty = session.summarize("Number", "value", where={"id": 3})
        assert empty == sde.NumericSummary(1, 0, None, None, None, None)


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_scan_projects_only_model_fields_and_native_revocation_refuses_reads(dialect: str) -> None:
    from sde.schema import QUOTE

    with fixture(dialect) as (session, role, rows, _recorder):
        quote = QUOTE[dialect]
        role.command(
            f"ALTER TABLE {quote('event')} ADD COLUMN extra "
            + ("text" if dialect == "postgres" else "String")
        )
        assert session.scan("Event", limit=1).rows[0].keys() == rows[0].keys()
        role.grant("event", revoke=True)
        with pytest.raises(sde.EngineError):
            session.scan("Event")
        with pytest.raises(sde.EngineError):
            session.count("Event")
        with pytest.raises(sde.EngineError):
            session.summarize("Event", "amount")


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_a_generation_column_is_not_a_logical_projection(dialect: str) -> None:
    from test_generation_session_live import fixture as generation_map

    with runtime_roles(dialect) as role:
        if dialect == "postgres":
            role.command("CREATE TABLE generations (id bigint PRIMARY KEY)")
        else:
            role.command(
                "CREATE TABLE generations (id Int64) ENGINE=ReplacingMergeTree ORDER BY id"
            )
        model, placement = generation_map(role.operator, "generations")
        sde.prepare_schema(model, placement, {"db": role.operator}, project_id="1" * 32)
        role.grant("generations")
        session = sde.Session(model, placement, {"db": role.runtime}, project_id="1" * 32)
        session.save_many("Record", [{"id": 1}, {"id": 2}])
        page = session.scan("Record", limit=1)
        assert page.rows == ({"id": 1},) and page.next_after == {"id": 1}
        assert session.count("Record") == 2
        assert session.summarize("Record", "id").total == 3


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_timestamp_order_and_bounds_do_not_depend_on_connection_timezone(dialect: str) -> None:
    from sde.testing.loader import model_from_neutral

    with runtime_roles(dialect) as role:
        model = model_from_neutral(
            {
                "entities": [
                    {
                        "name": "Event",
                        "fields": [
                            {"name": "id", "type": "int64"},
                            {"name": "at", "type": "timestamp"},
                        ],
                        "key": ["id"],
                    }
                ]
            }
        )
        layout = sde.default_layout(model, sde.colocation_groups(model)[0], dialect=dialect)
        role.operator.ensure_schema(layout, keys={"Event": ["id"]})
        role.grant("event")
        role.operator.insert_many(
            "event",
            [
                {"id": 1, "at": datetime(2026, 9, 14, 10, microsecond=123456)},
                {"id": 2, "at": datetime(2026, 9, 14, 10, microsecond=123457)},
            ],
        )
        if dialect == "postgres":
            role.runtime._cx.execute("SET TIME ZONE 'America/New_York'")
        else:
            role.runtime._cx.set_client_setting("session_timezone", "America/New_York")
        raw = {
            "tables": dict(layout.tables),
            "columns": {k: dict(v) for k, v in layout.columns.items()},
        }
        placement = sde.load_map(
            {
                "contract": 3,
                "model_version": model.version,
                "map_version": 1,
                "groups": {"Event": {"source": {"id": "source", "engine": "db", "layout": raw}}},
            },
            model=model,
        )
        session = sde.Session(model, placement, {"db": role.runtime})
        first = session.scan(
            "Event",
            bounds=sde.Range(
                "at", "2026-09-14T12:00:00.123456+02:00", "2026-09-14T10:00:00.123458Z"
            ),
            order_by="at",
            limit=1,
        )
        assert first.rows == ({"id": 1, "at": datetime(2026, 9, 14, 10, microsecond=123456)},)
        assert session.scan("Event", order_by="at", after=first.next_after).rows == (
            {"id": 2, "at": datetime(2026, 9, 14, 10, microsecond=123457)},
        )


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_float_projection_preserves_nonfinite_values_and_ranges_exclude_nan(dialect: str) -> None:
    import math

    with runtime_roles(dialect) as role:
        sde.clear_registry()

        @sde.entity
        class FloatValue:
            id: int
            value: float

        model = sde.build_model(FloatValue)
        layout = sde.default_layout(model, sde.colocation_groups(model)[0], dialect=dialect)
        role.operator.ensure_schema(layout, keys={"FloatValue": ["id"]})
        table = layout.table_for("FloatValue")
        role.grant(table)
        if dialect == "postgres":
            role.command(
                "INSERT INTO float_value VALUES (1,0.25),(2,'NaN'),(3,'Infinity'),(4,'-Infinity')"
            )
        else:
            role.command(
                "INSERT INTO float_value SELECT 1,0.25 UNION ALL SELECT 2,toFloat64('nan') "
                "UNION ALL SELECT 3,toFloat64('inf') UNION ALL SELECT 4,toFloat64('-inf')"
            )
        raw = {
            "tables": dict(layout.tables),
            "columns": {k: dict(v) for k, v in layout.columns.items()},
        }
        placement = sde.load_map(
            {
                "contract": 3,
                "model_version": model.version,
                "map_version": 1,
                "groups": {
                    "FloatValue": {"source": {"id": "source", "engine": "db", "layout": raw}}
                },
            },
            model=model,
        )
        session = sde.Session(model, placement, {"db": role.runtime})
        page = session.scan("FloatValue")
        assert [row["id"] for row in page.rows] == [1, 2, 3, 4]
        assert page.rows[0]["value"] == 0.25 and math.isnan(page.rows[1]["value"])
        assert page.rows[2]["value"] == float("inf") and page.rows[3]["value"] == float("-inf")
        assert [
            row["id"] for row in session.scan("FloatValue", bounds=sde.Range("value", 0)).rows
        ] == [1, 3]
        assert session.count("FloatValue", bounds=sde.Range("value", 0)) == 2


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_decimal_projection_preserves_declared_scale_without_context_rounding(dialect: str) -> None:
    from decimal import localcontext

    with runtime_roles(dialect) as role:
        sde.clear_registry()

        @sde.entity
        class Price:
            id: int
            value: Annotated[Decimal, sde.precision(38, 18)]

        model = sde.build_model(Price)
        layout = sde.default_layout(model, sde.colocation_groups(model)[0], dialect=dialect)
        role.operator.ensure_schema(layout, keys={"Price": ["id"]})
        role.grant("price")
        literal = "99999999999999999999.123456789012345678"
        role.command(
            f"INSERT INTO price VALUES (1,{literal}),(2,7)"
            if dialect == "postgres"
            else f"INSERT INTO price SELECT 1,CAST('{literal}' AS Decimal(38,18)) "
            "UNION ALL SELECT 2,CAST('7' AS Decimal(38,18))"
        )
        raw = {
            "tables": dict(layout.tables),
            "columns": {k: dict(v) for k, v in layout.columns.items()},
        }
        placement = sde.load_map(
            {
                "contract": 3,
                "model_version": model.version,
                "map_version": 1,
                "groups": {"Price": {"source": {"id": "source", "engine": "db", "layout": raw}}},
            },
            model=model,
        )
        session = sde.Session(model, placement, {"db": role.runtime})
        with localcontext() as context:
            context.prec = 3
            rows = session.scan("Price").rows
        assert str(rows[0]["value"]) == literal
        assert str(rows[1]["value"]) == "7.000000000000000000"

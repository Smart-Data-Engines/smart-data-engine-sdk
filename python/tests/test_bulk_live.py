"""Native bulk acceptance: actual values, conflicts, generation and transaction boundaries."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from test_generation_session_live import fixture as generation_map
from test_runtime_privileges_live import runtime_roles
from test_write_fence_live import guarded as guarded

import sde


@contextmanager
def native(dialect: str, *, json_field: bool = False) -> Iterator[tuple[Any, ...]]:
    with runtime_roles(dialect) as role:
        sde.clear_registry()
        if json_field:

            @sde.entity
            class Event:
                id: int
                value: dict[str, Any]
        else:

            @sde.entity
            class Event:
                id: int
                value: datetime

        model = sde.build_model(Event)
        layout = sde.default_layout(model, sde.colocation_groups(model)[0], dialect=dialect)
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
                    "Event": {"source": {"id": "source", "engine": "db", "layout": raw_layout}}
                },
            },
            model=model,
        )
        role.operator.ensure_schema(layout, keys={"Event": ["id"]})
        role.grant(layout.table_for("Event"))
        session = sde.Session(model, placement, {"db": role.runtime})
        yield session, role, layout.table_for("Event")


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_one_native_batch_preserves_all_rows_and_microseconds(dialect: str) -> None:
    with native(dialect) as (session, role, table):
        values = [
            {"id": i, "value": datetime(2026, 9, 14, microsecond=123456 + i, tzinfo=UTC)}
            for i in range(1000)
        ]
        session.save_many("Event", values)
        assert role.operator.count(table) == 1000
        assert role.operator.key_range(table, ["id"]) == values
        assert session.get("Event", {"id": 999}) == values[-1]


def test_postgres_uses_one_statement_and_keeps_conflicts_and_nested_rollback() -> None:
    with native("postgres") as (session, role, table):
        role.command("CREATE TABLE insert_calls (n integer)")
        role.command(f'GRANT INSERT ON insert_calls TO "{role.username}"')
        role.command(
            "CREATE FUNCTION record_insert() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN INSERT INTO insert_calls VALUES (1); RETURN NULL; END $$"
        )
        role.command(
            f'CREATE TRIGGER count_insert AFTER INSERT ON "{table}" '
            "FOR EACH STATEMENT EXECUTE FUNCTION record_insert()"
        )
        value = datetime(2026, 9, 14, tzinfo=UTC)
        batch = [{"id": 1, "value": value}, {"id": 2, "value": value}]
        session.save_many("Event", batch)
        assert role.operator._cx.execute("SELECT count(*) FROM insert_calls").fetchone()[0] == 1
        with pytest.raises(sde.EngineError):
            session.save_many("Event", [{"id": 3, "value": value}, batch[0]])
        assert role.operator.count(table) == 2
        with session.transaction("Event"):
            session.save_many("Event", [{"id": 3, "value": value}])
            with pytest.raises(sde.EngineError), session.transaction("Event"):
                session.save_many("Event", [{"id": 4, "value": value}, batch[0]])
        assert role.operator.count(table) == 3
        with pytest.raises(ValueError), session.transaction("Event"):
            with session.transaction("Event"):
                session.save_many("Event", [{"id": 5, "value": value}])
            raise ValueError("outer rollback")
        assert role.operator.count(table) == 3
        with pytest.raises(sde.EngineError, match="aborted"), session.transaction("Event"):
            session.save_many("Event", [{"id": 6, "value": value}])
            with pytest.raises(sde.EngineError):
                session.save_many("Event", batch)
        assert role.operator.count(table) == 3


def test_postgres_native_json_is_a_value_not_a_driver_object_or_array() -> None:
    with native("postgres", json_field=True) as (session, role, table):
        values = [
            {"id": 1, "value": {"nested": ["zażółć", {"n": 7}]}},
            {"id": 2, "value": [1, 2, {"a": True}]},
        ]
        with session.transaction("Event"):
            session.save_many("Event", values)
            values[0]["value"]["nested"][0] = "changed"
        assert role.operator.get(table, {"id": 1})["value"]["nested"][0] == "zażółć"
        assert role.operator.get(table, {"id": 2})["value"] == [1, 2, {"a": True}]


@pytest.mark.parametrize("guarded", ["postgres", "clickhouse"], indirect=True)
def test_old_session_batch_cannot_borrow_new_generation(guarded: Any) -> None:
    engine, table, fence = guarded
    model, first = generation_map(engine, table)
    sde.prepare_schema(model, first, {"db": engine}, project_id="1" * 32)
    old = sde.Session(model, first, {"db": engine}, project_id="1" * 32)
    old.save_many("Record", [{"id": 1}, {"id": 2}])
    fence.freeze("4" * 32)
    fence.advance(2)
    fence.release("4" * 32)
    model, second = generation_map(engine, table, epoch=2, version=2)
    current = sde.Session(model, second, {"db": engine}, project_id="1" * 32)
    with pytest.raises(sde.EngineError):
        old.save_many("Record", [{"id": 3}, {"id": 4}])
    current.save_many("Record", [{"id": 5}, {"id": 6}])
    assert engine.count(table) == 4
    assert engine.get(table, {"id": 5})[sde.WRITE_EPOCH_COLUMN] == 2


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_bad_batch_and_missing_copy_capability_never_write_native_source(dialect: str) -> None:
    with native(dialect) as (session, role, table):
        with pytest.raises(sde.BulkWriteRefused):
            session.save_many("Event", [{"id": 1, "value": datetime.now(UTC)}, {"id": 2}])
        assert role.operator.count(table) == 0
        with pytest.raises(sde.BulkWriteRefused):
            role.runtime.insert_many(table, [{"a b": 1, "c": 2}, {"a": 1, "b c": 2}])
        assert role.operator.count(table) == 0


@pytest.mark.parametrize(
    "source_dialect,target_dialect", [("postgres", "clickhouse"), ("clickhouse", "postgres")]
)
def test_real_bulk_fanout_and_transaction_queue(source_dialect: str, target_dialect: str) -> None:
    with (
        native(source_dialect) as (initial, source, table),
        runtime_roles(target_dialect) as target,
    ):
        model = initial.model
        group = sde.colocation_groups(model)[0]
        layout = sde.default_layout(model, group, dialect=target_dialect)
        target.operator.ensure_schema(layout, keys={"Event": ["id"]})
        target.grant(table)

        def record(value: Any) -> dict[str, Any]:
            return {
                "tables": dict(value.tables),
                "columns": {k: dict(v) for k, v in value.columns.items()},
            }

        placement = sde.load_map(
            {
                "contract": 3,
                "model_version": model.version,
                "map_version": 2,
                "groups": {
                    "Event": {
                        "source": {
                            "id": "source",
                            "engine": "source",
                            "layout": record(initial.placement.groups["Event"].source.layout),
                        },
                        "derived": [
                            {
                                "id": "copy",
                                "engine": "copy",
                                "layout": record(layout),
                                "lag_budget_ms": 30000,
                            }
                        ],
                        "also_write": ["copy"],
                    }
                },
            },
            model=model,
        )
        session = sde.Session(model, placement, {"source": source.runtime, "copy": target.runtime})
        rows = [
            {"id": i, "value": datetime(2026, 9, 14, microsecond=123456 + i, tzinfo=UTC)}
            for i in range(3)
        ]
        if source_dialect == "postgres":
            with session.transaction("Event"):
                with session.transaction("Event"):
                    session.save_many("Event", rows)
                assert target.operator.count(table) == 0
            with pytest.raises(ValueError), session.transaction("Event"):
                session.save_many("Event", [{"id": 99, "value": rows[0]["value"]}])
                raise ValueError("rollback")
        else:
            session.save_many("Event", rows)
        assert source.operator.key_range(table, ["id"]) == rows
        assert target.operator.key_range(table, ["id"]) == rows


@pytest.mark.parametrize("operation", ["save", "save_many", "copy_in"])
def test_clickhouse_lost_response_does_not_replay_an_accepted_insert(
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from http.client import RemoteDisconnected

    from urllib3.exceptions import ProtocolError

    with native("clickhouse") as (session, role, table):
        transport = role.runtime._cx.http
        pool = getattr(transport, "pool", transport)
        request = pool.request
        accepted: list[int] = []

        def lose_response(method: str, url: str, **options: Any) -> Any:
            # Native INSERT carries an encoded block generator; DESCRIBE/query use bytes or text.
            body = options.get("body")
            inserting = body is not None and not isinstance(body, (str, bytes, bytearray))
            response = request(method, url, **options)
            if inserting:
                accepted.append(response.status)
                if len(accepted) == 1:
                    response.close()
                    raise ProtocolError(
                        "controlled lost accepted response"
                    ) from RemoteDisconnected()
            return response

        values = {"id": 1, "value": datetime(2026, 9, 14, tzinfo=UTC)}
        with monkeypatch.context() as patch:
            patch.setattr(pool, "request", lose_response)
            with pytest.raises(sde.EngineError, match="not replayed"):
                if operation == "save":
                    session.save("Event", values)
                elif operation == "save_many":
                    session.save_many("Event", [values])
                else:
                    role.runtime.copy_in(table, [values])
        assert accepted == [200]
        # FINAL would collapse the duplicate and hide the very defect this test measures.
        assert role.operator._cx.query(f"SELECT count() FROM {table}").result_rows[0][0] == 1
        assert role.operator.get(table, {"id": 1}) == values

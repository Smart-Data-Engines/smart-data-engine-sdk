"""A physical design applied to real engines, and read back from their own catalogues.

Three claims, each against PostgreSQL and ClickHouse:

- a designed layout is created as declared - checked with SQL written here against the engine's
  catalogue, not with the adapter code under test;
- an existing table with another design is **refused** where a person provisions it, and only
  **reported** by a running session, which keeps serving the same rows (requirement 3.6);
- a restricted runtime login that cannot read ClickHouse's index catalogue gets "unverified", not
  a session that fails to start - the regression the first version of this check caused.

And one about the engine itself: the rule that a partition must follow the key is re-measured on
the server the suite runs against, because it rests on ClickHouse behaviour, not on ours.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from test_runtime_privileges_live import Roles, runtime_roles
from test_runtime_privileges_live import roles as roles

import sde
from sde.engines._operator import NativeOperator
from sde.engines._staging import NativeStaging, creation_marker
from sde.testing.loader import model_from_neutral

PROJECT = "1" * 32

MODEL = {
    "entities": [
        {
            "name": "Reading",
            "fields": [
                {"name": "at", "type": "timestamptz"},
                {"name": "humidity", "type": "int32"},
                {"name": "station", "type": "string"},
                {"name": "temperature", "type": "float64"},
            ],
            "key": ["station", "at"],
        }
    ]
}

COLUMNS = {
    "postgres": {
        "at": "timestamptz",
        "humidity": "integer",
        "station": "text",
        "temperature": "double precision",
    },
    "clickhouse": {
        "at": "DateTime64(6, 'UTC')",
        "humidity": "Int32",
        "station": "String",
        "temperature": "Float64",
    },
}

DESIGN: dict[str, dict[str, Any]] = {
    "postgres": {
        "key_order": {"Reading": ["at", "station"]},
        "indexes": [
            {"entity": "Reading", "name": "reading_at_brin", "columns": ["at"], "method": "brin"},
            {"entity": "Reading", "name": "reading_temperature", "columns": ["temperature"]},
        ],
    },
    "clickhouse": {
        "key_order": {"Reading": ["at", "station"]},
        "partition_by": {"Reading": {"field": "at", "granularity": "month"}},
        "indexes": [
            {
                "entity": "Reading",
                "name": "reading_temperature",
                "columns": ["temperature"],
                "method": "minmax",
                "granularity": 4,
            },
            {
                "entity": "Reading",
                "name": "reading_humidity",
                "columns": ["humidity"],
                "method": "set",
                "granularity": 2,
                "max_rows": 100,
            },
            {
                "entity": "Reading",
                "name": "reading_station",
                "columns": ["station"],
                "method": "bloom_filter",
                "granularity": 1,
            },
        ],
    },
}


def _placement(
    roles: Roles, *, design: bool, map_version: int = 1, table: str = "readings"
) -> tuple[sde.LogicalModel, sde.PlacementMap]:
    dialect = roles.operator.dialect
    model = model_from_neutral(MODEL)
    layout: dict[str, Any] = {
        "tables": {"Reading": table},
        "columns": {"Reading": COLUMNS[dialect]},
    }
    if design:
        layout.update(DESIGN[dialect])
    document = {
        "contract": 5,
        "project_id": PROJECT,
        "model_version": model.version,
        "map_version": map_version,
        "groups": {
            "Reading": {"write_epoch": 1, "source": {"id": "s", "engine": "db", "layout": layout}}
        },
    }
    return model, sde.load_map(document, model=model)


def _catalogue(roles: Roles, table: str) -> dict[str, Any]:
    """The physical design as the engine reports it, read with SQL written here."""
    if roles.operator.dialect == "postgres":
        cx = roles.operator._cx
        primary = cx.execute(
            "SELECT a.attname FROM pg_index i JOIN pg_class t ON t.oid = i.indrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(num, pos) ON true "
            "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.num "
            "WHERE i.indisprimary AND n.nspname = current_schema() AND t.relname = %s "
            "ORDER BY k.pos",
            (table,),
        ).fetchall()
        indexes = cx.execute(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = current_schema() AND tablename = %s ORDER BY indexname",
            (table,),
        ).fetchall()
        return {"key": [row[0] for row in primary], "indexes": dict(indexes)}
    cx = roles.operator._cx
    sorting, partition = cx.query(
        "SELECT sorting_key, partition_key FROM system.tables "
        "WHERE database = currentDatabase() AND name = {t:String}",
        parameters={"t": table},
    ).result_rows[0]
    indexes = cx.query(
        "SELECT name, type_full, expr, granularity FROM system.data_skipping_indices "
        "WHERE database = currentDatabase() AND table = {t:String} ORDER BY name",
        parameters={"t": table},
    ).result_rows
    return {
        "key": sorting,
        "partition": partition,
        "indexes": {
            name: (kind, expr, int(granularity)) for name, kind, expr, granularity in indexes
        },
    }


def test_a_designed_layout_is_created_as_declared(roles: Roles) -> None:
    model, placement = _placement(roles, design=True)
    sde.prepare_schema(model, placement, {"db": roles.operator}, project_id=PROJECT)
    found = _catalogue(roles, "readings")
    if roles.operator.dialect == "postgres":
        assert found["key"] == ["at", "station"]
        assert "USING brin (at)" in found["indexes"]["reading_at_brin"]
        assert "USING btree (temperature)" in found["indexes"]["reading_temperature"]
    else:
        assert found == {
            "key": "at, station",
            "partition": "toYYYYMM(at)",
            "indexes": {
                "reading_humidity": ("set(100)", "humidity", 2),
                "reading_station": ("bloom_filter", "station", 1),
                "reading_temperature": ("minmax", "temperature", 4),
            },
        }
    layout = placement.groups["Reading"].source.layout
    assert roles.operator.validate_schema(layout, keys={"Reading": ["station", "at"]}) == ()


def test_another_design_is_refused_at_provisioning_and_reported_by_a_running_session(
    roles: Roles, caplog: pytest.LogCaptureFixture
) -> None:
    dialect = roles.operator.dialect
    model, first = _placement(roles, design=False)
    sde.prepare_schema(model, first, {"db": roles.operator}, project_id=PROJECT)
    _, second = _placement(roles, design=True, map_version=2)

    # A person provisioning the new map over the old tables is told, table and aspect named.
    aspect = "primary key" if dialect == "postgres" else "sort key"
    with pytest.raises(sde.EngineError, match=f"readings: {aspect}"):
        sde.prepare_schema(model, second, {"db": roles.operator}, project_id=PROJECT)

    # An application started on the same new map keeps serving rows and says what it found.
    roles.grant("readings")
    with caplog.at_level(logging.INFO, logger="sde"):
        session = sde.Session(model, second, {"db": roles.runtime}, project_id=PROJECT)
    aspects = {finding.aspect: finding.found for finding in session.physical}
    assert aspects[aspect] == (
        "['station', 'at']" if dialect == "postgres" else "('station', 'at')"
    )
    if dialect == "postgres":
        assert aspects["index reading_at_brin"] == "absent"
    else:
        assert aspects["partition"] == "None"
        # The restricted login has no grant on the index catalogue: unverified, not absent, and
        # certainly not a session that failed to start.
        assert aspects["index reading_temperature"].startswith("unverified")
    events = [r.__dict__.get("sde_event") for r in caplog.records]
    assert "sde.schema.physical_mismatch" in events
    row = {"station": "s1", "at": _at(), "humidity": 40, "temperature": 21.5}
    session.save("Reading", row)
    assert session.get("Reading", {"station": "s1", "at": row["at"]}) is not None


def test_a_matching_table_reports_nothing_to_a_restricted_session(roles: Roles) -> None:
    model, placement = _placement(roles, design=True)
    sde.prepare_schema(model, placement, {"db": roles.operator}, project_id=PROJECT)
    roles.grant("readings")
    if roles.operator.dialect == "clickhouse":
        # With the optional grant the index catalogue is readable and the design is verified.
        roles.command(f"GRANT SELECT ON system.data_skipping_indices TO {roles.username}")
    session = sde.Session(model, placement, {"db": roles.runtime}, project_id=PROJECT)
    assert session.physical == ()


def test_a_staged_copy_is_created_with_its_whole_design(roles: Roles) -> None:
    """The staging creator builds one table at a time; the design must survive that split."""
    dialect = roles.operator.dialect
    stage = "5" * 32
    table = sde.staging_table_name(stage, 1)
    layout = sde.PhysicalLayout(
        tables={"Reading": table},
        columns={"Reading": dict(COLUMNS[dialect])},
        key_order={"Reading": ("at", "station")},
        partition_by=DESIGN[dialect].get("partition_by", {}),
        indexes=tuple(DESIGN[dialect]["indexes"]),
    )
    keys = {"Reading": ["station", "at"]}
    creator = NativeStaging(NativeOperator(roles.operator, [roles.runtime]))
    creator.preflight(layout, keys)
    creator.create_table(
        entity="Reading", layout=layout, keys=keys, marker=creation_marker(PROJECT, stage, table)
    )
    creator.create_indexes(layout)
    assert roles.operator.validate_schema(layout, keys=keys) == ()
    found = _catalogue(roles, table)
    if dialect == "postgres":
        assert found["key"] == ["at", "station"]
        assert "USING brin (at)" in found["indexes"]["reading_at_brin"]
    else:
        assert found["key"] == "at, station" and found["partition"] == "toYYYYMM(at)"
        assert set(found["indexes"]) == {
            "reading_humidity",
            "reading_station",
            "reading_temperature",
        }


def _at() -> Any:
    import datetime as dt

    return dt.datetime(2026, 9, 23, 12, 0, 0, 123456, tzinfo=dt.UTC)


def test_the_partition_rule_still_holds_on_this_clickhouse() -> None:
    """The measurement behind `partition_by` must follow the key, repeated on this server.

    Two writes of one key, a different value each. Partitioned on the key they are one row after
    OPTIMIZE FINAL. Partitioned on a column outside the key - which the map format refuses, so the
    control table is created by hand - they stay two rows. If a ClickHouse release ever collapsed
    across partitions, this test is what would say the refusal had become unnecessary.
    """
    with runtime_roles("clickhouse") as ch:
        rows: dict[str, int] = {}
        for name, partition in (("on_key", "toYYYYMM(at)"), ("off_key", "humidity")):
            ch.command(
                f"CREATE TABLE {name} (station String, at DateTime64(6, 'UTC'), humidity Int32) "
                f"ENGINE = ReplacingMergeTree PARTITION BY {partition} ORDER BY (station, at)"
            )
            for humidity in (10, 20):
                ch.command(
                    f"INSERT INTO {name} VALUES ('s1', '2026-09-23 12:00:00.000000', {humidity})"
                )
            ch.command(f"OPTIMIZE TABLE {name} FINAL")
            rows[name] = int(ch.operator._cx.query(f"SELECT count() FROM {name}").result_rows[0][0])
        assert rows == {"on_key": 1, "off_key": 2}


def test_a_unique_or_unfinished_index_is_not_the_declared_one() -> None:
    """A leftover of an interrupted concurrent build is not the index a layout declares.

    Measured on 15.19: a failed ``CREATE INDEX CONCURRENTLY`` leaves its index in the catalogue,
    neither valid nor ready, and ``CREATE INDEX CONCURRENTLY IF NOT EXISTS`` succeeds over it with
    only a notice. The verification read the name, method and columns alone and reported such a
    leftover as the declared index; a unique index of the declared shape passed the same way,
    although it refuses writes the layout never refuses.
    """
    import psycopg

    with runtime_roles("postgres") as roles:
        model, placement = _placement(roles, design=True)
        sde.prepare_schema(model, placement, {"db": roles.operator}, project_id=PROJECT)
        layout = placement.groups["Reading"].source.layout
        keys = {"Reading": ["station", "at"]}
        cx = roles.operator._cx

        def found() -> dict[str, str]:
            return {
                finding.aspect: finding.found
                for finding in roles.operator.validate_schema(layout, keys=keys)
            }

        assert found() == {}  # the declared index, valid and not unique: no finding
        cx.execute("DROP INDEX reading_temperature")
        cx.execute("CREATE UNIQUE INDEX reading_temperature ON readings (temperature)")
        assert found() == {"index reading_temperature": "btree on ['temperature'], unique"}

        cx.execute("DROP INDEX reading_temperature")
        cx.execute(
            "INSERT INTO readings (station, at, humidity, temperature, __sde_write_epoch) "
            "VALUES ('a', now(), 1, 20.0, 1), ('b', now(), 1, 20.0, 1)"
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            cx.execute(
                "CREATE UNIQUE INDEX CONCURRENTLY reading_temperature ON readings (temperature)"
            )
        assert found() == {
            "index reading_temperature": (
                "btree on ['temperature'], unique, not valid (an unfinished concurrent build)"
            )
        }

        # Not unique, only not valid: the catalogue flag a cancelled build leaves, set directly.
        cx.execute("DROP INDEX CONCURRENTLY reading_temperature")
        cx.execute("CREATE INDEX reading_temperature ON readings (temperature)")
        cx.execute(
            "UPDATE pg_index SET indisvalid = false "
            "WHERE indexrelid = 'reading_temperature'::regclass"
        )
        assert found() == {
            "index reading_temperature": (
                "btree on ['temperature'], not valid (an unfinished concurrent build)"
            )
        }

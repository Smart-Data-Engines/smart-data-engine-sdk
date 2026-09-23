"""The physical design vocabulary: the parts no shared vector can reach.

The conformance vectors pin what a map means and what DDL it renders. What they cannot reach is the
other direction - reading a design back out of an engine's catalogue - and the renderer's behaviour
for a layout built by hand rather than loaded from a document, which is how the control plane and
the staging operator construct them.
"""

from __future__ import annotations

import pytest

import sde
from sde.errors import EngineError, MapError
from sde.physical import (
    CLICKHOUSE_METHODS,
    INDEX_METHODS,
    METHODS_BY_DIALECT,
    POSTGRES_METHODS,
    TEMPORAL_TYPES,
    PhysicalFinding,
    capabilities,
    declared_tables,
    parse_identifier_list,
    parse_partition_key,
    refuse_findings,
)

# --- reading the catalogue back ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("station, at", ("station", "at")),
        ("at", ("at",)),
        # ClickHouse 24.8 leaves reserved words bare when they match the bare pattern - measured.
        ("select, order", ("select", "order")),
        # ... and quotes `null` and anything outside the pattern, with `\` escaping `\` and `` ` ``.
        ("`null`, `ząb`", ("null", "ząb")),
        ("`a b`, c", ("a b", "c")),
        ("`tick\\`tock`", ("tick`tock",)),
        ("`back\\\\slash`", ("back\\slash",)),
        ("", ()),
    ],
)
def test_catalogue_names_are_parsed_not_predicted(text: str, names: tuple[str, ...]) -> None:
    assert parse_identifier_list(text) == names


@pytest.mark.parametrize(
    "text",
    [
        "`unterminated",
        "`dangling\\",
        "a,b",  # the catalogue writes ", ", and anything else is not evidence of a match
        "a, ",
        "a, , b",
        "toYYYYMM(at)",  # an expression is not a list of names
        "1abc",
    ],
)
def test_text_the_parser_does_not_understand_is_an_error(text: str) -> None:
    with pytest.raises(ValueError):
        parse_identifier_list(text)


def test_partition_keys_are_one_function_of_one_name() -> None:
    assert parse_partition_key("") is None
    assert parse_partition_key("toYYYYMM(at)") == ("toYYYYMM", "at")
    assert parse_partition_key("toDate(`posted day`)") == ("toDate", "posted day")
    for text in ("at", "toYYYYMM(toDate(at))", "toYYYYMM(at, 'UTC')", "toYYYYMM(a, b)"):
        with pytest.raises(ValueError):
            parse_partition_key(text)


# --- what a dialect is offered ----------------------------------------------------------------


def test_capabilities_are_derived_from_the_same_tables_the_renderer_reads() -> None:
    postgres, clickhouse = capabilities("postgres"), capabilities("clickhouse")
    assert set(postgres["index_methods"]) == set(POSTGRES_METHODS)
    assert set(clickhouse["index_methods"]) == set(CLICKHOUSE_METHODS)
    assert set(INDEX_METHODS) == set(POSTGRES_METHODS) | set(CLICKHOUSE_METHODS)
    assert set(METHODS_BY_DIALECT) == {"clickhouse", "postgres"}
    # Only ClickHouse partitions, and only on a key column of a zone-independent time type.
    assert postgres["partition_granularities"] == []
    assert clickhouse["partition_granularities"] == ["day", "month", "year"]
    assert TEMPORAL_TYPES == ("date", "timestamptz")
    assert clickhouse["index_methods"]["set"] == {
        "granularity": [1, 1024],
        "columns": 1,
        "max_rows": [1, 65536],
    }
    assert postgres["index_methods"]["brin"] == {}
    # An engine this library renders nothing for is offered nothing, rather than an empty design.
    assert capabilities("orderbook") == {"physical_design": False}
    assert capabilities("mysql") == {"physical_design": False}


# --- what a layout declares -------------------------------------------------------------------


def _layout(**design: object) -> sde.PhysicalLayout:
    return sde.PhysicalLayout(
        tables={"Reading": "reading", "Archive": "archive"},
        columns={
            "Reading": {"station": "String", "at": "DateTime64(6, 'UTC')", "t": "Float64"},
            "Archive": {"sensor": "String", "day": "Date32"},
        },
        **design,  # type: ignore[arg-type]
    )


def test_declared_tables_carry_the_physical_key_partition_and_indexes() -> None:
    layout = _layout(
        key_order={"Reading": ("at", "station")},
        partition_by={"Reading": {"field": "at", "granularity": "month"}},
        indexes=(
            {
                "entity": "Reading",
                "name": "z_t",
                "columns": ["t"],
                "method": "minmax",
                "granularity": 4,
            },
            {
                "entity": "Reading",
                "name": "a_station",
                "columns": ["station"],
                "method": "set",
                "granularity": 2,
                "max_rows": 100,
            },
        ),
    )
    declared = declared_tables(layout, {"Reading": ["station", "at"], "Archive": ["sensor", "day"]})
    assert [entry.table for entry in declared] == ["archive", "reading"]
    archive, reading = declared
    assert archive.key == ("sensor", "day") and archive.partition is None and archive.indexes == ()
    assert reading.key == ("at", "station")
    assert reading.partition == ("toYYYYMM", "at")
    assert [(index.name, index.type_full, index.granularity) for index in reading.indexes] == [
        ("a_station", "set(100)", 2),
        ("z_t", "minmax", 4),
    ]


def test_a_hand_built_layout_is_held_to_the_key_rules_by_the_renderer() -> None:
    """The control plane builds layouts in memory; the loader's model rules are repeated here."""
    keys = {"Reading": ["station", "at"], "Archive": ["sensor", "day"]}
    reordered = _layout(key_order={"Reading": ("at",)})
    with pytest.raises(EngineError, match="must be a permutation of the key"):
        sde.schema_statements(reordered, keys=keys, dialect="clickhouse")
    with pytest.raises(EngineError, match="must be a permutation of the key"):
        sde.schema_statements(reordered, keys=keys, dialect="postgres")
    off_key = _layout(partition_by={"Reading": {"field": "t", "granularity": "day"}})
    with pytest.raises(EngineError, match="outside the key"):
        sde.schema_statements(off_key, keys=keys, dialect="clickhouse")
    two_columns = _layout(
        indexes=(
            {
                "entity": "Reading",
                "name": "i",
                "columns": ["t", "station"],
                "method": "minmax",
                "granularity": 1,
            },
        )
    )
    with pytest.raises(EngineError, match="exactly one column"):
        sde.schema_statements(two_columns, keys=keys, dialect="clickhouse")


def test_a_map_without_the_model_still_refuses_a_non_permutation_when_rendered() -> None:
    """Loading without a model skips the model rules; rendering with keys does not."""
    document = {
        "contract": 5,
        "project_id": "1" * 32,
        "model_version": "0" * 16,
        "map_version": 1,
        "groups": {
            "Reading": {
                "write_epoch": 1,
                "source": {
                    "id": "r",
                    "engine": "ch",
                    "layout": {
                        "tables": {"Reading": "reading"},
                        "columns": {"Reading": {"station": "String", "at": "DateTime64(6, 'UTC')"}},
                        "key_order": {"Reading": ["at"]},
                    },
                },
            }
        },
    }
    placement = sde.load_map(document)  # no model: nothing to check a permutation against
    layout = placement.groups["Reading"].source.layout
    assert layout.key_order == {"Reading": ("at",)}
    with pytest.raises(EngineError, match="permutation"):
        sde.schema_statements(layout, keys={"Reading": ["station", "at"]}, dialect="clickhouse")


def test_a_loaded_design_is_frozen() -> None:
    document = {
        "contract": 5,
        "project_id": "1" * 32,
        "model_version": "0" * 16,
        "map_version": 1,
        "groups": {
            "Reading": {
                "write_epoch": 1,
                "source": {
                    "id": "r",
                    "engine": "ch",
                    "layout": {
                        "tables": {"Reading": "reading"},
                        "columns": {"Reading": {"station": "String", "at": "DateTime64(6, 'UTC')"}},
                        "key_order": {"Reading": ["at", "station"]},
                        "partition_by": {"Reading": {"field": "at", "granularity": "month"}},
                    },
                },
            }
        },
    }
    layout = sde.load_map(document).groups["Reading"].source.layout
    with pytest.raises(TypeError):
        layout.key_order["Reading"] = ("station",)  # type: ignore[index]
    with pytest.raises(TypeError):
        layout.partition_by["Reading"]["granularity"] = "day"  # type: ignore[index]


def test_new_keys_in_an_old_contract_are_refused_rather_than_ignored() -> None:
    base = {
        "contract": 4,
        "project_id": "1" * 32,
        "model_version": "0" * 16,
        "map_version": 1,
        "groups": {
            "Reading": {
                "write_epoch": 1,
                "source": {
                    "id": "r",
                    "engine": "pg",
                    "layout": {
                        "tables": {"Reading": "reading"},
                        "columns": {"Reading": {"station": "text"}},
                    },
                },
            }
        },
    }
    for extra, match in (
        ({"key_order": {"Reading": ["station"]}}, "key_order is map contract 5"),
        (
            {"partition_by": {"Reading": {"field": "station", "granularity": "day"}}},
            "Partitioning is map contract 5",
        ),
        (
            {
                "indexes": [
                    {"entity": "Reading", "name": "i", "columns": ["station"], "method": "btree"}
                ]
            },
            "which placement map contract 5",
        ),
    ):
        document = {**base, "groups": {"Reading": {**base["groups"]["Reading"]}}}
        source = dict(document["groups"]["Reading"]["source"])
        source["layout"] = {**source["layout"], **extra}
        document["groups"]["Reading"]["source"] = source
        with pytest.raises(MapError, match=match):
            sde.load_map(document)


# --- provisioning refuses, a session reports ----------------------------------------------------


def test_findings_are_a_refusal_only_where_a_person_can_act() -> None:
    refuse_findings((), EngineError)  # nothing to say, nothing raised
    finding = PhysicalFinding("reading", "sort key", "['at', 'station']", "('station', 'at')")
    assert str(finding) == (
        "reading: sort key is ('station', 'at') and the map declares ['at', 'station']"
    )
    with pytest.raises(EngineError, match="reading: sort key") as caught:
        refuse_findings((finding,), EngineError)
    assert "IF NOT EXISTS" in str(caught.value)


def test_a_loaded_layout_passed_back_as_a_document_loads_again() -> None:
    """The controller builds the next map from the one in force, and a loaded map is frozen.

    Its lists come back as tuples. Found by the controller's own suite on the day the structural
    index rules arrived: the second automatic index was refused because the first one's columns
    were a tuple, in a document that is valid JSON either way.
    """
    document = {
        "contract": 5,
        "project_id": "1" * 32,
        "model_version": "0" * 16,
        "map_version": 1,
        "groups": {
            "Reading": {
                "write_epoch": 1,
                "source": {
                    "id": "r",
                    "engine": "pg",
                    "layout": {
                        "tables": {"Reading": "reading"},
                        "columns": {"Reading": {"station": "text", "at": "timestamptz"}},
                        "key_order": {"Reading": ["at", "station"]},
                        "indexes": [
                            {
                                "entity": "Reading",
                                "name": "r_at",
                                "columns": ["at"],
                                "method": "brin",
                            }
                        ],
                    },
                },
            }
        },
    }
    layout = sde.load_map(document).groups["Reading"].source.layout
    assert isinstance(layout.indexes[0]["columns"], tuple)
    again = {**document, "map_version": 2}
    again["groups"] = {
        "Reading": {
            "write_epoch": 1,
            "source": {
                "id": "r",
                "engine": "pg",
                "layout": {
                    "tables": layout.tables,
                    "columns": layout.columns,
                    "key_order": layout.key_order,
                    "indexes": layout.indexes,
                },
            },
        }
    }
    reloaded = sde.load_map(again).groups["Reading"].source.layout
    assert reloaded.key_order == layout.key_order and reloaded.indexes == layout.indexes

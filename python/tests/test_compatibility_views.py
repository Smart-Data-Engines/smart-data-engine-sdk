"""Compatibility views: what can stand under a table's old name after a group has moved.

Requirement 19.7 of the product specification, and implementing it corrected the requirement's
premise. A SQL view cannot select from another server, and a migration always changes engine - so
a view in the *old* engine standing in for data that is now in the new one is not unbuilt, it is
not expressible. What is expressible is a view on the **target** under the old name, for somebody
who re-points their tool at the new engine and whose SQL still says the old table.

The two statements are measured against live servers in ``test_postgres_slice`` and
``test_clickhouse_slice`` rather than only asserted as text here, because this is exactly the class
of defect that reads correctly and does not run: PostgreSQL has no ``CREATE VIEW IF NOT EXISTS``,
and the first draft of the renderer emitted one.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from typing import Annotated

import pytest

import sde
from sde.errors import EngineError
from sde.schema import QUOTE, compatibility_views, schema_statements
from sde.testing.loader import model_from_neutral


def _model() -> sde.LogicalModel:
    sde.clear_registry()

    @sde.entity
    class Reading:
        station: uuid.UUID
        at: dt.datetime
        celsius: Annotated[decimal.Decimal, sde.precision(12, 2)]

        class Meta:
            key = ["station", "at"]

    return sde.build_model(Reading)


def _related_model() -> sde.LogicalModel:
    """One entity with a **relation**, which is the fixture the ordering test needs.

    A relation appends a foreign-key column to the layout *after* the declared fields, so the
    layout document is not in name order. That is the only condition under which "the view lists
    columns in the layout's order" and "the table declares them sorted" are different sentences -
    with no relation anywhere the two coincide, and an assertion about them holds for a renderer
    that does either. Which is why the renderer was wrong for months under a green test: a fixture
    that cannot reach the case reports on something else.
    """
    sde.clear_registry()

    @sde.entity
    class Station:
        id: uuid.UUID
        label: str

    @sde.entity
    class Reading:
        station: uuid.UUID
        at: dt.datetime
        celsius: Annotated[decimal.Decimal, sde.precision(12, 2)]
        origin: sde.Ref[Station]

        class Meta:
            key = ["station", "at"]
            atomic_with = ["Station"]

    return sde.build_model(Station, Reading)


def _layout(dialect: str) -> sde.PhysicalLayout:
    model = _model()
    (group,) = sde.colocation_groups(model)
    return sde.default_layout(model, group, dialect=dialect)


def test_a_table_both_engines_call_the_same_thing_gets_no_view_and_a_reason() -> None:
    """The case that fires for every postgres-to-clickhouse move, and it is not a gap.

    A layout's table name comes from the entity name and nothing else, so the two dialects agree
    on it. There is no old name for a view to occupy - the name is not what moved. Returning an
    empty tuple would leave a caller unable to tell "nothing needed" from "nothing thought about".
    """
    views = compatibility_views(
        _layout("clickhouse"), was={"Reading": "reading"}, dialect="clickhouse"
    )
    assert views.create == ()
    assert views.drop == ()
    assert not views.complete
    (entity, why) = views.not_possible[0]
    assert entity == "Reading"
    assert "both engines call this table 'reading'" in why


def test_the_clickhouse_reason_says_what_to_add_because_the_answer_is_wrong_without_it() -> None:
    """The hazard that costs a number rather than an error.

    The target is a ``ReplacingMergeTree``, so a hand-written query moved verbatim counts a row
    written twice under one key twice, until a background merge collapses it. Measured on a live
    server: two rows against one. The library's own reads use ``FINAL``; a hand-written one does
    not, and nothing fails.
    """
    (_, why) = compatibility_views(
        _layout("clickhouse"), was={"Reading": "reading"}, dialect="clickhouse"
    ).not_possible[0]
    assert "FINAL" in why
    assert "ReplacingMergeTree" in why
    assert "measured" in why

    (_, postgres_why) = compatibility_views(
        _layout("postgres"), was={"Reading": "reading"}, dialect="postgres"
    ).not_possible[0]
    assert "FINAL" not in postgres_why, "a PostgreSQL table needs no such warning"


def test_a_renamed_table_gets_a_view_under_the_old_name_and_a_drop_to_match() -> None:
    """The case a view is for, and both halves: 19.7 removes it with the source."""
    views = compatibility_views(
        _layout("postgres"), was={"Reading": "weather_reading"}, dialect="postgres"
    )
    assert views.complete
    assert views.not_possible == ()
    assert views.create == (
        'CREATE OR REPLACE VIEW "weather_reading" AS SELECT "at", "celsius", "station" '
        'FROM "reading"',
    )
    assert views.drop == ('DROP VIEW IF EXISTS "weather_reading"',)


def test_the_clickhouse_view_reads_final_and_uses_backticks() -> None:
    views = compatibility_views(
        _layout("clickhouse"), was={"Reading": "weather_reading"}, dialect="clickhouse"
    )
    assert views.create == (
        "CREATE VIEW IF NOT EXISTS `weather_reading` AS SELECT `at`, `celsius`, `station` "
        "FROM `reading` FINAL",
    )
    assert views.drop == ("DROP VIEW IF EXISTS `weather_reading`",)


def test_postgres_cannot_say_if_not_exists_about_a_view_and_clickhouse_can() -> None:
    """Idempotence is spelled differently, and getting it wrong is a syntax error rather than a
    subtle one - measured against both servers. `CREATE VIEW IF NOT EXISTS` is not PostgreSQL."""
    postgres = compatibility_views(
        _layout("postgres"), was={"Reading": "old"}, dialect="postgres"
    ).create[0]
    clickhouse = compatibility_views(
        _layout("clickhouse"), was={"Reading": "old"}, dialect="clickhouse"
    ).create[0]
    assert postgres.startswith("CREATE OR REPLACE VIEW")
    assert clickhouse.startswith("CREATE VIEW IF NOT EXISTS")


def test_columns_are_listed_rather_than_starred() -> None:
    """So the view names exactly what the map names.

    A ``SELECT *`` view changes shape when the table does, which is the opposite of the promise a
    compatibility view makes to a query written against the old shape.
    """
    create = compatibility_views(
        _layout("postgres"), was={"Reading": "old"}, dialect="postgres"
    ).create[0]
    assert "*" not in create
    for column in ("station", "at", "celsius"):
        assert f'"{column}"' in create


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
@pytest.mark.parametrize("related", [False, True], ids=["plain", "with-a-relation"])
def test_the_view_lists_columns_in_the_same_order_the_table_declares_them(
    dialect: str, related: bool
) -> None:
    """The property, and the fixture that can actually fail it.

    A view whose columns come out in a different order from the table's breaks anything reading
    them by position, which is most of what a hand-written ``SELECT *`` does - and a compatibility
    view exists precisely for hand-written SQL. So the statement worth asserting is that the two
    renderings **agree**.

    ``related=True`` is the case that matters. The earlier version of this test had one fixture,
    with no relations, and passed against a renderer that read the view's order off the layout
    document - which is *not* sorted once a relation appends a foreign-key column. The two orders
    coincided in the fixture and nowhere else. Found by writing the ``schema/`` vectors, where
    ``009`` feeds a deliberately reversed document and pins both statements.
    """
    model = _related_model() if related else _model()
    group = next(g for g in sde.colocation_groups(model) if "Reading" in g.members)
    layout = sde.default_layout(model, group, dialect=dialect)
    keys = {name: model.entity(name).key for name in group.members}
    was = {name: f"old_{name.lower()}" for name in group.members}
    quote = QUOTE[dialect]

    statements = schema_statements(layout, keys=keys, dialect=dialect)
    views = compatibility_views(layout, was=was, dialect=dialect)
    assert views.create, "no view was rendered, so this test would prove nothing"

    def order_in(statement: str, quoted: list[str]) -> list[str]:
        """The quoted column names in the order this statement mentions them.

        Read out of the statement rather than derived from the layout, because deriving both sides
        from one source is the tautology that let three mutations survive in the control plane. The
        quoted form is unambiguous as a substring: a closing quote follows the name, so 'order_id'
        does not match inside 'order_ident'. ClickHouse repeats key columns in ORDER BY and only
        the first occurrence is taken, which is the definition list.
        """
        return [name for _, name in sorted((statement.index(n), n) for n in quoted)]

    for entity, table in sorted(layout.tables.items()):
        quoted = [quote(column) for column in layout.columns[entity]]
        create = next(s for s in statements if f"EXISTS {quote(table)} (" in s)
        view = next((s for s in views.create if f" FROM {quote(table)}" in s), None)
        if view is None:
            continue
        assert order_in(view, quoted) == order_in(create, quoted), (
            f"the view on {table} lists its columns in a different order from the CREATE TABLE. A "
            f"query moved onto this view and reading columns by position gets different values, "
            f"and hand-written SQL is the only reason this view exists."
        )


def test_a_fixed_schema_engine_has_nowhere_to_put_one_and_says_so() -> None:
    """The one case where the name really does move, and the one that cannot have a view.

    A fixed-schema engine's table name is the engine's own, so a query naming the old one breaks -
    and that engine accepts no DDL from this library at all, so there is nothing to create. Both
    facts in one sentence, with both names in it, because the only remaining action is an edit.
    """
    sde.clear_registry()
    fixed = model_from_neutral(
        {
            "name": "m",
            "entities": [
                {
                    "name": "Depth",
                    "fields": [
                        {"name": name, "type": kind, "nullable": False}
                        for name, kind in sde.ORDERBOOK_SHAPE.items()
                    ],
                    "key": list(sde.ORDERBOOK_KEY),
                }
            ],
            "relations": [],
            "atomic": [],
        }
    )
    (group,) = sde.colocation_groups(fixed)
    layout = sde.default_layout(fixed, group, dialect="orderbook")
    views = compatibility_views(layout, was={"Depth": "depth"}, dialect="orderbook")
    assert views.create == ()
    assert views.drop == ()
    (entity, why) = views.not_possible[0]
    assert entity == "Depth"
    assert "accepts no DDL" in why
    assert "'orderbook'" in why, "the name the engine imposes"
    assert "'depth'" in why, "and the name a hand-written query says"


def test_an_entity_the_source_layout_does_not_mention_is_reported_not_guessed() -> None:
    views = compatibility_views(_layout("postgres"), was={}, dialect="postgres")
    assert views.create == ()
    (_, why) = views.not_possible[0]
    assert "no table for 'Reading'" in why


def test_an_unknown_dialect_is_refused() -> None:
    with pytest.raises(EngineError, match="no compatibility view for dialect"):
        compatibility_views(_layout("postgres"), was={"Reading": "old"}, dialect="mysql")

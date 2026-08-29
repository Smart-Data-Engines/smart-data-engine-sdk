"""One value, two engines, one answer - or the product's central promise is not true.

The promise is that we own the physical schema and can move a group of entities from one engine to
another while the application keeps running and does not notice. "Does not notice" has a precise
meaning that nothing else in this suite checks: **the value a client reads back must not depend on
which engine it came from** - not its content, and not its Python type.

Nothing was checking it, and two divergences were living there:

- a naive datetime went into PostgreSQL as 12:00 and into ClickHouse as 10:00, because the
  ClickHouse driver reads a naive value as local time. One call, two hours apart, no error.
- a `timestamptz` came back aware from psycopg and naive from clickhouse-connect, so the same field
  read from the two engines produced two datetimes Python refuses to compare.

Both are fixed in the adapter. This file is what stops them coming back, and what will catch the
next one - the third adapter has to pass it before it is called an adapter.

    SDE_POSTGRES_DSN=postgresql://postgres:sde@127.0.0.1:55432/sde \\
    SDE_CLICKHOUSE_DSN=clickhouse://default:sde@127.0.0.1:58123/sde pytest
"""

from __future__ import annotations

import datetime as dt
import decimal
import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from sde.engines.clickhouse import ClickHouseEngine
from sde.engines.postgres import PostgresEngine
from sde.layout import CLICKHOUSE_TYPES, POSTGRES_TYPES
from sde.placement import PhysicalLayout
from sde.types import NEUTRAL_TYPES

PG_DSN = os.environ.get("SDE_POSTGRES_DSN")
CH_DSN = os.environ.get("SDE_CLICKHOUSE_DSN")

pytestmark = pytest.mark.skipif(
    not (PG_DSN and CH_DSN),
    reason="needs both SDE_POSTGRES_DSN and SDE_CLICKHOUSE_DSN; the one test that cannot be "
    "written against a single engine, because agreement between two is the whole subject",
)

TABLE = "agreement"

# One value per neutral type both dialects map, chosen to break things rather than to pass:
# sub-second precision, an integer past 2**53, a decimal whose cents a float would lose, a string
# with combining characters and an em dash, a float that is not representable in binary.
VALUES: dict[str, Any] = {
    "bool": True,
    "int32": -2_147_483_648,
    "int64": 9_007_199_254_740_993,
    "float32": 0.5,
    "float64": 0.1,
    "string": "Zamówienie óść — ok",
    "uuid": uuid.uuid4(),
    "date": dt.date(2026, 8, 27),
    "timestamp": dt.datetime(2026, 8, 27, 12, 0, 0, 500000),
    "timestamptz": dt.datetime(2026, 8, 27, 12, 0, 0, 500000),
    "decimal(12,2)": decimal.Decimal("1234.56"),
}


def _covered() -> list[str]:
    """The neutral types both dialects map. Derived, never listed by hand.

    Listing them would make this test go stale silently: the day a dialect gains a mapping, the type
    would still be excluded and the divergence it introduces would go unchecked. Deriving the set
    from the type maps means adding a mapping *automatically* extends this test, and the assertion
    below turns "I added a mapping" into "I have to make it agree".
    """
    shared = set(POSTGRES_TYPES) & set(CLICKHOUSE_TYPES)
    return sorted(shared | {"decimal(12,2)"})


def test_every_shared_neutral_type_has_a_value_to_check() -> None:
    """The guard on the guard: a type nobody wrote a value for is silently untested."""
    missing = sorted(set(_covered()) - set(VALUES))
    assert not missing, (
        f"both dialects map {missing} and this file has no value for it, so agreement on that type "
        "is "
        f"not being checked. Add one - chosen to break things, not to pass."
    )


def test_the_types_left_out_are_left_out_for_a_written_reason() -> None:
    """`bytes` and `json` are unmapped in ClickHouse on purpose, and this pins that to a reason.

    If somebody adds either mapping, this test fails and points at the two problems that have to be
    solved first - which is better than the agreement test quietly starting to cover a type whose
    read path is known to be wrong.
    """
    unmapped = sorted(
        neutral for neutral in NEUTRAL_TYPES if neutral not in CLICKHOUSE_TYPES
    )
    assert unmapped == ["bytes", "json"], (
        f"the set of neutral types ClickHouse does not map has changed to {unmapped}. If a mapping "
        "was added: `bytes` needs the read path fixed first (a ClickHouse String column returns "
        "hex "
        f"text for bytes that are not valid UTF-8, and nothing tells a binary String from a "
        "text one on the way back), and `json` needs a decision about what the neutral type "
        "promises "
        "on return - PostgreSQL gives a dict, a ClickHouse String gives the original text. Both "
        "are "
        f"tasks. Neither is a mapping."
    )


@pytest.fixture
def engines() -> Iterator[tuple[PostgresEngine, ClickHouseEngine]]:
    assert PG_DSN and CH_DSN
    covered = _covered()
    pg_columns = {name: POSTGRES_TYPES.get(name, "numeric(12,2)") for name in covered}
    ch_columns = {name: CLICKHOUSE_TYPES.get(name, "Decimal(12, 2)") for name in covered}
    pg_columns["id"] = "uuid"
    ch_columns["id"] = "UUID"

    with (
        PostgresEngine(PG_DSN) as pg,
        ClickHouseEngine(CH_DSN) as ch,
        pg._cx.cursor() as cur,
    ):
        cur.execute(f'DROP TABLE IF EXISTS "{TABLE}"')
        ch._cx.command(f"DROP TABLE IF EXISTS `{TABLE}`")

        pg.ensure_schema(
            PhysicalLayout(tables={"A": TABLE}, columns={"A": pg_columns}), keys={"A": ["id"]}
        )
        ch.ensure_schema(
            PhysicalLayout(tables={"A": TABLE}, columns={"A": ch_columns}), keys={"A": ["id"]}
        )
        yield pg, ch


def test_the_same_value_read_from_either_engine_is_the_same_value(
    engines: tuple[PostgresEngine, ClickHouseEngine],
) -> None:
    """Field by field, content **and** Python type.

    The type matters as much as the content and is the half that gets forgotten: a client comparing
    a naive datetime to an aware one gets a TypeError, not a wrong answer, and a `Decimal` silently
    becoming a `float` loses cents without ever raising.
    """
    pg, ch = engines
    key = uuid.uuid4()
    row = {"id": key, **{name: VALUES[name] for name in _covered()}}

    pg.insert(TABLE, row)
    ch.insert(TABLE, row)

    from_pg = pg.get(TABLE, {"id": key})
    from_ch = ch.get(TABLE, {"id": key})
    assert from_pg is not None and from_ch is not None

    disagreements = []
    for name in _covered():
        left, right = from_pg[name], from_ch[name]
        if type(left) is not type(right):
            disagreements.append(
                f"{name}: PostgreSQL gives {type(left).__name__} and ClickHouse gives "
                f"{type(right).__name__}"
            )
        elif left != right:
            disagreements.append(f"{name}: {left!r} against {right!r}")

    assert not disagreements, (
        "the two engines disagree about a value this library wrote to both:\n  "
        + "\n  ".join(disagreements)
        + "\nThat is the promise 'we can move your group and you will not notice' failing, and it "
        "fails silently in a client's application rather than here."
    )


def test_a_naive_datetime_means_the_same_instant_in_both(
    engines: tuple[PostgresEngine, ClickHouseEngine],
) -> None:
    """Called out on its own, because it is the divergence that was actually there.

    Asserted against an absolute value rather than only against each other: two engines that agree
    on the wrong instant would pass the test above.
    """
    pg, ch = engines
    key = uuid.uuid4()
    noon = dt.datetime(2026, 8, 27, 12, 0, 0)
    row = {"id": key, **{name: VALUES[name] for name in _covered()}}
    row["timestamptz"] = noon
    row["timestamp"] = noon

    pg.insert(TABLE, row)
    ch.insert(TABLE, row)

    expected = noon.replace(tzinfo=dt.UTC)
    for engine, name in ((pg, "PostgreSQL"), (ch, "ClickHouse")):
        read = engine.get(TABLE, {"id": key})
        assert read is not None
        assert read["timestamptz"] == expected, f"{name} moved a naive datetime"
        assert read["timestamp"] == noon, f"{name} moved a value in a column with no timezone"

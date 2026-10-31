"""What a temporal column's spelling says of its zone (format contract 7a).

The same table is in the TypeScript and C++ libraries' tests: the vectors pin the refusals, and this
pins every spelling that must be read, in either direction, and every one that must not.
"""

from __future__ import annotations

import pytest

from sde.layout import column_zone

CASES = [
    ('clickhouse', "DateTime64(6, 'UTC')", True),
    ('clickhouse', "DateTime64(3,'Europe/Warsaw')", True),
    ('clickhouse', "DateTime64( 6 , 'UTC' )", True),
    ('clickhouse', "DateTime('UTC')", True),
    ('clickhouse', "Nullable(DateTime64(6, 'UTC'))", True),
    ('clickhouse', 'Nullable( DateTime64(6) )', False),
    ('clickhouse', 'DateTime64(6)', False),
    ('clickhouse', 'DateTime', False),
    ('clickhouse', 'DateTime64(6, UTC)', None),
    ('clickhouse', 'datetime64(6)', None),
    ('clickhouse', 'DateTime64()', None),
    ('clickhouse', 'DateTime64(6) ', None),
    ('clickhouse', 'Date', None),
    ('clickhouse', 'timestamptz', None),
    ('postgres', 'timestamptz', True),
    ('postgres', 'TIMESTAMPTZ(3)', True),
    ('postgres', 'timestamp with time zone', True),
    ('postgres', 'timestamp(6) with time zone', True),
    ('postgres', 'Timestamp (6)  With  Time  Zone', True),
    ('postgres', 'timestamp', False),
    ('postgres', 'timestamp(6)', False),
    ('postgres', 'timestamp without time zone', False),
    ('postgres', 'TIMESTAMP(6) WITHOUT TIME ZONE', False),
    ('postgres', 'timestamp ', None),
    ('postgres', 'timestamptz with time zone', None),
    ('postgres', 'timestamp(x)', None),
    ('postgres', 'date', None),
    ('postgres', 'DateTime64(6)', None),
    ('elasticsearch', 'long/timestamptz', True),
    ('elasticsearch', 'long/timestamp', False),
    ('elasticsearch', 'long', None),
    ('mysql', 'DateTime64(3)', None),
]


@pytest.mark.parametrize(("dialect", "spelling", "zoned"), CASES)
def test_a_spelling_says_whether_its_column_carries_a_zone(
    dialect: str, spelling: str, zoned: bool | None
) -> None:
    assert column_zone(spelling, dialect=dialect) is zoned

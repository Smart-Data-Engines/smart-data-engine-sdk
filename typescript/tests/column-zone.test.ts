/**
 * What a temporal column's spelling says of its zone (format contract 7a).
 *
 * The same table is in the Python and C++ libraries' tests: the vectors pin the refusals, and this
 * pins every spelling that must be read, in either direction, and every one that must not.
 */
import { expect, it } from 'vitest'
import { columnZone } from '../src/layout.js'

const CASES: readonly (readonly [string, string, boolean | null])[] = [
  ["clickhouse", "DateTime64(6, 'UTC')", true],
  ["clickhouse", "DateTime64(3,'Europe/Warsaw')", true],
  ["clickhouse", "DateTime64( 6 , 'UTC' )", true],
  ["clickhouse", "DateTime('UTC')", true],
  ["clickhouse", "Nullable(DateTime64(6, 'UTC'))", true],
  ["clickhouse", "Nullable( DateTime64(6) )", false],
  ["clickhouse", "DateTime64(6)", false],
  ["clickhouse", "DateTime", false],
  ["clickhouse", "DateTime64(6, UTC)", null],
  ["clickhouse", "datetime64(6)", null],
  ["clickhouse", "DateTime64()", null],
  ["clickhouse", "DateTime64(6) ", null],
  ["clickhouse", "Date", null],
  ["clickhouse", "timestamptz", null],
  ["postgres", "timestamptz", true],
  ["postgres", "TIMESTAMPTZ(3)", true],
  ["postgres", "timestamp with time zone", true],
  ["postgres", "timestamp(6) with time zone", true],
  ["postgres", "Timestamp (6)  With  Time  Zone", true],
  ["postgres", "timestamp", false],
  ["postgres", "timestamp(6)", false],
  ["postgres", "timestamp without time zone", false],
  ["postgres", "TIMESTAMP(6) WITHOUT TIME ZONE", false],
  ["postgres", "timestamp ", null],
  ["postgres", "timestamptz with time zone", null],
  ["postgres", "timestamp(x)", null],
  ["postgres", "date", null],
  ["postgres", "DateTime64(6)", null],
  ["elasticsearch", "long/timestamptz", true],
  ["elasticsearch", "long/timestamp", false],
  ["elasticsearch", "long", null],
  ["mysql", "DateTime64(3)", null],
]

it.each(CASES)('%s %s carries a zone: %s', (dialect, spelling, zoned) => {
  expect(columnZone(spelling, dialect)).toBe(zoned)
})

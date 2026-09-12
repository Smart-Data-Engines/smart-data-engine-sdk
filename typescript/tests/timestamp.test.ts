import { describe, expect, it } from 'vitest'
import { Timestamp } from '../src/index.js'
import { literal } from '../src/engines/clickhouse.js'

describe('exact microsecond timestamps', () => {
  it.each([
    ['2026-09-12 09:30:15.123456', '2026-09-12T09:30:15.123456Z'],
    ['2026-09-12T11:30:15.123456+02', '2026-09-12T09:30:15.123456Z'],
    ['2026-09-12T04:00:15.123456-0530', '2026-09-12T09:30:15.123456Z'],
    ['1900-01-01 05:30:20.000001+05:30:20', '1900-01-01T00:00:00.000001Z'],
    ['1969-12-31T23:59:59.999999Z', '1969-12-31T23:59:59.999999Z'],
    ['0001-01-01T00:00:00Z', '0001-01-01T00:00:00.000000Z'],
    ['9999-12-31T23:59:59.999999Z', '9999-12-31T23:59:59.999999Z'],
    ['2024-02-29T23:59:59.1Z', '2024-02-29T23:59:59.100000Z'],
  ])('normalises %s without losing a digit', (input, expected) => {
    const value = Timestamp.from(input)
    expect(value.toISOString()).toBe(expected)
    expect(Timestamp.fromEpochMicroseconds(value.epochMicroseconds).toISOString()).toBe(expected)
    expect(JSON.parse(JSON.stringify({ value }))).toEqual({ value: expected })
  })

  it.each([
    '2026-02-29T00:00:00Z', '2026-04-31T00:00:00Z', '2026-00-01T00:00:00Z',
    '2026-01-00T00:00:00Z', '2026-01-01T24:00:00Z', '2026-01-01T00:60:00Z',
    '2026-01-01T00:00:60Z', '2026-01-01T00:00:00.1234567Z',
    '2026-01-01T00:00:00+24', '2026-01-01T00:00:00+01:60',
    '0000-01-01T00:00:00Z', 'infinity', '-infinity', 'not a timestamp',
  ])('refuses %s rather than normalising invalid input', (input) => {
    expect(() => Timestamp.from(input)).toThrow(RangeError)
  })

  it('uses floor division immediately before the epoch', () => {
    expect(Timestamp.fromEpochMicroseconds(-1n).toISOString()).toBe('1969-12-31T23:59:59.999999Z')
    expect(Timestamp.from('1969-12-31T23:59:59.999999Z').epochMicroseconds).toBe(-1n)
  })

  it('accepts Date inputs, but refuses a lossy conversion back to Date', () => {
    const date = new Date('2026-09-12T09:30:15.123Z')
    expect(Timestamp.from(date).toDate()).toEqual(date)
    expect(() => Timestamp.from('2026-09-12T09:30:15.123456Z').toDate()).toThrow('cannot represent')
    expect(() => Timestamp.from(new Date(Number.NaN))).toThrow('invalid Date')
  })

  it('is immutable and requires explicit exact comparison', () => {
    const value = Timestamp.fromEpochMicroseconds(1n)
    expect(Object.isFrozen(value)).toBe(true)
    expect(Timestamp.from(value)).toBe(value)
    expect(() => Number(value)).toThrow('compare Timestamp.epochMicroseconds')
    expect(() => Timestamp.fromEpochMicroseconds(253_402_300_800_000_000n)).toThrow(RangeError)
  })

  it('renders a timestamp key with six digits in ClickHouse predicates', () => {
    expect(literal(Timestamp.from('2026-09-12T09:30:15.123456Z')))
      .toBe("'2026-09-12 09:30:15.123456'")
  })
})

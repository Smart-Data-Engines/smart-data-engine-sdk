/**
 * The days and instants an engine holds, read from the engine (format contract section 8b).
 *
 * The contract admits a date and a timestamp from year 0001 to 9999 in UTC. ClickHouse 24.8 holds
 * `Date32` and `DateTime64` from 1900-01-01 to 2299-12-31 only, and moves a value outside to the
 * nearer end without an error: measured on 1 November 2026, 1800-01-01 was stored as 1900-01-01
 * and 2500-06-01 12:00 as 2299-12-31 12:00, and the server's `date_time_overflow_behavior='throw'`
 * did not apply to an insert. ClickHouse 26.9 holds the contract's whole range. So the range is the
 * server's, and it is read from the server: a conversion moves a value as an insert does, so
 * converting the contract's ends gives the server's (`RANGE_SQL`).
 *
 * A session refuses a row with a value outside the range of an engine its write goes to, before
 * any engine is called, and a migration refuses to copy one. An engine that holds the contract's
 * whole range has no range here (`null`).
 */
import { Timestamp } from './timestamp.js'

/** ClickHouse's own ends: the contract's, moved as an insert would move them. */
export const RANGE_SQL =
  "SELECT toString(toDate32('0001-01-01')) AS first_day, toString(toDate32('9999-12-31')) AS last_day, " +
  "toString(toDateTime64('0001-01-01 00:00:00', 6, 'UTC')) AS first_instant, " +
  "toString(toDateTime64('9999-12-31 23:59:59.999999', 6, 'UTC')) AS last_instant"

export const TEMPORAL_KINDS: ReadonlySet<string> = new Set(['date', 'timestamp', 'timestamptz'])

/** The first and last day and instant an engine holds. */
export class TemporalRange {
  constructor(
    readonly firstDay: string,
    readonly lastDay: string,
    readonly firstInstant: Timestamp,
    readonly lastInstant: Timestamp,
  ) {}

  /**
   * Whether a value of a field of `kind` is inside the range. A value reaches here in the form a
   * filter gets (section 8b): a date as `YYYY-MM-DD` text, a timestamp as a `Timestamp`. A copy's
   * row may hold either as text, which is read the same way.
   */
  holds(kind: string, value: unknown): boolean {
    if (kind === 'date') {
      const day = String(value)
      return this.firstDay <= day && day <= this.lastDay
    }
    const instant = (value instanceof Timestamp ? value : Timestamp.from(String(value))).epochMicroseconds
    return this.firstInstant.epochMicroseconds <= instant && instant <= this.lastInstant.epochMicroseconds
  }

  /** The range as a refusal says it: by its days, a timestamp's as well. */
  describe(): string {
    return `from ${this.firstDay} to ${this.lastDay}`
  }
}

export function kindNoun(kind: string): string {
  return kind === 'date' ? 'a date' : 'a timestamp'
}

/** The range from `RANGE_SQL`'s four answers; `null` for the contract's whole one. */
export function fromEngineText(firstDay: string, lastDay: string, first: string, last: string): TemporalRange | null {
  const range = new TemporalRange(firstDay, lastDay, Timestamp.from(first), Timestamp.from(last))
  const whole =
    firstDay === '0001-01-01' && lastDay === '9999-12-31' &&
    range.firstInstant.epochMicroseconds === Timestamp.from('0001-01-01T00:00:00Z').epochMicroseconds &&
    range.lastInstant.epochMicroseconds === Timestamp.from('9999-12-31T23:59:59.999999Z').epochMicroseconds
  return whole ? null : range
}

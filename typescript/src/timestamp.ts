/** An immutable UTC instant with the microsecond precision of the SDE timestamp types.
 *
 * JavaScript Date loses the last three digits before a migration can compare them. Both engine
 * adapters therefore return Timestamp, and accept it on writes and in keys. Date remains a valid
 * input for applications whose values really are millisecond-resolution. See docs/timestamps.md.
 */
export class Timestamp {
  private constructor(readonly epochMicroseconds: bigint) {
    Object.freeze(this)
  }

  static fromEpochMicroseconds(value: bigint): Timestamp {
    if (typeof value !== 'bigint') throw new TypeError('epochMicroseconds must be a bigint')
    // Years 0001 through 9999, the common finite range of the host representations. Each engine
    // may impose a narrower range; its write error remains authoritative.
    if (value < -62_135_596_800_000_000n || value > 253_402_300_799_999_999n) {
      throw new RangeError('Timestamp must be within UTC years 0001 through 9999')
    }
    return new Timestamp(value)
  }

  /** ISO date-time or engine text; an absent offset means UTC, never the process timezone. */
  static from(value: string | Date | Timestamp): Timestamp {
    if (value instanceof Timestamp) return value
    if (value instanceof Date) {
      if (!Number.isFinite(value.getTime())) throw new RangeError('invalid Date for Timestamp')
      return Timestamp.fromEpochMicroseconds(BigInt(value.getTime()) * 1000n)
    }
    if (typeof value !== 'string') throw new TypeError('Timestamp requires a string or Date')
    const parts = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(?:[Zz]|([+-])(\d{2})(?::?(\d{2}))?(?::?(\d{2}))?)?$/.exec(value)
    if (parts === null) {
      throw new RangeError('Timestamp requires a finite ISO date-time with at most six fractional digits')
    }
    const year = Number(parts[1])
    const month = Number(parts[2])
    const day = Number(parts[3])
    const hour = Number(parts[4])
    const minute = Number(parts[5])
    const second = Number(parts[6])
    const date = new Date(0)
    date.setUTCFullYear(year, month - 1, day)
    date.setUTCHours(hour, minute, second, 0)
    if (
      year < 1 || date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 ||
      date.getUTCDate() !== day || date.getUTCHours() !== hour ||
      date.getUTCMinutes() !== minute || date.getUTCSeconds() !== second
    ) {
      throw new RangeError('invalid calendar date or time for Timestamp')
    }
    const offsetHour = Number(parts[9] ?? 0)
    const offsetMinute = Number(parts[10] ?? 0)
    const offsetSecond = Number(parts[11] ?? 0)
    if (offsetHour > 23 || offsetMinute > 59 || offsetSecond > 59) {
      throw new RangeError('invalid UTC offset for Timestamp')
    }
    const offset = (offsetHour * 3600 + offsetMinute * 60 + offsetSecond) *
      (parts[8] === '-' ? -1 : 1)
    const fraction = BigInt((parts[7] ?? '').padEnd(6, '0'))
    return Timestamp.fromEpochMicroseconds(
      BigInt(date.getTime()) * 1000n + fraction - BigInt(offset) * 1_000_000n,
    )
  }

  toISOString(): string {
    // Floor, not truncation towards zero: the microsecond immediately before the epoch belongs
    // to the previous millisecond, with a positive fractional remainder.
    const remainder = ((this.epochMicroseconds % 1000n) + 1000n) % 1000n
    const milliseconds = (this.epochMicroseconds - remainder) / 1000n
    const iso = new Date(Number(milliseconds)).toISOString()
    return `${iso.slice(0, -1)}${remainder.toString().padStart(3, '0')}Z`
  }

  toString(): string {
    return this.toISOString()
  }

  toJSON(): string {
    return this.toISOString()
  }

  /** Convert only when Date can represent the instant exactly. No implicit rounding. */
  toDate(): Date {
    if (this.epochMicroseconds % 1000n !== 0n) {
      throw new RangeError(
        'Timestamp has microseconds that Date cannot represent; use epochMicroseconds or toISOString()',
      )
    }
    return new Date(Number(this.epochMicroseconds / 1000n))
  }

  /** Ordering must name the exact quantity rather than coerce it to a lossy number. */
  valueOf(): never {
    throw new TypeError('compare Timestamp.epochMicroseconds explicitly')
  }
}

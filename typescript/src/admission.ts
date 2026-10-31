/**
 * What a row may give a field of each neutral type, and the form the value reaches an engine in.
 *
 * Format contract section 8b, point 4. Until 7 October 2026 a row's values went to the adapter as
 * the application gave them, and each driver and each engine converted them in its own way.
 * Measured through `Session.save` on PostgreSQL 15 and ClickHouse 24.8, in this library and in the
 * reference:
 * - an integer outside int32 wrapped to the opposite sign in ClickHouse;
 * - `NaN` reached a decimal column as `0.00`;
 * - a date that does not exist moved two days;
 * - a number became a timestamp;
 * - a decimal with more fractional digits than its column was rounded by PostgreSQL and truncated
 *   by ClickHouse.
 *
 * The same model meant something different per engine, and moving a group changed what.
 *
 * A value is admitted in the forms a filter of the same type takes (`queryValue`), so a row and a
 * read agree about what a value is. It is passed on in the form a filter gets, so every adapter
 * receives one representation per type. A refusal says what the type does not hold and never the
 * value: a refusal is logged, and the value is the client's.
 */
import { decimalParts, QueryRefused, queryValue } from './query.js'

/** Returns the value as engines receive it, or throws `Misfit`. */
export type Check = (value: unknown) => unknown

/** What a field's type does not hold about a value, in words that never include it. */
export class Misfit extends Error {
  override readonly name = 'Misfit'
}

/** The types whose rule is a filter value's rule exactly, and what a refusal calls them. */
const NOUNS: Readonly<Record<string, string>> = {
  bool: 'a boolean',
  string: 'text',
  uuid: 'a UUID',
  date: 'a date',
  timestamp: 'a timestamp',
  timestamptz: 'a timestamp',
  bytes: 'bytes',
}

function asFilter(kind: string): Check {
  const column = { name: '', type: kind }
  const noun = NOUNS[kind]!
  return (value) => {
    try {
      return queryValue(column, value)
    } catch (error) {
      if (error instanceof QueryRefused) throw new Misfit(`a value that is not ${noun}`)
      throw error
    }
  }
}

function integer(kind: 'int32' | 'int64'): Check {
  const bound = kind === 'int32' ? 2n ** 31n : 2n ** 63n
  return (value) => {
    if (typeof value === 'number') {
      if (!Number.isSafeInteger(value)) throw new Misfit('a value that is not an integer')
      // Every safe integer is inside int64; only int32 has to look.
      if (kind === 'int32' && (value < -(2 ** 31) || value >= 2 ** 31)) throw new Misfit(`an integer outside ${kind}`)
      return value
    }
    if (typeof value !== 'bigint') throw new Misfit('a value that is not an integer')
    if (value < -bound || value >= bound) throw new Misfit(`an integer outside ${kind}`)
    return value
  }
}

function float(kind: 'float32' | 'float64'): Check {
  const single = kind === 'float32'
  return (value) => {
    if (typeof value !== 'number') throw new Misfit('a value that is not a number')
    // NaN and the infinities are values both float types hold, and both engines store them. What
    // float32 cannot hold is a finite number it rounds to an infinity, or one that is not zero and
    // rounds to zero: PostgreSQL refuses both for `real`, and ClickHouse stores the infinity or the
    // zero.
    if (single && value !== 0 && Number.isFinite(value)) {
      const rounded = Math.fround(value)
      if (rounded === 0 || !Number.isFinite(rounded)) throw new Misfit('a number outside float32')
    }
    return value
  }
}

/**
 * The reference's limits on decimal text: past them its `Decimal()` refuses the text as invalid,
 * so this library refuses it as not a decimal rather than as too large, and the two agree.
 */
const LARGEST_ADJUSTED = 999999999999999999n
const SMALLEST_EXPONENT = -1999999999999999997n

function decimal(kind: string): Check {
  const [precision, scale] = kind.slice('decimal('.length, -1).split(',').map(Number) as [number, number]
  const places = BigInt(scale)
  const whole = BigInt(precision - scale)
  return (value) => {
    let text: string
    if (typeof value === 'string') text = value
    else if (typeof value === 'bigint' || (typeof value === 'number' && Number.isSafeInteger(value))) text = String(value)
    else throw new Misfit('a value that is not an exact decimal')
    const parts = decimalParts(text)
    if (parts === null) throw new Misfit('a value that is not an exact decimal')
    const fraction = parts[3] ?? ''
    const digits = (parts[2]! + fraction).replace(/^0+/, '')
    let exponent = BigInt(parts[4] ?? '0') - BigInt(fraction.length)
    if (exponent + BigInt(Math.max(digits.length, 1)) - 1n > LARGEST_ADJUSTED || exponent < SMALLEST_EXPONENT) {
      throw new Misfit('a value that is not an exact decimal')
    }
    let significant = '0'
    if (digits !== '') {
      // Trailing zeros say nothing about the value: 1.230 fits two fractional digits.
      significant = digits.replace(/0+$/, '')
      exponent += BigInt(digits.length - significant.length)
      if (-exponent > places) throw new Misfit(`more than ${scale} fractional digits`)
      if (BigInt(significant.length) + exponent > whole) throw new Misfit(`more than ${precision - scale} integer digits`)
    } else {
      exponent = 0n
    }
    return typeof value === 'string' ? atScale(parts[1] === '-' && digits !== '', significant, exponent, scale) : value
  }
}

/**
 * `digits * 10**exponent` in plain notation with exactly `scale` fractional digits.
 *
 * The text a decimal reaches an engine in, the same in every library: `1.230` and `1.23` and
 * `123E-2` all become `1.23` in a `decimal(12,2)`, and `1E+3` becomes `1000.00`. The caller has
 * checked that the value fits, so no digit is dropped and the text has at most the field's
 * precision in digits.
 */
export function atScale(negative: boolean, digits: string, exponent: bigint, scale: number): string {
  const shifted = (digits + '0'.repeat(Number(exponent + BigInt(scale)))).padStart(scale + 1, '0')
  const text = scale === 0 ? shifted : shifted.slice(0, -scale) + '.' + shifted.slice(-scale)
  return negative ? '-' + text : text
}

/** The check for a field of this neutral type, or undefined where every value is admitted. */
export function checkFor(kind: string): Check | undefined {
  if (Object.hasOwn(NOUNS, kind)) return asFilter(kind)
  if (kind === 'int32' || kind === 'int64') return integer(kind)
  if (kind === 'float32' || kind === 'float64') return float(kind)
  if (kind.startsWith('decimal(')) return decimal(kind)
  // json: whatever the adapter can serialise. The section 8b rule does not reach it yet.
  return undefined
}

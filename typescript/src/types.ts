/**
 * The neutral type vocabulary, and how a TypeScript declaration reaches it.
 *
 * Here is where TypeScript differs from Python in the way that matters, and why it was worth writing
 * second. Python can read a class's annotations at runtime and derive the model from them. TypeScript
 * cannot: its types are erased before the code runs. So the model has to be *stated*, as values, and
 * there is no possibility of inferring it from the shape of a class.
 *
 * That is a better test of the contract than a similar language would have been. Anything the format
 * contract left implicit - anything that was really "what Python's introspection happens to produce"
 * - has nowhere to hide here, because nothing is introspected.
 *
 * The vocabulary itself is fixed and shared:
 *
 *     bool  int32  int64  float32  float64  decimal(p,s)
 *     string  bytes  uuid  date  timestamp  timestamptz  json
 *
 * `decimal` is written `decimal(12,2)` - precision, comma, scale, no spaces, both required. A decimal
 * without precision is not a storable type in any engine we place data in, and letting the engine
 * choose would make the physical schema depend on something the model never said.
 */

import { DeclarationError } from './errors.js'

export const NEUTRAL_TYPES = [
  'bool',
  'int32',
  'int64',
  'float32',
  'float64',
  'string',
  'bytes',
  'uuid',
  'date',
  'timestamp',
  'timestamptz',
  'json',
] as const

export type NeutralType = (typeof NEUTRAL_TYPES)[number] | `decimal(${number},${number})`

/** A field: its neutral type, and whether it may be null. */
export interface FieldType {
  readonly type: NeutralType
  readonly nullable: boolean
}

function simple(type: NeutralType): FieldType & { nullable: false } {
  return { type, nullable: false }
}

/**
 * The vocabulary as values.
 *
 * Note what is absent: any mapping from a TypeScript type. `T.timestamptz` is not "the neutral form
 * of `Date`" - a `Date` has no zone information to inspect and `number` could be any of five things.
 * The declaration says which one it is, because only the author knows.
 */
export const T = {
  bool: simple('bool'),
  int32: simple('int32'),
  int64: simple('int64'),
  float32: simple('float32'),
  float64: simple('float64'),
  string: simple('string'),
  bytes: simple('bytes'),
  uuid: simple('uuid'),
  date: simple('date'),
  timestamp: simple('timestamp'),
  timestamptz: simple('timestamptz'),
  json: simple('json'),

  /** `T.decimal(12, 2)`. Both arguments are required, for the reason in this module's docstring. */
  decimal(digits: number, scale: number): FieldType {
    if (!Number.isInteger(digits) || !Number.isInteger(scale)) {
      throw new DeclarationError(
        `decimal(${digits}, ${scale}) needs whole numbers: a fractional precision is not a thing`,
      )
    }
    if (digits < 1 || scale < 0 || scale > digits) {
      throw new DeclarationError(
        `decimal(${digits}, ${scale}) is not a usable decimal: digits must be at least 1 and scale ` +
          'must be between 0 and digits',
      )
    }
    return { type: `decimal(${digits},${scale})`, nullable: false }
  },

  /** Make any of the above nullable: `T.nullable(T.string)`. */
  nullable(inner: FieldType): FieldType {
    return { type: inner.type, nullable: true }
  },
} as const

const DECIMAL = /^decimal\((\d+),(\d+)\)$/

/** Validate a type name that came from outside - a vector, or a hand-written declaration. */
export function checkType(name: string, where: string): NeutralType {
  if ((NEUTRAL_TYPES as readonly string[]).includes(name)) {
    return name as NeutralType
  }
  if (name.startsWith('decimal')) {
    const match = DECIMAL.exec(name)
    if (match) {
      const digits = Number(match[1])
      const scale = Number(match[2])
      if (digits >= 1 && scale >= 0 && scale <= digits) {
        return name as NeutralType
      }
    }
    throw new DeclarationError(
      `${where}: ${JSON.stringify(name)} is not a well-formed decimal. The written form is ` +
        'decimal(digits,scale) - precision then scale, no spaces, both required. No spaces because ' +
        'whitespace inside a type name is exactly the sort of thing two libraries would disagree ' +
        'about, and both required because a decimal without precision is not a storable type in any ' +
        'engine we place data in.',
    )
  }
  throw new DeclarationError(
    `${where}: ${JSON.stringify(name)} is not in the neutral type vocabulary ` +
      `(${[...NEUTRAL_TYPES].sort().join(', ')}, decimal(p,s))`,
  )
}

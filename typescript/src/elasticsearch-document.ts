/**
 * A row as an Elasticsearch document: its `_id` and its `_source` (format contract 7m).
 *
 * The same function as `sde.engines.elasticsearch.document`, held to the same `documents/` vectors,
 * because two libraries writing one row to one index have to produce one `_id` - or a second save
 * is a second document - and one `_source`, or a reader gets back another value than was written.
 *
 * A value backed by a `long` - an int64, an instant in microseconds, a decimal times 10^scale - is
 * a `bigint` here, never a `number`: past 2^53 a `number` is not the value any more.
 */

import { createHash } from 'node:crypto'

import { EngineError } from './errors.js'
import { TOKENS_FIELD } from './schema.js'
import { tokens } from './search.js'
import { Timestamp } from './timestamp.js'

/** The longest text a keyword holds, in UTF-8 bytes: Lucene's limit on one term. */
export const KEYWORD_BYTES = 32_766

export type StoredValue = boolean | number | bigint | string | null

const UUID_TEXT = /^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$/
const DATE_TEXT = /^(\d{4})-(\d{2})-(\d{2})$/
const DECIMAL_TYPE = /^long\/decimal\((\d+),(\d+)\)$/
const DECIMAL_TEXT = /^([+-]?)(\d+)(?:\.(\d+))?$/

function integer(value: unknown, bits: bigint): bigint {
  if (typeof value !== 'bigint' && !(typeof value === 'number' && Number.isSafeInteger(value))) {
    throw new EngineError('an integer field takes a bigint or a safe integer')
  }
  const exact = BigInt(value)
  if (exact < -(2n ** (bits - 1n)) || exact >= 2n ** (bits - 1n)) {
    throw new EngineError(`the value is outside int${bits}`)
  }
  return exact
}

function scaled(value: unknown, digits: number, scale: number): bigint {
  const text = typeof value === 'bigint' || (typeof value === 'number' && Number.isSafeInteger(value))
    ? String(value)
    : value
  if (typeof text !== 'string') {
    throw new EngineError('a decimal field takes decimal text or an integer, never a float')
  }
  const parts = DECIMAL_TEXT.exec(text.trim())
  if (parts === null) throw new EngineError('a decimal field takes a finite decimal')
  const fraction = parts[3] ?? ''
  const kept = fraction.slice(0, scale)
  if (/[1-9]/.test(fraction.slice(scale))) {
    throw new EngineError(`the value has more than ${scale} fractional digits`)
  }
  const exact = BigInt((parts[2] as string) + kept.padEnd(scale, '0')) * (parts[1] === '-' ? -1n : 1n)
  if ((exact < 0n ? -exact : exact) >= 10n ** BigInt(digits)) {
    throw new EngineError(`the value has more than ${digits} digits`)
  }
  return exact
}

function micros(value: unknown): bigint {
  if (!(value instanceof Timestamp || value instanceof Date || typeof value === 'string')) {
    throw new EngineError('a timestamp field takes a Timestamp, a Date or ISO text')
  }
  try {
    return Timestamp.from(value).epochMicroseconds
  } catch {
    throw new EngineError('a timestamp field takes a Timestamp, a Date or ISO text')
  }
}

/** The JSON value a column of `kind` keeps for `value`, exactly, or a refusal. */
export function stored(kind: string, value: unknown): StoredValue {
  if (value === null || value === undefined) return null
  switch (kind) {
    case 'boolean':
      if (typeof value !== 'boolean') throw new EngineError('a boolean field takes a bool')
      return value
    case 'integer':
      return Number(integer(value, 32n))
    case 'long':
      return integer(value, 64n)
    case 'float':
    case 'double':
      if (typeof value !== 'number') throw new EngineError('a float field takes a number')
      if (!Number.isFinite(value)) throw new EngineError('Elasticsearch stores no NaN or infinity')
      return value
    case 'keyword':
      if (typeof value !== 'string') throw new EngineError('a text field takes a string')
      if (Buffer.byteLength(value, 'utf8') > KEYWORD_BYTES) {
        throw new EngineError(
          `a text value longer than ${KEYWORD_BYTES} UTF-8 bytes is one term Elasticsearch ` +
            'cannot index; refused before it is sent',
        )
      }
      return value
    case 'keyword/uuid':
      if (typeof value !== 'string' || !UUID_TEXT.test(value)) {
        throw new EngineError('a uuid field takes canonical uuid text')
      }
      return value.toLowerCase()
    case 'date': {
      const parts = typeof value === 'string' ? DATE_TEXT.exec(value) : null
      const day = parts === null ? null : new Date(0)
      if (parts !== null && day !== null) {
        day.setUTCFullYear(Number(parts[1]), Number(parts[2]) - 1, Number(parts[3]))
      }
      if (parts === null || day === null || Number(parts[1]) < 1 ||
        day.getUTCFullYear() !== Number(parts[1]) || day.getUTCMonth() !== Number(parts[2]) - 1 ||
        day.getUTCDate() !== Number(parts[3])) {
        throw new EngineError('a date field takes YYYY-MM-DD text')
      }
      return value as string
    }
    case 'long/timestamp':
    case 'long/timestamptz':
      return micros(value)
  }
  const decimal = DECIMAL_TYPE.exec(kind)
  if (decimal !== null) return scaled(value, Number(decimal[1]), Number(decimal[2]))
  throw new EngineError(`'${kind}' is not a column type this adapter writes`)
}

/**
 * The `_id` of a document, from its key values as stored: each a tag, its length as four
 * big-endian bytes and its bytes - `s` and the UTF-8 of a text exactly as written, `i` and the
 * decimal digits of an integer, `b` and `true` or `false` - then SHA-256, in lowercase hex.
 * Not the canonical encoding, which normalises text: two keys that differ only in how an accent is
 * composed are two rows in every other engine, and are two documents here.
 */
export function documentId(keyValues: readonly StoredValue[]): string {
  const digest = createHash('sha256')
  for (const value of keyValues) {
    let tag: string, payload: Buffer
    if (typeof value === 'boolean') [tag, payload] = ['b', Buffer.from(value ? 'true' : 'false')]
    else if (typeof value === 'bigint' || (typeof value === 'number' && Number.isInteger(value))) {
      [tag, payload] = ['i', Buffer.from(BigInt(value).toString(), 'ascii')]
    } else if (typeof value === 'string') [tag, payload] = ['s', Buffer.from(value, 'utf8')]
    else throw new EngineError(`a key value of type ${typeof value} cannot address a document`)
    const length = Buffer.alloc(4)
    length.writeUInt32BE(payload.length)
    digest.update(Buffer.concat([Buffer.from(tag, 'ascii'), length, payload]))
  }
  return digest.digest('hex')
}

/** What a text field contributes to its token field: distinct tokens, in code point order. */
export function searchTokens(text: unknown): string[] {
  if (typeof text !== 'string') return []
  return [...new Set(tokens(text))].sort((a, b) => {
    const left = [...a], right = [...b]
    for (let index = 0; index < Math.min(left.length, right.length); index += 1) {
      const order = (left[index] as string).codePointAt(0)! - (right[index] as string).codePointAt(0)!
      if (order !== 0) return order
    }
    return left.length - right.length
  })
}

/**
 * A row as a document. Every column of the index is in the source, null where the row has no
 * value; each text field that holds a value has its tokens under `__sde_tokens`.
 */
export function document(
  columns: Readonly<Record<string, string>>,
  key: readonly string[],
  values: Readonly<Record<string, unknown>>,
): { id: string; source: Record<string, unknown> } {
  const unknown = Object.keys(values).filter((name) => !Object.hasOwn(columns, name)).sort()
  if (unknown.length > 0) throw new EngineError(`the index has no field ${JSON.stringify(unknown)}`)
  const missing = key.filter((column) => values[column] === null || values[column] === undefined)
  if (missing.length > 0) throw new EngineError(`a document needs its key ${JSON.stringify(missing)}`)
  const source: Record<string, unknown> = {}
  const found: Record<string, string[]> = {}
  for (const [column, kind] of Object.entries(columns)) {
    const value = stored(kind, values[column])
    source[column] = value
    if (kind === 'keyword' && value !== null) {
      const fieldTokens = searchTokens(value)
      if (fieldTokens.length > 0) found[column] = fieldTokens
    }
  }
  if (Object.keys(found).length > 0) source[TOKENS_FIELD] = found
  const keyed = key.map((column) => source[column] as StoredValue)
  if (key.some((column) => ['float', 'double'].includes(columns[column] as string))) {
    throw new EngineError('a key of a float type cannot address a document')
  }
  return { id: documentId(keyed), source }
}

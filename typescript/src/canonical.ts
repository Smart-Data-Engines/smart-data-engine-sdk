/**
 * Canonical encoding: the one place where the cross-language contract lives.
 *
 * This is a port of the rules in `docs/format-contract.md`, not a port of the Python file. That
 * distinction matters: if this were written by translating Python line by line it would inherit
 * whatever Python happens to do, and the point of a second implementation is to find out where the
 * document said "what Python does" instead of something language-neutral.
 *
 * It found one, immediately, and it is the reason this file does not use `Array.prototype.sort`.
 *
 * JavaScript compares strings by UTF-16 code unit. The contract requires code point order. For
 * everything in the Basic Multilingual Plane those are the same, so the difference is invisible in
 * any test written with Latin or even CJK identifiers. It stops being invisible above U+FFFF: an
 * astral character is stored as a surrogate pair starting at 0xD800, so UTF-16 order places every
 * emoji and every CJK extension character *before* U+E000, while code point order places them after.
 * A model with one such field name would hash differently in Python and in TypeScript, the control
 * plane would see two models, and nothing would fail until half a fleet was writing to the wrong
 * tables.
 *
 * So `compareCodePoints` below exists, and `conformance/vectors/model/004-astral-identifier` exists
 * to make sure nobody ever replaces it with `.sort()`.
 */

import { createHash } from 'node:crypto'

/** A value cannot be encoded canonically. */
export class CanonicalError extends Error {
  override readonly name = 'CanonicalError'
}

export type Canonical =
  | null
  | boolean
  | number
  | string
  | readonly Canonical[]
  | { readonly [key: string]: Canonical }

const SHORT_ESCAPES: Readonly<Record<number, string>> = {
  0x08: '\\b',
  0x09: '\\t',
  0x0a: '\\n',
  0x0c: '\\f',
  0x0d: '\\r',
  0x22: '\\"',
  0x5c: '\\\\',
}

/**
 * Compare two strings by Unicode code point.
 *
 * Not `a < b`, which is UTF-16 code unit order. See the note at the top of this file: the two orders
 * disagree for anything above U+FFFF, and that disagreement would be a hash mismatch between
 * languages rather than a visible bug.
 */
export function compareCodePoints(a: string, b: string): number {
  const left = [...a]
  const right = [...b]
  const shorter = Math.min(left.length, right.length)
  for (let i = 0; i < shorter; i += 1) {
    const x = left[i]!.codePointAt(0)!
    const y = right[i]!.codePointAt(0)!
    if (x !== y) return x < y ? -1 : 1
  }
  return left.length - right.length
}

function escape(text: string): string {
  const normalised = text.normalize('NFC')
  let out = '"'
  for (const char of normalised) {
    const code = char.codePointAt(0)!
    const short = SHORT_ESCAPES[code]
    if (short !== undefined) {
      out += short
    } else if (code < 0x20) {
      out += `\\u${code.toString(16).padStart(4, '0')}`
    } else {
      out += char
    }
  }
  return `${out}"`
}

function encode(value: unknown, path: string): string {
  if (value === null) return 'null'
  if (value === true) return 'true'
  if (value === false) return 'false'

  if (typeof value === 'string') return escape(value)

  if (typeof value === 'number') {
    // The contract forbids floating point values in the encoding, and JavaScript has only one
    // numeric type - so "is this an integer" has to be asked explicitly rather than assumed from the
    // declared type. This is a place where a Python port would have nothing to do and TypeScript has
    // to be careful.
    if (!Number.isInteger(value)) {
      throw new CanonicalError(
        `float at ${path}: floating point is not representable in canonical form, because its ` +
          'textual form differs between languages. Use an integer, or a decimal string, or the name ' +
          'of a float type if you meant to describe a type.',
      )
    }
    if (!Number.isSafeInteger(value)) {
      throw new CanonicalError(
        `integer at ${path} is outside the safe range: ${value}. JavaScript numbers lose precision ` +
          'above 2^53, so emitting this would produce bytes another language cannot reproduce. ' +
          'Refused rather than truncated.',
      )
    }
    return String(value)
  }

  if (typeof value === 'bigint') {
    return value.toString()
  }

  if (Array.isArray(value)) {
    return `[${value.map((item, i) => encode(item, `${path}[${i}]`)).join(',')}]`
  }

  if (typeof value === 'object') {
    const pairs: Array<[string, unknown]> = []
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      if (item === undefined) {
        // `undefined` is not `null` and has no canonical form. JSON.stringify drops such keys
        // silently, which would make the bytes depend on whether a field was omitted or set to
        // undefined - exactly the kind of invisible difference this encoding exists to prevent.
        throw new CanonicalError(
          `undefined at ${path}.${key}: use null if you mean a null, or omit the key entirely. ` +
            'Dropping it silently would make the bytes depend on how the caller spelled absence.',
        )
      }
      pairs.push([key.normalize('NFC'), item])
    }
    pairs.sort((left, right) => compareCodePoints(left[0], right[0]))

    const seen = new Set<string>()
    const parts: string[] = []
    for (const [key, item] of pairs) {
      if (seen.has(key)) {
        throw new CanonicalError(
          `duplicate key ${JSON.stringify(key)} at ${path} after NFC normalisation: two keys that ` +
            'differ only in Unicode composition are the same key here',
        )
      }
      seen.add(key)
      parts.push(`${escape(key)}:${encode(item, `${path}.${key}`)}`)
    }
    return `{${parts.join(',')}}`
  }

  throw new CanonicalError(
    `${typeof value} at ${path} has no canonical form. The canonical encoding accepts only null, ` +
      'boolean, integer, string, array and plain object; anything richer has to be reduced to those ' +
      'by the caller, so that the reduction is visible and testable.',
  )
}

/** Canonical form as text. Prefer {@link canonicalBytes} for hashing. */
export function canonicalString(value: unknown): string {
  return encode(value, '$')
}

/** Canonical form as UTF-8 bytes. This is what gets hashed and what the vectors compare. */
export function canonicalBytes(value: unknown): Buffer {
  return Buffer.from(canonicalString(value), 'utf8')
}

/**
 * The identifier form used for `model_version` and `shape.id`: lowercase hex, first eight bytes of
 * SHA-256 over the canonical bytes.
 */
export function digest16(value: unknown): string {
  return createHash('sha256').update(canonicalBytes(value)).digest('hex').slice(0, 16)
}

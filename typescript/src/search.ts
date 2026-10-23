/**
 * Full-text search: one rule for what a token is and when a row matches (format contract 7l).
 *
 * The same rule as `sde/search.py`, held to the same vectors in `conformance/vectors/search/`:
 *
 * - a token is a maximal run of code points, in the text normalised to NFC, whose general category
 *   is a letter (L*) or a number (N*);
 * - two tokens match when they are equal after each code point is replaced by its simple lower-case
 *   mapping - one for one, without context. `U+0130` (`İ`) is the one code point whose full lower
 *   case is two; its simple mapping, `i`, is used;
 * - a row matches when every token of the query matches some token of some chosen field.
 *
 * An engine only ever supplies candidates. This module decides, in every language the same way.
 */

import { QueryRefused } from './query.js'
import type { ReadPlan } from './query.js'
import type { Row } from './session.js'

/** The most rows one page of a search returns. */
export const MAX_SEARCH_LIMIT = 1000
/** How many rows one search call examines, at most, unless the caller says otherwise. */
export const DEFAULT_MAX_EXAMINED = 100_000
/** The most a caller may let one search call examine. */
export const MAX_EXAMINED = 1_000_000

/** A search that cannot be run as asked, refused before any engine is asked anything. */
export class SearchRefused extends QueryRefused {
  override readonly name: string = 'SearchRefused'
}

export interface SearchOptions {
  /** The entity's `string` fields to look in; all of them when absent. */
  readonly fields?: readonly string[]
  readonly after?: Readonly<Row> | null
  readonly limit?: number
  readonly maxExamined?: number
}

/**
 * One page of a search, in key order. `nextAfter` is `null` only when nothing is left to examine:
 * a page cut short by the examined bound holds fewer rows than the limit and still has a position.
 */
export interface SearchPage {
  readonly rows: readonly Row[]
  readonly nextAfter: Readonly<Row> | null
  readonly examined: number
}

/** An adapter whose engine finds candidates for a search in an index of its own. */
export interface TextIndex {
  /**
   * One page of `plan` - key order, `after`, `limit` plus one - restricted to rows that hold every
   * token of `query` in one of `fields`. The library checks every row returned, so a row too many
   * costs time and a row too few is a wrong answer.
   */
  searchRows(table: string, plan: ReadPlan, query: readonly string[], fields: readonly string[]): Promise<Row[]>
}

/** The adapter, when its engine has a text index (`searchRows`); otherwise `null`. */
export function textIndex(engine: object): TextIndex | null {
  return 'searchRows' in engine && typeof engine.searchRows === 'function' ? (engine as TextIndex) : null
}

const WORD = /^[\p{L}\p{N}]$/u

function foldCodePoint(char: string): string {
  if (char === '\u0130') return 'i'
  const lower = char.toLowerCase()
  return [...lower].length === 1 ? lower : char
}

/** The token with each code point replaced by its simple lower-case mapping. */
export function fold(token: string): string {
  let out = ''
  for (const char of token) out += foldCodePoint(char)
  return out
}

/** The tokens of `text`, in order, folded, duplicates kept. */
export function tokens(text: string): string[] {
  const found: string[] = []
  let current = ''
  for (const char of text.normalize('NFC')) {
    if (WORD.test(char)) {
      current += foldCodePoint(char)
    } else if (current !== '') {
      found.push(current)
      current = ''
    }
  }
  if (current !== '') found.push(current)
  return found
}

function compareCodePoints(a: string, b: string): number {
  const left = [...a].map((char) => char.codePointAt(0) as number)
  const right = [...b].map((char) => char.codePointAt(0) as number)
  for (let index = 0; index < Math.min(left.length, right.length); index += 1) {
    if (left[index] !== right[index]) return (left[index] as number) - (right[index] as number)
  }
  return left.length - right.length
}

/** The distinct folded tokens of a query, in code point order; refused when there are none. */
export function queryTokens(text: string): string[] {
  if (typeof text !== 'string') throw new SearchRefused('a search query is text')
  const found = [...new Set(tokens(text))].sort(compareCodePoints)
  if (found.length === 0) {
    throw new SearchRefused(
      'the query has no token - no letter or digit - so it would match every row or none, ' +
        'and neither is an answer to a question',
    )
  }
  return found
}

/** Whether every token of `query` (already folded) appears in one of `fields` of `row`. */
export function matches(
  row: Readonly<Record<string, unknown>>,
  query: readonly string[],
  fields: Iterable<string>,
): boolean {
  const present = new Set<string>()
  for (const name of fields) {
    const value = row[name]
    if (typeof value === 'string') for (const token of tokens(value)) present.add(token)
  }
  return query.every((token) => present.has(token))
}

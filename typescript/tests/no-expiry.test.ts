/**
 * Nothing in this library reads the wall clock in order to decide anything (requirement 15a.1).
 * The promise is that a client can check it by reading the code, and this file reads it. The
 * reference's `test_no_expiry.py` and the C++ library's `test_invariants.cpp` hold the same rule.
 *
 * Every wall-clock read in `src/` is named here, with what it stamps. A monotonic clock
 * (`performance.now()`, `process.hrtime`) counts from an arbitrary start and cannot be compared with
 * a date, so it cannot gate on one, and it is not a read here. The source is read as TypeScript, so
 * a comment that names `Date.now` is no read and a clock passed rather than called is one.
 */

import { readdirSync, readFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

import ts from 'typescript'
import { describe, expect, it } from 'vitest'

import { loadMap } from '../src/index.js'
import { modelFromNeutral } from '../src/testing/loader.js'

const SOURCE = fileURLToPath(new URL('../src/', import.meta.url))

function sources(directory: string): string[] {
  const out: string[] = []
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name)
    if (entry.isDirectory()) out.push(...sources(path))
    else if (entry.name.endsWith('.ts')) out.push(path)
  }
  return out.sort()
}

const COMPARISONS = new Set([
  ts.SyntaxKind.LessThanToken, ts.SyntaxKind.LessThanEqualsToken, ts.SyntaxKind.GreaterThanToken,
  ts.SyntaxKind.GreaterThanEqualsToken, ts.SyntaxKind.EqualsEqualsToken, ts.SyntaxKind.EqualsEqualsEqualsToken,
  ts.SyntaxKind.ExclamationEqualsToken, ts.SyntaxKind.ExclamationEqualsEqualsToken,
])

interface Read {
  readonly where: string
  readonly what: string
  readonly compared: boolean
}

/**
 * `Date.now`, called or passed; `new Date()` with no argument; `Date()` called; and
 * `performance.timeOrigin`, the epoch instant a monotonic reading counts from, which with
 * `performance.now()` added is the wall clock by another name.
 */
function wallClockReads(file: string, text: string): Read[] {
  const tree = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true)
  const found: Read[] = []
  const date = (node: ts.Node): boolean => ts.isIdentifier(node) && node.text === 'Date'
  const compared = (node: ts.Node): boolean => {
    for (let at: ts.Node | undefined = node.parent; at !== undefined; at = at.parent) {
      if (ts.isBinaryExpression(at) && COMPARISONS.has(at.operatorToken.kind)) return true
      if (ts.isFunctionLike(at) || ts.isBlock(at) || ts.isSourceFile(at)) return false
    }
    return false
  }
  const visit = (node: ts.Node): void => {
    let what: string | undefined
    if (ts.isPropertyAccessExpression(node) && date(node.expression) && node.name.text === 'now') what = 'Date.now'
    else if (ts.isNewExpression(node) && date(node.expression) && (node.arguments?.length ?? 0) === 0) what = 'new Date()'
    else if (ts.isCallExpression(node) && date(node.expression)) what = 'Date()'
    else if (
      ts.isPropertyAccessExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === 'performance' &&
      node.name.text === 'timeOrigin'
    ) {
      what = 'performance.timeOrigin'
    }
    if (what !== undefined) found.push({ where: file, what, compared: compared(node) })
    ts.forEachChild(node, visit)
  }
  visit(tree)
  return found
}

function libraryReads(): Read[] {
  return sources(SOURCE).flatMap((path) => wallClockReads(relative(SOURCE, path), readFileSync(path, 'utf8')))
}

describe('no expiry', () => {
  it('reads the wall clock only to stamp what happened', () => {
    // Two reads, each a stamp and neither a gate:
    // - migration.ts stamps a verification report with when it ran;
    // - books.ts is a book stream's default machine clock: an update the application gives no time
    //   takes it as its event time (format contract 8c), and it meets nothing but that side's last
    //   time, so that a side's times never go back.
    // A third has to say what it decides, because a clock read that decides something is where an
    // expiry check would live.
    expect(libraryReads().map((read) => `${read.where}: ${read.what}`)).toEqual([
      'books.ts: Date.now',
      'migration.ts: new Date()',
    ])
  })

  it('compares no wall-clock read with anything', () => {
    expect(libraryReads().filter((read) => read.compared)).toEqual([])
  })

  it('sees a read called, passed or compared, and no read in a comment', () => {
    const reads = wallClockReads(
      'probe.ts',
      [
        '// Date.now() in a comment is no read',
        'const a = Date.now()',
        'const b = { clock: Date.now }',
        'const c = new Date()',
        'const d = Date()',
        'const e = new Date(0)',
        'if (expires < Date.now()) throw new Error()',
        'const f = performance.timeOrigin + performance.now()',
      ].join('\n'),
    )
    expect(reads.map((read) => `${read.what}${read.compared ? ' compared' : ''}`)).toEqual([
      'Date.now',
      'Date.now',
      'new Date()',
      'Date()',
      'Date.now compared',
      'performance.timeOrigin',
    ])
  })

  /** Words a key that could hold a date would carry: the reference's `DATE_ISH`. */
  const DATE_ISH = ['date', 'expir', 'valid', 'until', 'issued', 'not_after', 'not_before', 'ttl', 'lease',
    'renew', 'licen', 'grace', 'deadline']

  /**
   * Every key `placement.ts` reads from a map document: `document['key']` and `'key' in document`,
   * read from the parser's syntax tree rather than from a document a test wrote, which would assert
   * something about the test and nothing about the library.
   */
  function keysTheParserReads(): Set<string> {
    const path = join(SOURCE, 'placement.ts')
    const tree = ts.createSourceFile(path, readFileSync(path, 'utf8'), ts.ScriptTarget.Latest, true)
    const keys = new Set<string>()
    const visit = (node: ts.Node): void => {
      if (ts.isElementAccessExpression(node) && ts.isStringLiteral(node.argumentExpression)) {
        keys.add(node.argumentExpression.text)
      }
      if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.InKeyword && ts.isStringLiteral(node.left)) {
        keys.add(node.left.text)
      }
      ts.forEachChild(node, visit)
    }
    visit(tree)
    return keys
  }

  it('reads no key of a map that could hold a date', () => {
    // A library that has never been told when a map was issued cannot decide that it is too old,
    // whatever a later reader calls the field.
    const keys = keysTheParserReads()
    expect(keys.size, 'the parser reads no key at all, so this test measures nothing').toBeGreaterThan(10)
    expect([...keys].filter((key) => DATE_ISH.some((word) => key.toLowerCase().includes(word)))).toEqual([])
  })

  it('changes nothing for a date written into a map by hand', () => {
    // The behavioural half, the one a suspicious client would run: the parsed map routes and lays
    // out exactly as the map without the dates does.
    const vector = fileURLToPath(new URL('../../conformance/vectors/routing/001-derived-materialisation/', import.meta.url))
    const model = modelFromNeutral(JSON.parse(readFileSync(join(vector, 'model.json'), 'utf8')))
    const raw = JSON.parse(readFileSync(join(vector, 'map.json'), 'utf8')) as Record<string, unknown>
    const plain = loadMap(raw, { model })
    const dated = loadMap({ ...raw, valid_through: '2020-01-01', expires_at: '2020-01-01T00:00:00Z' }, { model })
    expect(dated.mapVersion).toBe(plain.mapVersion)
    expect(dated.modelVersion).toBe(plain.modelVersion)
    expect(Object.keys(dated.groups).sort()).toEqual(Object.keys(plain.groups).sort())
    for (const [name, placed] of Object.entries(dated.groups)) {
      expect(placed.source.engine).toBe(plain.groups[name]!.source.engine)
      expect(placed.source.layout.tables).toEqual(plain.groups[name]!.source.layout.tables)
    }
  })
})

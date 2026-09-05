/**
 * Requirement 12.6 in the other language: this library has no way to say anything.
 *
 * The Python library has a closed event vocabulary and emits through the standard logging module
 * at INFO, so "does not complain about the account" is a claim about a stream there, and is
 * checked as one. Here there is no logging at all - no logger, no vocabulary, no output - and
 * that is the stronger position: a library that cannot speak cannot nag.
 *
 * So the thing worth pinning is the absence, not a stream. A `console.warn` added here would be
 * worse than a log line in Python, because it lands in whatever the client's process prints and
 * there is no handler to turn it off. And a rule that holds in one runtime and not the other is
 * the defect class this project has paid for repeatedly: one contract, two behaviours, no
 * compilation error.
 *
 * Written over the source text rather than by spying on `console` at runtime, because a spy only
 * covers the paths a test drives, and the line that would matter is on the path nobody drives.
 */

import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

const SRC = join(__dirname, '..', 'src')

function sourceFiles(dir: string): string[] {
  const out: string[] = []
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) out.push(...sourceFiles(full))
    else if (entry.name.endsWith('.ts')) out.push(full)
  }
  return out
}

/**
 * Every way this file could put something in front of a client.
 *
 * `debugger` is in the list because it does not merely print - it stops their process. It has no
 * business in a published library and is the one entry here that is not about tone.
 */
const CHANNELS = [
  /\bconsole\s*\./,
  /\bprocess\s*\.\s*std(out|err)\b/,
  /\bdebugger\b/,
  /\bprocess\s*\.\s*emitWarning\b/,
]

function offenders(source: string): string[] {
  // Comments and strings are stripped first. Without that this check cannot tell a line that
  // writes to the console from a comment saying not to - the use-and-mention problem that cost
  // four attempts on the prose checks in the control plane.
  const code = source
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:])\/\/.*$/gm, '$1')
    .replace(/'(?:[^'\\]|\\.)*'/g, "''")
    .replace(/"(?:[^"\\]|\\.)*"/g, '""')
    .replace(/`(?:[^`\\]|\\.)*`/g, '``')
  return CHANNELS.filter((pattern) => pattern.test(code)).map(String)
}

describe('the library says nothing at all', () => {
  it('has no channel to a client console anywhere in src', () => {
    const found: string[] = []
    for (const file of sourceFiles(SRC)) {
      for (const channel of offenders(readFileSync(file, 'utf8'))) {
        found.push(`${file.slice(SRC.length + 1)}: ${channel}`)
      }
    }
    expect(found).toEqual([])
  })

  it('catches each channel when one is planted', () => {
    // The other half. A check that passes by finding nothing has to be shown a case it finds, or
    // it can stop looking and stay green.
    expect(offenders('console.warn("no account configured")')).toHaveLength(1)
    expect(offenders('process.stderr.write("no account\\n")')).toHaveLength(1)
    expect(offenders('function f() { debugger }')).toHaveLength(1)
    expect(offenders('process.emitWarning("no account")')).toHaveLength(1)
  })

  it('does not trip on a comment or a string that mentions one', () => {
    expect(offenders('// never call console.log from here')).toEqual([])
    expect(offenders('const bad = "console.log"')).toEqual([])
  })
})

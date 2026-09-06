/**
 * The library's main entry point opens no socket, and the engine adapters are outside it.
 *
 * This is the TypeScript half of the promise the whole product rests on: a client can read the code
 * that touches their data. The specific claim here is narrower and checkable - **importing this
 * library does not pull in a database driver and does not reach a network module** - and it matters
 * for three separate reasons.
 *
 * It is what makes the no-account mode honest: hand the library a map you wrote yourself and it
 * routes, renders schema and hashes identifiers with no key, no account and nothing to connect to.
 * It is what keeps the cost of Tier 0 at zero for a client who only needs it. And it is what makes
 * `docs/observability.md` true rather than promised.
 *
 * **Checked statically, over the import closure, because this runtime offers nothing better.** The
 * reference implementation runs a subprocess and looks at `sys.modules`, which is a stronger check
 * and has no ES-module equivalent: there is no registry to read. So the closure of `index.ts` is
 * computed here from the source, and the check is shown a case it finds - the adapters themselves,
 * which do import a socket module and are deliberately not in that closure.
 *
 * The same shape of check found a real defect in the control plane: an import closure that resolved
 * a leaf module without executing the package initialisers on the way to it reported a package as
 * clean while Python was loading nine other modules. Here the analogue is a re-export, so the walker
 * follows every `export ... from` as well as every `import`.
 */

import { existsSync, readFileSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

const SRC = resolve(join(__dirname, '..', 'src'))

/** Every module specifier a file imports, re-exports, or imports dynamically. */
function specifiers(source: string): string[] {
  const code = source
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:])\/\/.*$/gm, '$1')
  const found = new Set<string>()
  for (const pattern of [
    /\b(?:import|export)\b[\s\S]*?\bfrom\s*['"]([^'"]+)['"]/g,
    /\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)/g,
    /\bimport\s*['"]([^'"]+)['"]/g,
  ]) {
    for (const match of code.matchAll(pattern)) {
      const specifier = match[1]
      if (specifier !== undefined) found.add(specifier)
    }
  }
  return [...found]
}

function fileFor(from: string, specifier: string): string | null {
  if (!specifier.startsWith('.')) return null
  const candidate = resolve(dirname(from), specifier.replace(/\.js$/, '.ts'))
  return existsSync(candidate) ? candidate : null
}

/** Every source file reachable from one entry point, and every bare specifier they name. */
function closure(entry: string): { files: Set<string>; bare: Map<string, string[]> } {
  const files = new Set<string>()
  const bare = new Map<string, string[]>()
  const queue = [resolve(entry)]
  while (queue.length > 0) {
    const file = queue.pop() as string
    if (files.has(file)) continue
    files.add(file)
    for (const specifier of specifiers(readFileSync(file, 'utf8'))) {
      const resolved = fileFor(file, specifier)
      if (resolved === null) {
        bare.set(specifier, [...(bare.get(specifier) ?? []), relative(SRC, file)])
        continue
      }
      queue.push(resolved)
    }
  }
  return { files, bare }
}

/**
 * Modules that mean "this code can reach the network", and the drivers.
 *
 * `node:dns` and `node:net` are on the list even though nothing here would use them directly: the
 * question is whether the closure *can* reach a socket, and a check that only forbade the modules
 * somebody happened to reach for is a check that passes until the next person reaches differently.
 */
const NETWORK = [
  'pg',
  'node:http',
  'node:https',
  'node:net',
  'node:tls',
  'node:dgram',
  'node:dns',
  'http',
  'https',
  'net',
  'tls',
]

describe('importing the library reaches nothing', () => {
  const main = closure(join(SRC, 'index.ts'))

  it('names no network module and no driver anywhere in its closure', () => {
    const offending = NETWORK.filter((name) => main.bare.has(name)).map(
      (name) => `${name} <- ${(main.bare.get(name) ?? []).join(', ')}`,
    )
    expect(
      offending,
      'the main entry point can reach a socket. That breaks the no-account mode, the zero cost of ' +
        'Tier 0, and the promise in docs/observability.md - all three at once.',
    ).toEqual([])
  })

  it('does not reach the engine adapters', () => {
    const reached = [...main.files]
      .map((file) => relative(SRC, file))
      .filter((name) => name.startsWith('engines'))
    expect(
      reached,
      'an engine adapter is reachable from the main entry point, so importing this library would ' +
        'resolve a database driver.',
    ).toEqual([])
  })

  it('is a check with something to find, because the adapters do reach one', () => {
    // The other half. A check that passes by finding nothing has to be shown a case it finds - and
    // the case here is not hypothetical, it is the two files this library ships for the purpose.
    const postgres = closure(join(SRC, 'engines', 'postgres.ts'))
    const clickhouse = closure(join(SRC, 'engines', 'clickhouse.ts'))
    expect(postgres.bare.has('pg')).toBe(true)
    expect(clickhouse.bare.has('node:http')).toBe(true)
  })

  it('walks re-exports as well as imports', () => {
    // The walker has to follow `export ... from`, which is how `index.ts` is written: a version
    // that only followed `import` would report the whole library as one file and pass everything.
    expect(main.files.size).toBeGreaterThan(10)
    const names = [...main.files].map((file) => relative(SRC, file))
    for (const expected of ['canonical.ts', 'placement.ts', 'session.ts', 'migration.ts']) {
      expect(names).toContain(expected)
    }
  })

  it('finds a planted network import', () => {
    // And the pattern matcher itself, shown each of the three import forms.
    expect(specifiers("import { Client } from 'pg'")).toContain('pg')
    expect(specifiers("const pg = await import('pg')")).toContain('pg')
    expect(specifiers("import 'node:http'")).toContain('node:http')
    expect(specifiers("export { request } from 'node:https'")).toContain('node:https')
    // And that a mention in a comment or a string is not a call.
    expect(specifiers("// import { Client } from 'pg'")).toEqual([])
  })
})

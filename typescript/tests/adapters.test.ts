/**
 * Adapter properties that need no server, and one that no server can show.
 *
 * The live slices measure what the two adapters do against real engines. Three things are not
 * measurable that way and are here instead.
 *
 * **Every read against a client table carries `FINAL`, and for a point read that cannot be
 * asserted behaviourally.** A `ReplacingMergeTree` does not collapse on insert, so a key written
 * twice really is two rows until a merge happens - measured: a plain `count()` returns 2 where a
 * `FINAL` one returns 1. For `count` and `keyRange` that number is the observable and the live
 * slice asserts it. For `get` the observable is *which* row comes back, and without `FINAL` that is
 * whichever part the scan reaches first: measured on this server, it happened to be the newest,
 * which is exactly why removing `FINAL` from that one method **passed every live test**. A property
 * no output can distinguish is held statically, the same way the histogram's integer arithmetic is.
 *
 * **The quoting comes from one place.** Both adapters bind it from `schema.ts`, so the DDL that
 * creates a table and the DML that writes to it cannot disagree about how an identifier is escaped -
 * which is the kind of difference that produces a table nobody can read rather than an error.
 *
 * **The ClickHouse literal renderer is total.** It has no bound-parameter protocol to fall back on,
 * so key values are rendered - and a value that reached a query as an unquoted `[object Object]`
 * would be a syntax error at best and a different query at worst.
 */

import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import { EngineError } from '../src/index.js'
import { ClickHouseEngine } from '../src/engines/clickhouse.js'

const SOURCE = join(__dirname, '..', 'src', 'engines')

function body(file: string, method: string): string {
  const source = readFileSync(join(SOURCE, file), 'utf8')
  const start = source.indexOf(`\n  async ${method}(`)
  expect(start, `${file} has no method called ${method}`).toBeGreaterThan(-1)
  // To the start of the next method at the same indentation, which is enough structure for a check
  // over four known methods and not enough to be a parser.
  const rest = source.slice(start + 1)
  const end = rest.search(/\n {2}(?:async |private |\/\*\*|[a-zA-Z]+[<(])/)
  return end === -1 ? rest : rest.slice(0, end)
}

describe('every ClickHouse read of a client table collapses the key', () => {
  // The four methods that read a table the client's data is in. The bookkeeping tables are
  // deliberately not here: those are read with `max()` over an append-only table, where a duplicate
  // cannot change the answer, so `FINAL` would be a cost with no effect.
  for (const method of ['get', 'count', 'keyRange', 'nthKey']) {
    it(`${method} carries FINAL`, () => {
      expect(
        body('clickhouse.ts', method),
        `${method} reads a client table without FINAL. For count and keyRange that is a wrong ` +
          'number; for get it is nondeterministically the superseded row, which is why this check ' +
          'is over the source - removing it from get passed every live test.',
      ).toContain('FINAL')
    })
  }

  it('is a check with something to find', () => {
    // The other half. `insert` is a write and has no FINAL, so it is the case that must not match.
    expect(body('clickhouse.ts', 'insert')).not.toContain('FINAL')
  })
})

describe('both adapters escape identifiers the same way the DDL does', () => {
  for (const file of ['postgres.ts', 'clickhouse.ts']) {
    it(`${file} binds its quoting from schema.ts`, () => {
      const source = readFileSync(join(SOURCE, file), 'utf8')
      // Bound, not written again. Two implementations of an escaping rule is how one of them ends
      // up missing the doubling, and the failure is a table nobody can read rather than an error.
      expect(source).toMatch(/const quote = QUOTE\[/)
      expect(source).not.toMatch(/function quote\w*\(/)
    })
  }
})

describe('the ClickHouse literal renderer is total', () => {
  const engine = new ClickHouseEngine('clickhouse://default:x@127.0.0.1:8123/sde')

  it('refuses a kind it cannot render rather than rendering it wrong', async () => {
    // Not connected, so the refusal has to come from the rendering rather than from a socket -
    // which is the point: this is a rule about the query, not about the server.
    await expect(engine.get('t', { id: Symbol('nope') as unknown as string })).rejects.toThrow(
      EngineError,
    )
    await expect(engine.get('t', { id: { nested: true } })).rejects.toThrow(
      'cannot be rendered for ClickHouse',
    )
  })

  it('refuses a number no engine can store', async () => {
    await expect(engine.get('t', { id: Number.NaN })).rejects.toThrow('not a value any engine')
    await expect(engine.get('t', { id: Number.POSITIVE_INFINITY })).rejects.toThrow(
      'not a value any engine',
    )
  })

  it('refuses a DSN that is not one, at construction', () => {
    // At construction rather than at the first query, because a typo in a connection string is a
    // deployment mistake and the moment to report it is the moment it is read.
    expect(() => new ClickHouseEngine('not a dsn')).toThrow('is not a ClickHouse DSN')
  })
})

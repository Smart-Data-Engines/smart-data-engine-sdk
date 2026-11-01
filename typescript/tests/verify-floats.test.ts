/**
 * NaN is NaN, and a zero is a zero of either sign, when a copy is verified (format contract section
 * 7e, "The same value").
 *
 * `Object.is` already held NaN to NaN here, where the reference's `==` and C++'s did not - a group
 * with a NaN in a float column never verified in either of them. It also held a copy to the sign of
 * a zero, which no copy on ClickHouse can keep: a part whose column is mostly zeros is written
 * sparsely, and a negative zero comes back as zero (24.8). The memory engines hold whatever they are
 * given, and the comparison does not read a field's type, so the values sit in the one column of
 * `migration/011` that is not the key.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import { loadMap, Session } from '../src/index.js'
import { verify } from '../src/migration.js'
import { modelFromNeutral } from '../src/testing/loader.js'
import { enginesFrom } from '../src/testing/memory.js'

const CASE = join(__dirname, '..', '..', 'conformance', 'vectors', 'migration',
  '011-verify-names-the-column-that-differs')
const read = (name: string): Record<string, unknown> =>
  JSON.parse(readFileSync(join(CASE, name), 'utf8')) as Record<string, unknown>

describe('a copy holds a float as the contract compares it', () => {
  it.each([
    ['nan', Number.NaN, Number.NaN, true],
    ['an infinity', Infinity, Infinity, true],
    ['a negative zero', -0, 0, true],
    ['a zero', 0, -0, true],
    ['a number', 1.5, 1.5, true],
    ['nan, then a number', Number.NaN, 1.5, false],
    ['a number, then nan', 1.5, Number.NaN, false],
    ['nan, then null', Number.NaN, null, false],
    ['both infinities', Infinity, -Infinity, false],
  ] as const)('%s', async (_, held, copied, same) => {
    const model = modelFromNeutral(read('model.json'))
    const engines = enginesFrom({
      'pg-main': { tables: { reading: [{ id: 1, celsius: held }], sample: [] } },
      'pg-copy': {
        tables: { reading: [{ id: 1, celsius: copied }], sample: [] },
        markers: { 'Reading@pg2|Reading': 1 },
      },
    })
    const session = await Session.open(model, loadMap(read('map.json'), { model }), engines)
    const report = await verify(session, 'Reading', { chunkRows: 3 })
    expect(report.matched).toBe(same)
    if (!same) expect(report.differences[0]?.columns).toEqual(['celsius'])
  })
})

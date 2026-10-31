/**
 * Section 8b, point 4: a value its field's type does not hold is refused before any engine.
 *
 * Measured on 7 October 2026 through `Session.save` on PostgreSQL 15 and ClickHouse 24.8, before
 * this rule: an integer outside int32 wrapped to the opposite sign in ClickHouse, `NaN` reached a
 * decimal column as 0.00, a date that does not exist moved two days. The shared vectors pin the
 * refusals and the form a value reaches an engine in (`errors/113`-`120`, `migration/200`); these
 * pin each type's boundaries, here and in `test_admission.py` alike.
 */
import { randomBytes } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { atScale, checkFor, Misfit } from '../src/admission.js'
import {
  BulkWriteRefused,
  hashIdentifiers,
  loadMap,
  ModelPlanningError,
  Session,
  Timestamp,
} from '../src/index.js'
import { modelFromNeutral } from '../src/testing/loader.js'
import { enginesFrom, type MemoryEngine } from '../src/testing/memory.js'

function admits(kind: string, value: unknown): unknown {
  return checkFor(kind)!(value)
}

function refusal(kind: string, value: unknown): string {
  try {
    checkFor(kind)!(value)
  } catch (error) {
    expect(error).toBeInstanceOf(Misfit)
    return (error as Error).message
  }
  throw new Error(`${kind} admitted ${String(value)}`)
}

describe('decimal', () => {
  it.each([
    ['1.23', '1.23'], ['1.230', '1.23'], ['123E-2', '1.23'], ['1E+3', '1000.00'], ['-0.00', '0.00'],
    ['0E+100', '0.00'], [' .5 ', '0.50'], ['+1.5', '1.50'], ['9999999999.99', '9999999999.99'],
    ['-9999999999.99', '-9999999999.99'], ['1.230000000000000000000000000000000', '1.23'],
  ])('reaches the engine at its scale: %s', (given, held) => {
    expect(admits('decimal(12,2)', given)).toBe(held)
  })

  it('keeps an integer given to a decimal field', () => {
    expect(admits('decimal(12,2)', 7)).toBe(7)
    expect(admits('decimal(12,2)', 7n)).toBe(7n)
    expect(admits('decimal(12,2)', -(10 ** 10) + 1)).toBe(-(10 ** 10) + 1)
  })

  it.each([
    ['1.239', 'more than 2 fractional digits'],
    ['0.001', 'more than 2 fractional digits'],
    ['9999999999.995', 'more than 2 fractional digits'],
    // Both misfit: the fractional digits are named first, in every library.
    ['12345678901.239', 'more than 2 fractional digits'],
    ['12345678901.23', 'more than 10 integer digits'],
    ['1E+10', 'more than 10 integer digits'],
    [10 ** 10, 'more than 10 integer digits'],
    [10n ** 10n, 'more than 10 integer digits'],
    ['1e999999999999999999', 'more than 10 integer digits'],
    ['1e-1999999999999999997', 'more than 2 fractional digits'],
    // Past the reference's own limits its Decimal() calls the text invalid.
    ['1e1000000000000000000', 'a value that is not an exact decimal'],
    ['1e-1999999999999999998', 'a value that is not an exact decimal'],
    ['NaN', 'a value that is not an exact decimal'],
    ['Infinity', 'a value that is not an exact decimal'],
    [Number.NaN, 'a value that is not an exact decimal'],
    [Number.POSITIVE_INFINITY, 'a value that is not an exact decimal'],
    [1.5, 'a value that is not an exact decimal'],
    [0.1 + 0.2, 'a value that is not an exact decimal'],
    [2 ** 53, 'a value that is not an exact decimal'],
    [true, 'a value that is not an exact decimal'],
    ['1_000.5', 'a value that is not an exact decimal'],
    ['', 'a value that is not an exact decimal'],
    [[], 'a value that is not an exact decimal'],
  ])('refuses %s by class', (given, why) => {
    expect(refusal('decimal(12,2)', given)).toBe(why)
  })

  it('has the edges of no integer and no fractional digits', () => {
    expect(admits('decimal(2,2)', '0.99')).toBe('0.99')
    expect(refusal('decimal(2,2)', '1.5')).toBe('more than 0 integer digits')
    expect(admits('decimal(5,0)', '12345')).toBe('12345')
    expect(refusal('decimal(5,0)', '1.5')).toBe('more than 0 fractional digits')
    expect(refusal('decimal(5,0)', '123456')).toBe('more than 5 integer digits')
  })

  it('writes plain notation with the column\'s digits', () => {
    expect(atScale(false, '123', -2n, 2)).toBe('1.23')
    expect(atScale(true, '5', -1n, 2)).toBe('-0.50')
    expect(atScale(false, '1', 3n, 2)).toBe('1000.00')
    expect(atScale(false, '0', 0n, 0)).toBe('0')
    expect(atScale(false, '1', 3n, 0)).toBe('1000')
  })
})

describe('integers', () => {
  it('admits the edges of each type as given', () => {
    expect(admits('int32', -(2 ** 31))).toBe(-(2 ** 31))
    expect(admits('int32', 2 ** 31 - 1)).toBe(2 ** 31 - 1)
    expect(admits('int32', 7n)).toBe(7n)
    expect(admits('int64', -(2n ** 63n))).toBe(-(2n ** 63n))
    expect(admits('int64', 2n ** 63n - 1n)).toBe(2n ** 63n - 1n)
    expect(admits('int64', Number.MAX_SAFE_INTEGER)).toBe(Number.MAX_SAFE_INTEGER)
  })

  it.each([
    ['int32', 2 ** 31, 'an integer outside int32'],
    ['int32', -(2 ** 31) - 1, 'an integer outside int32'],
    ['int32', 2n ** 31n, 'an integer outside int32'],
    ['int64', 2n ** 63n, 'an integer outside int64'],
    ['int64', -(2n ** 63n) - 1n, 'an integer outside int64'],
    ['int32', true, 'a value that is not an integer'],
    ['int64', false, 'a value that is not an integer'],
    ['int32', 1.5, 'a value that is not an integer'],
    ['int64', 2 ** 53, 'a value that is not an integer'],
    ['int32', '7', 'a value that is not an integer'],
  ])('%s refuses %s', (kind, given, why) => {
    expect(refusal(kind, given)).toBe(why)
  })
})

describe('floats', () => {
  it.each([1.1, 3.4028234663852886e38, -3.4028234663852886e38, 1e-45, 0, -0, Infinity, -Infinity])(
    'float32 holds %s',
    (given) => {
      expect(admits('float32', given)).toBe(given)
    },
  )

  it('holds NaN in both widths', () => {
    expect(admits('float32', Number.NaN)).toBeNaN()
    expect(admits('float64', Number.NaN)).toBeNaN()
  })

  it.each([3.4028235677973366e38, -3.5e38, 1e39, 1e-46, -1e-50])('float32 refuses %s', (given) => {
    expect(refusal('float32', given)).toBe('a number outside float32')
  })

  it('takes numbers only', () => {
    expect(admits('float64', 1e308)).toBe(1e308)
    for (const given of [true, '1.5', 7n, [1.5]]) expect(refusal('float64', given)).toBe('a value that is not a number')
  })
})

describe('the types whose rule is a filter\'s', () => {
  it('bool, string and bytes', () => {
    expect(admits('bool', true)).toBe(true)
    expect(refusal('bool', 1)).toBe('a value that is not a boolean')
    expect(refusal('bool', 'true')).toBe('a value that is not a boolean')
    expect(admits('string', 'é')).toBe('é')
    expect(refusal('string', 7)).toBe('a value that is not text')
    expect(refusal('string', '\ud800')).toBe('a value that is not text')
    expect(admits('bytes', new Uint8Array([97, 98]))).toEqual(Buffer.from('ab'))
    expect(refusal('bytes', 'ab')).toBe('a value that is not bytes')
  })

  it('uuid and date', () => {
    expect(admits('uuid', '0E984725-C51C-4BF4-9960-E1C80E27ABA0')).toBe('0e984725-c51c-4bf4-9960-e1c80e27aba0')
    expect(refusal('uuid', 'not-a-uuid')).toBe('a value that is not a UUID')
    expect(refusal('uuid', 7)).toBe('a value that is not a UUID')
    expect(admits('date', '2026-10-07')).toBe('2026-10-07')
    expect(refusal('date', '2026-02-30')).toBe('a value that is not a date')
    expect(refusal('date', new Date('2026-10-07T00:00:00Z'))).toBe('a value that is not a date')
    expect(refusal('date', 7)).toBe('a value that is not a date')
  })

  it('a timestamp is its instant in UTC, and a timezone-free one its UTC wall time', () => {
    const utc = Timestamp.from('2026-11-09T09:30:15.123456Z')
    for (const kind of ['timestamptz', 'timestamp']) {
      expect((admits(kind, '2026-11-09T11:30:15.123456+02:00') as Timestamp).toISOString()).toBe(utc.toISOString())
      expect((admits(kind, utc) as Timestamp).toISOString()).toBe(utc.toISOString())
      expect((admits(kind, new Date('2026-11-09T09:30:15.123Z')) as Timestamp).toISOString())
        .toBe('2026-11-09T09:30:15.123000Z')
    }
    expect((admits('timestamp', '2026-11-09 09:30:15') as Timestamp).toISOString()).toBe('2026-11-09T09:30:15.000000Z')
    for (const given of ['2026-10-07T25:00:00Z', '2026-10-07T10:00:00.1234567Z', 'not a time', 7]) {
      expect(refusal('timestamptz', given)).toBe('a value that is not a timestamp')
    }
  })

  it('does not check json yet', () => {
    expect(checkFor('json')).toBeUndefined()
  })
})

describe('in a session', () => {
  const ENTRY = {
    entities: [{
      name: 'Entry',
      fields: [
        { name: 'amount', type: 'decimal(12,2)' },
        { name: 'count', type: 'int32', nullable: true },
        { name: 'id', type: 'string' },
      ],
      key: ['id'],
    }],
  }

  async function open(hashed = false): Promise<[Session, MemoryEngine]> {
    let model = modelFromNeutral(ENTRY)
    let names
    if (hashed) ({ model, names } = hashIdentifiers(model, randomBytes(32)))
    const groups = Object.fromEntries(model.entities.map((entity) => [entity.name, {
      source: { id: 'source', engine: 'pg-main', layout: { auto: true } },
    }]))
    const placement = loadMap({ contract: 3, model_version: model.version, map_version: 1, groups }, { model })
    const engines = enginesFrom({ 'pg-main': { dialect: 'postgres' } })
    const session = await Session.open(model, placement, engines, names === undefined ? {} : { names })
    return [session, engines['pg-main'] as MemoryEngine]
  }

  it('refuses a save before the engine and names the type', async () => {
    const [session, engine] = await open()
    const refused = await session.save('Entry', { amount: '1.239', count: 1, id: 'e-1' }).catch((error: unknown) => error)
    expect((refused as Error).constructor).toBe(ModelPlanningError)
    expect((refused as Error).message).toBe('Entry.amount is decimal(12,2) and this row gives it more than 2 fractional digits')
    expect(engine.recorded.calls).toEqual([])
  })

  it('passes the value at its scale and an integer as given', async () => {
    const [session, engine] = await open()
    await session.save('Entry', { amount: '1.230', count: null, id: 'e-1' })
    await session.save('Entry', { amount: 7, count: 2, id: 'e-2' })
    const rows = Object.values(engine.tables)[0]!
    expect(rows.find((row) => row['id'] === 'e-1')).toEqual({ amount: '1.23', count: null, id: 'e-1' })
    expect(rows.find((row) => row['id'] === 'e-2')).toEqual({ amount: 7, count: 2, id: 'e-2' })
  })

  it('refuses a batch whole, with the row\'s position', async () => {
    const [session, engine] = await open()
    const refused = await session.saveMany('Entry', [
      { amount: '1.23', count: 1, id: 'e-1' },
      { amount: '1.23', count: 2 ** 31, id: 'e-2' },
    ]).catch((error: unknown) => error)
    expect((refused as Error).constructor).toBe(BulkWriteRefused)
    expect((refused as Error).message).toBe('row 1: Entry.count is int32 and this row gives it an integer outside int32')
    expect(engine.recorded.calls).toEqual([])
  })

  it('names the client\'s field under hashed identifiers', async () => {
    const [session, engine] = await open(true)
    const refused = await session.save('Entry', { amount: '1.23', count: true, id: 'e-1' }).catch((error: unknown) => error)
    expect((refused as Error).message).toBe('Entry.count is int32 and this row gives it a value that is not an integer')
    expect(engine.recorded.calls).toEqual([])
  })
})

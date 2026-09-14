/** Exact mean calculation uses integer division and explicit half-even rounding. */
import { expect, it } from 'vitest'
import { numericSummary } from '../src/query.js'
it.each([
  ['1', '2', 0, '0'], ['3', '2', 0, '2'], ['-1', '2', 0, '-0'], ['-3', '2', 0, '-2'],
  ['1', '3', 6, '0.333333'], ['2', '3', 6, '0.666667'],
  ['-1', '3', 6, '-0.333333'], ['-2', '3', 6, '-0.666667'],
  ['18446744073709551614', '2', 6, '9223372036854775807.000000'],
])('rounds total=%s count=%s at scale=%s', (total, present, scale, mean) => {
  const result = numericSummary({ sde_count: present, sde_present: present, sde_min: '0',
    sde_max: total, sde_total: total }, { name: 'value', type: 'int64' }, scale as number)
  expect(result.mean).toBe(mean)
  expect(result.total).toBe(BigInt(total))
})
it('keeps a 56-digit decimal sum and its exact mean', () => {
  const total = '199999999999999999999999999999999999998.246913578024691356'
  const result = numericSummary({ sde_count: '3', sde_present: '2', sde_min: '0.00',
    sde_max: '99999999999999999999999999999999999999.123456789012345678', sde_total: total },
  { name: 'value', type: 'decimal(56,18)' }, 18)
  expect(result.total).toBe(total)
  expect(result.mean).toBe('99999999999999999999999999999999999999.123456789012345678')
})
it('normalizes no non-null values independently of backend empty defaults', () => {
  expect(numericSummary({ sde_count: '3', sde_present: '0', sde_min: '0', sde_max: '0', sde_total: '0' },
    { name: 'value', type: 'int64' }, 6)).toEqual({
    count: 3n, nonNullCount: 0n, minimum: null, maximum: null, total: null, mean: null,
  })
})

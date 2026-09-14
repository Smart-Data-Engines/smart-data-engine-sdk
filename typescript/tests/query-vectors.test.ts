/** Shared logical read normalization and exact summary bytes. */
import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { join } from 'node:path'
import { expect, it } from 'vitest'
import { canonicalBytes, Timestamp } from '../src/index.js'
import { numericSummary, planRead, QueryRefused, type ReadColumn, type ReadOptions } from '../src/query.js'
const root = fileURLToPath(new URL('../../conformance/vectors/query', import.meta.url))
function decode(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(decode)
  if (value !== null && typeof value === 'object') {
    const record = value as Record<string, unknown>, keys = Object.keys(record)
    if (keys.length === 1 && keys[0] === '$int') return BigInt(record['$int'] as string)
    if (keys.length === 1 && keys[0] === '$bytes') return Buffer.from(record['$bytes'] as string, 'hex')
    return Object.fromEntries(Object.entries(record).map(([name, item]) => [name, decode(item)]))
  }
  return value
}
function normalized(column: ReadColumn, value: unknown): unknown {
  if (value === null || column.type === 'bool') return value
  if (column.type.startsWith('float')) { const bytes = Buffer.alloc(8); bytes.writeDoubleBE(value as number); return bytes.toString('hex') }
  if (value instanceof Timestamp) return value.toISOString()
  if (column.type === 'bytes') return Buffer.from(value as Uint8Array).toString('hex')
  return String(value)
}
for (const name of readdirSync(root).sort()) it('query/' + name, () => {
  const folder = join(root, name)
  const fixture = JSON.parse(readFileSync(join(folder, 'case.json'), 'utf8')) as {
    kind: string; columns: Record<string, string>; key: string[]; options?: Record<string, unknown>;
    record: Record<string, unknown>; type: string; mean_scale?: number
  }
  let got: unknown
  try {
    if (fixture.kind === 'summary') {
      const summary = numericSummary(fixture.record, { name: 'value', type: fixture.type }, fixture.mean_scale ?? 6)
      got = { count: summary.count.toString(), non_null_count: summary.nonNullCount.toString(),
        minimum: summary.minimum === null ? null : String(summary.minimum),
        maximum: summary.maximum === null ? null : String(summary.maximum),
        total: summary.total === null ? null : String(summary.total), mean: summary.mean }
    } else {
      const options = decode(fixture.options ?? {}) as Record<string, unknown>
      if ('order_by' in options) { options['orderBy'] = options['order_by']; delete options['order_by'] }
      const result = planRead(Object.entries(fixture.columns).map(([name, type]) => ({ name, type })), fixture.key, options as ReadOptions)
      got = { filters: result.filters.map(item => ({ field: item.column.name, type: item.column.type,
        op: item.operation, value: normalized(item.column, item.value) })),
        order: result.order.map(item => item.name), descending: result.descending,
        after: result.after === null ? null : result.order.map((column, index) => normalized(column, result.after![index])),
        limit: result.limit,
      }
    }
  } catch (error) {
    if (!(error instanceof QueryRefused)) throw error
    got = { error: 'QueryRefused' }
  }
  expect(got).toEqual(JSON.parse(readFileSync(join(folder, 'expected.json'), 'utf8')))
  expect(Buffer.from(canonicalBytes(got)).toString('hex')).toBe(readFileSync(join(folder, 'expected.hex'), 'utf8').trim())
})

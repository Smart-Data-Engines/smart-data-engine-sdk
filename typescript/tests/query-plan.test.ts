/** Bounded plans and positional parameter order, before any engine is called. */
import { expect, it } from 'vitest'
import { planRead, QueryRefused, queryValue, readSql, type ReadPlan, type ReadOptions } from '../src/query.js'
import { Timestamp } from '../src/timestamp.js'

function sql(plan: ReadPlan, dialect = 'postgres', count = false): [string, unknown[]] {
  const values: unknown[] = []
  const statement = readSql('events', plan, dialect, value => {
    values.push(value); return dialect === 'postgres' ? '$' + values.length : '%(q' + values.length + ')s'
  }, count)
  return [statement, values]
}
it('binds each occurrence of a three-column cursor prefix in statement order', () => {
  const plan = planRead(['a', 'b', 'c'].map(name => ({ name, type: 'int64' })), ['a', 'b', 'c'],
    { after: { a: 1n, b: 2n, c: 3n } })
  const [statement, values] = sql(plan)
  expect(values).toEqual([1n, 1n, 2n, 1n, 2n, 3n, 101])
  expect(statement).toContain('"c" > $6')
  expect(statement).toContain('LIMIT $7')
})
it.each([false, true])('keeps NULL last and continues inside the NULL prefix, descending=%s', descending => {
  const plan = planRead([{ name: 'id', type: 'int64' }, { name: 'label', type: 'string' }], ['id'],
    { orderBy: 'label', descending, after: { label: null, id: 7n } })
  const [statement, values] = sql(plan)
  expect(statement).toContain('(("label" COLLATE "C") IS NULL AND ')
  expect(statement).toContain(descending ? '"id" < $1' : '"id" > $1')
  expect(values).toEqual([7n, 101])
})
it('uses explicit portable UUID and text ordering expressions', () => {
  const id = '00000000-0000-0001-0000-000000000000'
  const plan = planRead([{ name: 'id', type: 'uuid' }, { name: 'tag', type: 'string' }], ['id'],
    { where: { tag: 'é' }, after: { id } })
  expect(sql(plan)[0]).toContain('("tag" COLLATE "C") = $1')
  const [statement, values] = sql(plan, 'clickhouse')
  expect(statement).toContain('toString(')
  expect(statement).toContain(' FINAL')
  expect(values).toContain(id)
})
it('keeps all six timestamp digits and refuses a seventh', () => {
  const field = { name: 'at', type: 'timestamp' }
  expect(queryValue(field, '2026-09-14T12:00:00.123456+02:00')).toEqual(Timestamp.from('2026-09-14T10:00:00.123456Z'))
  expect(() => queryValue(field, '2026-09-14T12:00:00.1234567Z')).toThrow(QueryRefused)
})
it('uses the finer decimal scale and keeps a large bound as exact text', () => {
  const plan = planRead([{ name: 'id', type: 'int64' }, { name: 'amount', type: 'decimal(12,2)' }], ['id'],
    { bounds: { field: 'amount', low: '123456.005' } })
  const [statement, values] = sql(plan, 'clickhouse')
  expect(statement).toContain('Nullable(Decimal(76,3))')
  expect(values).toEqual(['123456.005', 101])
  expect(queryValue({ name: 'amount', type: 'decimal(12,2)' }, '-.25')).toBe('-0.25')
  expect(() => queryValue({ name: 'amount', type: 'decimal(12,2)' }, '1e999999')).toThrow(QueryRefused)
})
it.each([
  { limit: 0 }, { limit: 1001 }, { limit: true }, { limit: null }, { descending: 'yes' }, { descending: null },
  { after: { label: 'a' } }, { bounds: { field: 'label', low: 'a', high: 'z' } },
  { bounds: { field: 'id' } }, { where: { missing: 3 } }, { orderBy: 'missing' },
])('refuses malformed options before I/O %#', options => {
  expect(() => planRead([{ name: 'id', type: 'int64' }, { name: 'label', type: 'string' }], ['id'], options as ReadOptions)).toThrow(QueryRefused)
})
it('counts without requiring a paginated float-key order', () => {
  const plan = planRead([{ name: 'id', type: 'float64' }], ['id'], { paginate: false })
  expect(sql(plan, 'postgres', true)).toEqual(['SELECT CAST(count(*) AS text) AS sde_count FROM "events"', []])
})
it('accepts an absent logical position as null', () => {
  const plan = planRead([{ name: 'id', type: 'int64' }], ['id'], { where: null, after: null, bounds: null, orderBy: null })
  expect(sql(plan)[1]).toEqual([101])
})

it('keeps aggregate key width independent and refuses an unrepresentable decimal cursor', () => {
  const columns = Array.from({ length: 33 }, (_, i) => ({ name: 'k' + i, type: 'int64' }))
  const query = planRead(columns, columns.map(column => column.name), { paginate: false })
  expect(sql(query, 'postgres', true)[1]).toEqual([])
  expect(() => planRead([{ name: 'id', type: 'decimal(77,0)' }], ['id'])).toThrow('76 digits')
})

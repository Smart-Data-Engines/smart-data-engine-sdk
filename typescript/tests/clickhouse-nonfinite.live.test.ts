/**
 * NaN and the infinities reach ClickHouse as themselves.
 *
 * JSON has no token for them, and `JSON.stringify` writes null: measured on 7 October 2026 through
 * `Session.save`, ClickHouse 24.8 stored NULL in a nullable float column and 0.00 in a required
 * decimal one, with no error. Quoted, the server reads them as the values (24.8 and 26.9; the bare
 * token `nan` is refused). A single insert, a batch and a migration's copy share the one path.
 */
import { randomUUID } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { ClickHouseEngine } from '../src/engines/clickhouse.js'

const dsn = process.env['SDE_CLICKHOUSE_DSN']
const root = dsn ? new URL(dsn) : undefined

async function native(statement: string): Promise<string> {
  const response = await fetch(`http://${root!.host}/?database=${encodeURIComponent(root!.pathname.slice(1))}`, {
    method: 'POST',
    body: statement,
    headers: { Authorization: `Basic ${Buffer.from(`${decodeURIComponent(root!.username)}:${decodeURIComponent(root!.password)}`).toString('base64')}` },
  })
  const text = await response.text()
  if (!response.ok) throw new Error(text)
  return text
}

describe.skipIf(!dsn)('non-finite floats into ClickHouse', () => {
  it('stores NaN and both infinities in required and nullable columns of both widths', async () => {
    const table = 'sde_nonfinite_' + randomUUID().replaceAll('-', '')
    const engine = new ClickHouseEngine(dsn!)
    try {
      await native(`CREATE TABLE ${table} (id Int64, single Float32, double Float64, ` +
        'maybe_single Nullable(Float32), maybe_double Nullable(Float64)) ENGINE = MergeTree ORDER BY id')
      await engine.connect()
      const row = (id: number, value: number) => ({ id, single: value, double: value, maybe_single: value, maybe_double: value })
      await engine.insert(table, row(1, Number.NaN))
      await engine.insertMany(table, [row(2, Number.POSITIVE_INFINITY), row(3, Number.NEGATIVE_INFINITY)])
      await engine.copyIn(table, [row(4, Number.NaN), row(5, 1.5)])
      const held = await native(`SELECT id, toString(single), toString(double), toString(maybe_single), ` +
        `toString(maybe_double) FROM ${table} ORDER BY id FORMAT TSV`)
      expect(held.trim().split('\n')).toEqual([
        '1\tnan\tnan\tnan\tnan',
        '2\tinf\tinf\tinf\tinf',
        '3\t-inf\t-inf\t-inf\t-inf',
        '4\tnan\tnan\tnan\tnan',
        '5\t1.5\t1.5\t1.5\t1.5',
      ])
    } finally {
      await engine.close()
      await native(`DROP TABLE IF EXISTS ${table} SYNC`)
    }
  })
})

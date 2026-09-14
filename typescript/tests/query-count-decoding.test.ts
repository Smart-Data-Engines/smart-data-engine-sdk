/** COUNT arrives as decimal text and must not pass through a JavaScript number. */
import { expect, it, vi } from 'vitest'
import { PostgresEngine } from '../src/engines/postgres.js'
import { ClickHouseEngine } from '../src/engines/clickhouse.js'
import { planRead } from '../src/query.js'

it('PostgreSQL count decoding retains integers beyond 2^53', async () => {
  const engine = new PostgresEngine('postgresql://localhost/unused')
  const raw = engine as unknown as { run: (sql: string, params: unknown[]) => Promise<{ rows: { sde_count: string }[] }> }
  const run = vi.spyOn(raw, 'run').mockResolvedValue({ rows: [{ sde_count: '9007199254740993' }] })
  try {
    expect(await engine.countRows('events', planRead([{ name: 'id', type: 'int64' }], ['id'], { paginate: false }))).toBe(9007199254740993n)
    expect(run).toHaveBeenCalledOnce()
  } finally { run.mockRestore() }
})
it('ClickHouse count decoding retains integers beyond 2^53', async () => {
  const engine = new ClickHouseEngine('clickhouse://localhost/unused')
  const raw = engine as unknown as { query: (sql: string) => Promise<{ sde_count: string }[]> }
  const query = vi.spyOn(raw, 'query').mockResolvedValue([{ sde_count: '9007199254740993' }])
  try {
    expect(await engine.countRows('events', planRead([{ name: 'id', type: 'int64' }], ['id'], { paginate: false }))).toBe(9007199254740993n)
    expect(query).toHaveBeenCalledOnce()
  } finally { query.mockRestore() }
})

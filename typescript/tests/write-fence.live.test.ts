/** Each language changes a native guard which the other language reads and writes through. */
import { execFileSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { resolve } from 'node:path'
import { Client } from 'pg'
import { describe, expect, it } from 'vitest'
import { WRITE_EPOCH_COLUMN, WriteFence, type Row } from '../src/index.js'
import { ClickHouseFences } from '../src/engines/_write-fences.js'
import { PostgresEngine } from '../src/engines/postgres.js'
import { ClickHouseEngine, literal } from '../src/engines/clickhouse.js'
import { QUOTE } from '../src/schema.js'

const PG = process.env['SDE_POSTGRES_DSN'], CH = process.env['SDE_CLICKHOUSE_DSN']
type Dialect = 'postgres' | 'clickhouse'
const PROJECT = '1'.repeat(32), HOLD = '2'.repeat(32)
function peer(action: string, dialect: Dialect, namespace: string, table: string): Record<string, unknown> {
  return JSON.parse(execFileSync(resolve('../python/.venv/bin/python'), [
    resolve('../python/tests/fence_peer.py'), action, dialect, namespace, table,
  ], { encoding: 'utf8', timeout: 30_000 })) as Record<string, unknown>
}
async function rawCh(sql: string, settings: Record<string, string> = {}): Promise<string> {
  const dsn = new URL(CH as string)
  const parameters = new URLSearchParams({ wait_end_of_query: '1', ...settings })
  const response = await fetch(`http://${dsn.host}/?${parameters.toString()}`, {
    method: 'POST', body: sql,
    headers: { Authorization: `Basic ${Buffer.from(`${decodeURIComponent(dsn.username)}:${decodeURIComponent(dsn.password)}`).toString('base64')}` },
  })
  const text = await response.text()
  if (!response.ok) throw new Error(text)
  return text
}
async function fixture(dialect: Dialect, body: (engine: PostgresEngine | ClickHouseEngine,
  namespace: string, table: string) => Promise<void>): Promise<void> {
  const namespace = 'ts_fence_' + randomUUID().replaceAll('-', '')
  const table = dialect === 'postgres' ? 'events "quoted' : 'events `quoted'
  const quote = QUOTE[dialect] as (name: string) => string
  const raw = dialect === 'postgres' ? new Client({ connectionString: PG as string }) : undefined
  let engine: PostgresEngine | ClickHouseEngine
  if (raw !== undefined) {
    await raw.connect()
    await raw.query(`CREATE SCHEMA ${quote(namespace)}`)
    await raw.query(`CREATE TABLE ${quote(namespace)}.${quote(table)} (id bigint PRIMARY KEY)`)
    const dsn = new URL(PG as string)
    dsn.searchParams.set('options', `-csearch_path=${namespace}`)
    dsn.searchParams.set('application_name', namespace)
    engine = new PostgresEngine(dsn.toString())
  } else {
    await rawCh(`CREATE DATABASE ${quote(namespace)} ENGINE=Atomic`)
    await rawCh(`CREATE TABLE ${quote(namespace)}.${quote(table)} (id Int64) ENGINE=ReplacingMergeTree ORDER BY id`)
    const dsn = new URL(CH as string); dsn.pathname = '/' + namespace
    engine = new ClickHouseEngine(dsn.toString())
  }
  try {
    await engine.connect()
    await body(engine, namespace, table)
  } finally {
    await engine.close()
    if (raw !== undefined) {
      await raw.query(`DROP SCHEMA ${quote(namespace)} CASCADE`); await raw.end()
    } else {
      const detached = Number(await rawCh(`SELECT count() FROM system.detached_tables WHERE database='${namespace}'`))
      if (detached > 0) await rawCh(`ATTACH TABLE ${quote(namespace)}.${quote(table)}`)
      await rawCh(`DROP DATABASE ${quote(namespace)} SYNC`)
    }
  }
}

describe.each(['postgres', 'clickhouse'] as const)('native fencing on %s', (dialect) => {
  const live = dialect === 'postgres' ? PG !== undefined : CH !== undefined
  it.skipIf(!live)('accepts a Python guard, rejects old writes after a Python transition', async () => {
    await fixture(dialect, async (engine, namespace, table) => {
      peer('prepare', dialect, namespace, table)
      const fence = engine.writeFence(table, { projectId: PROJECT })
      expect((await fence.state()).epoch).toBe(1)
      await expect(engine.insert(table, { id: 1 })).rejects.toThrow()
      await engine.insert(table, { id: 2, [WRITE_EPOCH_COLUMN]: 1 })
      peer('advance', dialect, namespace, table)
      await expect(engine.insert(table, { id: 3, [WRITE_EPOCH_COLUMN]: 1 })).rejects.toThrow()
      await engine.insert(table, { id: 4, [WRITE_EPOCH_COLUMN]: 2 })
      expect(await engine.count(table)).toBe(2)
    })
  })
  it.skipIf(!live)('publishes native constraints Python can read, and releases only its hold', async () => {
    await fixture(dialect, async (engine, namespace, table) => {
      const fence = engine.writeFence(table, { projectId: PROJECT })
      await fence.prepare(1)
      await engine.insert(table, { id: 1, [WRITE_EPOCH_COLUMN]: 1 })
      await fence.freeze(HOLD)
      await expect(engine.insert(table, { id: 2, [WRITE_EPOCH_COLUMN]: 1 })).rejects.toThrow()
      await fence.advance(2)
      expect(peer('read', dialect, namespace, table)['closed']).toBe(true)
      await fence.release(HOLD)
      expect(peer('read', dialect, namespace, table)['lower_epoch']).toBe(2)
      await expect(engine.insert(table, { id: 3, [WRITE_EPOCH_COLUMN]: 1 })).rejects.toThrow()
      await engine.insert(table, { id: 4, [WRITE_EPOCH_COLUMN]: 2 })
      expect(await engine.count(table)).toBe(2)
    })
  })
})


it.skipIf(CH === undefined)('ClickHouse drains an INSERT that captured older metadata', async () => {
  await fixture('clickhouse', async (engine, namespace, table) => {
    const quote = QUOTE['clickhouse'] as (name: string) => string
    const fence = engine.writeFence(table, { projectId: PROJECT })
    await fence.prepare(1)
    const marker = 'old_insert_' + randomUUID().replaceAll('-', '')
    const pending = rawCh(`INSERT INTO ${quote(namespace)}.${quote(table)} SELECT 1, 1+sleep(2) /* ${marker} */`)
      .then(() => ({ ok: true }), (error: unknown) => ({ error }))
    try {
      const deadline = Date.now() + 5000
      let observed = false
      while (Date.now() < deadline) {
        const count = Number(await rawCh(`SELECT count() FROM system.processes WHERE startsWith(query,'INSERT') ` +
          `AND position(query,'${marker}')>0 AND elapsed>0.1`))
        if (count === 1) { observed = true; break }
        await new Promise((resolve) => setTimeout(resolve, 10))
      }
      expect(observed).toBe(true)
      expect((await fence.freeze(HOLD)).closed).toBe(true)
      expect(await engine.count(table)).toBe(1)
      expect(await pending).toEqual({ ok: true })
    } finally {
      await pending
    }
  })
}, 15_000)

it.skipIf(PG === undefined)('PostgreSQL drains an open source transaction before returning', async () => {
  await fixture('postgres', async (engine, namespace, table) => {
    const quote = QUOTE['postgres'] as (name: string) => string
    const fence = engine.writeFence(table, { projectId: PROJECT })
    await fence.prepare(1)
    const writer = new Client({ connectionString: PG as string })
    const observer = new Client({ connectionString: PG as string })
    await writer.connect(); await observer.connect()
    let pending: Promise<unknown> | undefined
    try {
      await writer.query('BEGIN')
      await writer.query(`INSERT INTO ${quote(namespace)}.${quote(table)} VALUES (1,1)`)
      pending = fence.freeze(HOLD).then((state) => ({ closed: state.closed }), (error: unknown) => ({ error }))
      const deadline = Date.now() + 5000
      let observed = false
      while (Date.now() < deadline) {
        const result = await observer.query('SELECT wait_event_type FROM pg_stat_activity WHERE application_name=$1', [namespace])
        if (result.rows.some((row) => row.wait_event_type === 'Lock')) { observed = true; break }
        await new Promise((resolve) => setTimeout(resolve, 10))
      }
      expect(observed).toBe(true)
      await writer.query('COMMIT')
      expect(await pending).toEqual({ closed: true })
      expect(await engine.count(table)).toBe(1)
      await expect(engine.insert(table, { id: 2, [WRITE_EPOCH_COLUMN]: 1 })).rejects.toThrow()
    } finally {
      await writer.query('ROLLBACK')
      await pending
      await writer.end(); await observer.end()
    }
  })
}, 15_000)


it.skipIf(CH === undefined)('confirms the drain intent even when HTTP defaults enable async insertion', async () => {
  await fixture('clickhouse', async (engine, namespace, table) => {
    await engine.writeFence(table, { projectId: PROJECT }).prepare(1)
    const settings = { database: namespace, async_insert: '1', wait_for_async_insert: '0',
      async_insert_busy_timeout_ms: '2000', async_insert_use_adaptive_busy_timeout: '0' }
    const query = async (sql: string): Promise<Row[]> =>
      (JSON.parse(await rawCh(sql + ' FORMAT JSON', settings)) as { data: Row[] }).data
    const control = await query("SELECT toUInt8(getSetting('async_insert')) AS asynchronous, " +
      "toUInt8(getSetting('wait_for_async_insert')) AS waits")
    expect(control).toEqual([{ asynchronous: 1, waits: 0 }])
    const observed: number[] = []
    const command = async (sql: string): Promise<void> => {
      if (sql.startsWith('DETACH TABLE')) {
        const rows = await query(`SELECT count() AS n FROM __sde_fence_drains WHERE hold='${HOLD}'`)
        observed.push(Number(rows[0]?.['n']))
        expect(Number(rows[0]?.['n'])).toBe(1)
      }
      await rawCh(sql, settings)
    }
    const fence = new WriteFence(new ClickHouseFences({ query, command, literal }), table, { projectId: PROJECT })
    expect((await fence.freeze(HOLD)).closed).toBe(true)
    expect(observed).toEqual([1])
  })
})

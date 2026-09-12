import { randomUUID } from 'node:crypto'
import { Client } from 'pg'
import { PostgresEngine } from '../src/engines/postgres.js'
import { ClickHouseEngine } from '../src/engines/clickhouse.js'
import { QUOTE } from '../src/schema.js'
const PG = process.env['SDE_POSTGRES_DSN'], CH = process.env['SDE_CLICKHOUSE_DSN']
export type Dialect = 'postgres' | 'clickhouse'
export type Database = PostgresEngine | ClickHouseEngine

async function rawCh(sql: string): Promise<void> {
  const dsn = new URL(CH as string)
  const response = await fetch(`http://${dsn.host}/?wait_end_of_query=1`, { method: 'POST', body: sql,
    headers: { Authorization: `Basic ${Buffer.from(`${decodeURIComponent(dsn.username)}:${decodeURIComponent(dsn.password)}`).toString('base64')}` } })
  if (!response.ok) throw new Error(await response.text())
}

export async function fixture(dialect: Dialect, body: (engine: Database, table: string, namespace: string) => Promise<void>): Promise<void> {
  const name = 'generation_' + randomUUID().replaceAll('-', '')
  const quote = QUOTE[dialect] as (name: string) => string
  let engine: Database
  const admin = dialect === 'postgres' ? new Client({ connectionString: PG as string }) : undefined
  if (admin !== undefined) {
    await admin.connect(); await admin.query(`CREATE SCHEMA ${quote(name)}`)
    const dsn = new URL(PG as string); dsn.searchParams.set('options', `-csearch_path=${name}`)
    engine = new PostgresEngine(dsn.toString())
  } else {
    await rawCh(`CREATE DATABASE ${quote(name)} ENGINE=Atomic`)
    const dsn = new URL(CH as string); dsn.pathname = '/' + name
    engine = new ClickHouseEngine(dsn.toString())
  }
  try { await engine.connect(); await body(engine, 'records', name) }
  finally {
    await engine.close()
    if (admin !== undefined) { await admin.query(`DROP SCHEMA ${quote(name)} CASCADE`); await admin.end() }
    else await rawCh(`DROP DATABASE ${quote(name)} SYNC`)
  }
}

/** Disposable restricted logins on the same isolated databases used by native generation tests. */
import { randomBytes } from 'node:crypto'
import { Client } from 'pg'
import { PostgresEngine } from '../src/engines/postgres.js'
import { ClickHouseEngine } from '../src/engines/clickhouse.js'
import { QUOTE } from '../src/schema.js'
import { fixture, type Database, type Dialect } from './_generation-engines.js'

export interface Roles {
  readonly operator: Database
  readonly runtime: Database
  readonly namespace: string
  statement(sql: string, runtime?: boolean): Promise<Record<string, unknown>[]>
  grant(table: string, revokeSelect?: boolean): Promise<void>
}

export function withRoles(dialect: Dialect, body: (roles: Roles) => Promise<void>): Promise<void> {
  return fixture(dialect, async (operator, _table, namespace) => {
    const base = new URL(process.env[dialect === 'postgres' ? 'SDE_POSTGRES_DSN' : 'SDE_CLICKHOUSE_DSN'] as string)
    if (dialect === 'postgres') base.searchParams.set('options', `-csearch_path=${namespace}`)
    else base.pathname = '/' + namespace
    const username = 'runtime_' + randomBytes(12).toString('hex'), password = randomBytes(24).toString('hex')
    const app = new URL(base); app.username = username; app.password = password
    const quote = QUOTE[dialect] as (value: string) => string
    const admin = dialect === 'postgres' ? new Client({ connectionString: base.toString() }) : undefined
    let rawRuntime: Client | undefined, runtime: Database | undefined, created = false
    async function statement(sql: string, useRuntime = false): Promise<Record<string, unknown>[]> {
      if (admin !== undefined) return (await (useRuntime ? rawRuntime as Client : admin).query(sql)).rows as Record<string, unknown>[]
      const dsn = useRuntime ? app : base
      const url = new URL(`${dsn.protocol === 'clickhouses:' ? 'https:' : 'http:'}//${dsn.host}`)
      url.searchParams.set('database', namespace)
      url.searchParams.set('wait_end_of_query', '1')
      url.searchParams.set('default_format', 'JSON')
      const response = await fetch(url, { method: 'POST', body: sql, headers: {
        Authorization: `Basic ${Buffer.from(`${decodeURIComponent(dsn.username)}:${decodeURIComponent(dsn.password)}`).toString('base64')}`,
      } })
      const text = await response.text()
      if (!response.ok) throw new Error(text)
      return text.trim() === '' ? [] : (JSON.parse(text) as { data: Record<string, unknown>[] }).data
    }
    try {
      await admin?.connect()
      if (dialect === 'postgres') {
        // The password is generated hexadecimal text, never caller-provided SQL.
        await statement(`CREATE ROLE ${quote(username)} LOGIN PASSWORD '${password}'`); created = true
        await statement(`GRANT USAGE ON SCHEMA ${quote(namespace)} TO ${quote(username)}`)
        rawRuntime = new Client({ connectionString: app.toString() }); await rawRuntime.connect()
        runtime = new PostgresEngine(app.toString())
      } else {
        await statement(`CREATE USER ${quote(username)} IDENTIFIED WITH sha256_password BY '${password}'`); created = true
        await statement(`GRANT SELECT ON system.settings TO ${quote(username)}`)
        runtime = new ClickHouseEngine(app.toString())
      }
      await runtime.connect()
      await body({ operator, runtime, namespace, statement,
        grant: async (table, revokeSelect = false) => {
          await statement(`${revokeSelect ? 'REVOKE SELECT' : 'GRANT SELECT, INSERT'} ON ` +
            `${quote(namespace)}.${quote(table)} ${revokeSelect ? 'FROM' : 'TO'} ${quote(username)}`)
        },
      })
    } finally {
      await runtime?.close(); await rawRuntime?.end()
      if (created) {
        if (dialect === 'postgres') await statement(`DROP OWNED BY ${quote(username)}`)
        await statement(`DROP ${dialect === 'postgres' ? 'ROLE' : 'USER'} ${quote(username)}`)
      }
      await admin?.end()
    }
  })
}

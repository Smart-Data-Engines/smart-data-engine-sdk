/** Python writes and independently reads the rows TypeScript migrates. Neither side may certify
 * a copy by first throwing away the digits it is supposed to compare. Real engines, both ways.
 */
import { execFileSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { resolve } from 'node:path'
import { Client } from 'pg'
import { describe, expect, it } from 'vitest'
import { backfill, buildModel, entity, loadMap, Session, T, Timestamp, verify, neutralDeclaration, VerificationRequest, verifyRecord } from '../src/index.js'
import { PostgresEngine } from '../src/engines/postgres.js'
import { ClickHouseEngine } from '../src/engines/clickhouse.js'

const PG = process.env['SDE_POSTGRES_DSN']
const CH = process.env['SDE_CLICKHOUSE_DSN']
type Dialect = 'postgres' | 'clickhouse'
type WireRow = { id: string; at: string; naive: string }
const ROWS: WireRow[] = [
  { id: '9007199254740993', at: '2026-09-12T09:30:15.123456+00:00', naive: '2026-09-12T09:30:15.123456' },
  { id: '9007199254740993', at: '2026-09-12T09:30:15.123457+00:00', naive: '2026-09-12T09:30:15.123457' },
]

function peer(action: 'read' | 'write', dialect: Dialect, table: string, rows = ROWS): WireRow[] {
  const output = execFileSync(resolve('../python/.venv/bin/python'), [
    resolve('../python/tests/timestamp_peer.py'), action, dialect, table,
  ], { input: JSON.stringify(rows), encoding: 'utf8', timeout: 30_000 })
  return action === 'read' ? JSON.parse(output) as WireRow[] : []
}

async function rawClickHouse(sql: string): Promise<string> {
  const dsn = new URL(CH as string)
  const response = await fetch(`http://${dsn.host}/?database=${encodeURIComponent(dsn.pathname.slice(1))}`, {
    method: 'POST', body: sql,
    headers: { Authorization: `Basic ${Buffer.from(`${decodeURIComponent(dsn.username)}:${decodeURIComponent(dsn.password)}`).toString('base64')}` },
  })
  const text = await response.text()
  if (!response.ok) throw new Error(text)
  return text
}

async function fixture(sourceName: Dialect, body: (context: {
  source: PostgresEngine | ClickHouseEngine
  target: PostgresEngine | ClickHouseEngine
  session: Session
  table: string
  targetName: Dialect
  raw: Client
  document: Record<string, unknown>
}) => Promise<void>): Promise<void> {
  const table = `ts_time_${randomUUID().replaceAll('-', '')}`
  const model = buildModel([entity('Event', {
    fields: { id: T.int64, at: T.timestamptz, naive: T.timestamp }, key: ['at', 'id'],
  })])
  const engines = {
    postgres: new PostgresEngine(PG as string), clickhouse: new ClickHouseEngine(CH as string),
  }
  const raw = new Client({ connectionString: PG as string })
  await raw.connect()
  await engines.postgres.connect()
  await engines.clickhouse.connect()
  const targetName: Dialect = sourceName === 'postgres' ? 'clickhouse' : 'postgres'
  const materialization = (name: Dialect) => ({
    id: `${table}@${name}`, engine: name,
    layout: { tables: { Event: table }, columns: { Event: name === 'postgres'
      ? { id: 'bigint', at: 'timestamptz', naive: 'timestamp' }
      : { id: 'Int64', at: "DateTime64(6, 'UTC')", naive: 'DateTime64(6)' } },
    },
  })
  try {
    const document = { contract: 3, map_version: 1, model_version: model.version,
      groups: { Event: { source: materialization(sourceName),
        derived: [{ ...materialization(targetName), lag_budget_ms: 30000 }],
        also_write: [materialization(targetName).id] } },
    }
    const map = loadMap(document, { model })
    for (const name of ['postgres', 'clickhouse'] as const) {
      const placement = name === sourceName ? map.groups['Event']!.source : map.groups['Event']!.derived[0]!
      await engines[name].ensureSchema(placement.layout, { keys: { Event: ['at', 'id'] } })
    }
    const session = await Session.open(model, map, engines, { projectId: '1'.repeat(32) })
    await body({ source: engines[sourceName], target: engines[targetName], session, table, targetName, raw, document })
  } finally {
    await raw.query(`DROP TABLE IF EXISTS "${table}"`)
    await rawClickHouse(`DROP TABLE IF EXISTS ${table}`)
    await engines.postgres.close()
    await engines.clickhouse.close()
    await raw.end()
  }
}

it.skipIf(!PG || !CH)('has the engines required for timestamp interoperability', () => {
  expect(PG).toBeTruthy()
  expect(CH).toBeTruthy()
})

describe.skipIf(!PG || !CH)('lossless Python / TypeScript timestamps', () => {
  for (const direction of ['postgres', 'clickhouse'] as const) {
    it(`reads Python microseconds and resumes a copy from ${direction}`, async () => {
      await fixture(direction, async ({ source, session, table, targetName, document }) => {
        peer('write', direction, table)
        const first = await source.keyRange(table, ['at', 'id'], { limit: 1 })
        expect(first[0]?.['at']).toBeInstanceOf(Timestamp)
        expect((first[0]?.['at'] as Timestamp).toISOString()).toBe('2026-09-12T09:30:15.123456Z')
        const next = await source.nthKey(table, ['at', 'id'], { position: 2 })
        expect((next?.[0] as Timestamp).toISOString()).toBe('2026-09-12T09:30:15.123457Z')
        const stopped = await backfill(session, 'Event', { chunkRows: 1, stopAfter: 1 })
        expect(stopped.rowsThisRun).toBe(1)
        expect(stopped.complete).toBe(false)
        const resumed = await backfill(session, 'Event', { chunkRows: 1, stopAfter: 2 })
        expect(resumed.rowsThisRun).toBe(1)
        expect(resumed.complete).toBe(true)
        expect(peer('read', targetName, table)).toEqual(ROWS)
        const request = VerificationRequest.fromRecord(JSON.parse(execFileSync(
          resolve('../python/.venv/bin/python'), [resolve('../python/tests/verification_peer.py')], {
            input: JSON.stringify({ model: neutralDeclaration(session.model), map: document,
              project_id: '1'.repeat(32), request_id: '2'.repeat(32), group: 'Event',
              requested_at: '2000-01-01T00:00:00Z' }),
            encoding: 'utf8', timeout: 30_000,
          },
        )))
        const report = await verify(session, 'Event', { request })
        expect(report.matched).toBe(true)
        expect(verifyRecord(report)['request']).toEqual(request.asRecord())
      })
    }, 30_000)

    it(`detects one microsecond of corruption with ${direction} as source`, async () => {
      await fixture(direction, async ({ session, table, targetName }) => {
        peer('write', direction, table)
        peer('write', targetName, table, ROWS.map((row, index) => index === 0
          ? { ...row, naive: '2026-09-12T09:30:15.123455' } : row))
        const report = await verify(session, 'Event')
        expect(report.matched).toBe(false)
        expect(report.differences).toHaveLength(1)
        expect(report.differences[0]?.columns).toEqual(['naive'])
      })
    }, 30_000)

    it(`dual-writes exact TypeScript values and Python reads both, from ${direction}`, async () => {
      await fixture(direction, async ({ session, table, raw }) => {
        for (const row of ROWS) {
          await session.save('Event', { id: BigInt(row.id), at: Timestamp.from(row.at), naive: Timestamp.from(row.naive) })
        }
        expect(peer('read', 'postgres', table)).toEqual(ROWS)
        expect(peer('read', 'clickhouse', table)).toEqual(ROWS)
        // The application's own pg client still uses its normal Date parser.
        expect((await raw.query(`SELECT at FROM "${table}" LIMIT 1`)).rows[0].at).toBeInstanceOf(Date)
      })
    }, 30_000)
  }

  it('still accepts Date inputs and treats a naive timestamp as UTC', async () => {
    await fixture('postgres', async ({ session, table }) => {
      const value = new Date('2026-09-12T09:30:15.123Z')
      await session.save('Event', { id: 1n, at: value, naive: value })
      const expected = [{ id: '1', at: '2026-09-12T09:30:15.123000+00:00', naive: '2026-09-12T09:30:15.123000' }]
      expect(peer('read', 'postgres', table)).toEqual(expected)
      expect(peer('read', 'clickhouse', table)).toEqual(expected)
    })
  }, 30_000)
})

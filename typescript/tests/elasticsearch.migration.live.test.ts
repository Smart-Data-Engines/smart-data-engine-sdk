/**
 * A group copied onto Elasticsearch and off it, from TypeScript: the adapter's half of a migration.
 *
 * Set SDE_ELASTICSEARCH_DSN to run, and SDE_POSTGRES_DSN as well for the copy between engines. The
 * marker one library records, the other reads, through the Python peer.
 */
import { execFileSync } from 'node:child_process'
import { randomBytes, randomUUID } from 'node:crypto'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  backfill, buildModel, entity, loadMap, prepareSchema, Session, T, Timestamp, verify,
  WRITE_EPOCH_COLUMN as EPOCH_COLUMN, type PhysicalLayout, type Row,
} from '../src/index.js'
import { ElasticsearchEngine, KEY_PAGE } from '../src/engines/elasticsearch.js'
import { PostgresEngine } from '../src/engines/postgres.js'
import { fencePipeline } from '../src/engines/_write-fences.js'

const DSN = process.env['SDE_ELASTICSEARCH_DSN']
const PG_DSN = process.env['SDE_POSTGRES_DSN']
const PROJECT = '0'.repeat(31) + '3'

type Raw = { request(method: string, path: string, body?: unknown, ndjson?: string): Promise<[number, unknown]> }

async function engine(body: (es: ElasticsearchEngine, created: string[], raw: Raw) => Promise<void>): Promise<void> {
  const es = new ElasticsearchEngine(DSN as string)
  await es.connect()
  const raw = es as unknown as Raw
  const created: string[] = []
  try {
    await body(es, created, raw)
  } finally {
    for (const index of created) {
      await raw.request('DELETE', `/${index}`)
      await raw.request('DELETE', `/_ingest/pipeline/${fencePipeline(index)}`)
    }
    await es.close()
  }
}

async function index(es: ElasticsearchEngine, created: string[], columns: Record<string, string>, key: string[]): Promise<string> {
  const name = 'sde_tsmig_' + randomBytes(6).toString('hex')
  created.push(name)
  const layout: PhysicalLayout = { tables: { Row: name }, columns: { Row: columns }, indexes: [], partitionBy: {} }
  await es.ensureSchema(layout, { keys: { Row: key } })
  return name
}

/** Text in code point order, which is UTF-8 byte order - what every engine sorts a key by. */
function byCodePoint(left: string, right: string): number {
  return Buffer.compare(Buffer.from(left, 'utf8'), Buffer.from(right, 'utf8'))
}

const NAMES = ['a', 'B', 'ä', 'é', 'é', '\u{1f600}', 'ｚ', '', 'a b']

describe.skipIf(DSN === undefined)('Elasticsearch in a migration, from TypeScript', () => {
  it('reads keys in code point order across pages, with both bounds, and counts its way to a row', async () => {
    await engine(async (es, created) => {
      const table = await index(es, created, { name: 'keyword', n: 'long', body: 'keyword' }, ['name', 'n'])
      const rows: Row[] = []
      for (let i = 0; i < 2 * KEY_PAGE + 345; i += 1) {
        rows.push({ name: NAMES[i % NAMES.length] as string, n: BigInt(((i * 7919) % 10007) - 5000), body: `row ${i}` })
      }
      await es.copyIn(table, rows)
      // UTF-16 order would put the emoji, a surrogate pair, before U+FF5A: the engines do not.
      const expected = [...rows].sort((a, b) =>
        byCodePoint(a['name'] as string, b['name'] as string) || ((a['n'] as bigint) < (b['n'] as bigint) ? -1 : 1))
      const keys: unknown[][] = expected.map((row) => [row['name'], row['n']])
      const at = (position: number): unknown[] => keys[position] as unknown[]
      const read = async (options: { after?: unknown[]; upto?: unknown[]; limit?: number } = {}) =>
        (await es.keyRange(table, ['name', 'n'], options)).map((row) => [row['name'], row['n']])

      expect(await es.keyRange(table, ['name', 'n'])).toEqual(expected)
      expect(await read({ after: at(KEY_PAGE - 1), limit: KEY_PAGE + 500 })).toEqual(keys.slice(KEY_PAGE, 2 * KEY_PAGE + 500))
      expect(await read({ after: at(10), upto: at(KEY_PAGE + 500) })).toEqual(keys.slice(11, KEY_PAGE + 501))
      expect(await read({ upto: at(0) })).toEqual(keys.slice(0, 1))
      expect(await read({ after: at(keys.length - 1) })).toEqual([])
      await expect(read({ after: [null, 1n] })).rejects.toThrow(/null/)
      expect(await es.nthKey(table, ['name', 'n'], { position: 1 })).toEqual(keys[0])
      expect(await es.nthKey(table, ['name', 'n'], { position: KEY_PAGE + 1 })).toEqual(keys[KEY_PAGE])
      expect(await es.nthKey(table, ['name', 'n'], { position: keys.length })).toEqual(keys[keys.length - 1])
      expect(await es.nthKey(table, ['name', 'n'], { position: keys.length + 1 })).toBeNull()
      expect(await es.count(table)).toBe(rows.length)
    })
  }, 120_000)

  it('leaves a document already there, says what it refused, and keeps the largest marker', async () => {
    await engine(async (es, created) => {
      const table = await index(es, created, { id: 'long', body: 'keyword' }, ['id'])
      await es.writeFence(table, { projectId: PROJECT }).prepare(3)
      await es.copyIn(table, [{ id: 1n, body: 'first', [EPOCH_COLUMN]: 3n }, { id: 2n, body: 'two', [EPOCH_COLUMN]: 3n }])
      await es.copyIn(table, [{ id: 1n, body: 'second', [EPOCH_COLUMN]: 3n }, { id: 3n, body: 'three', [EPOCH_COLUMN]: 3n }])
      expect(await es.get(table, { id: 1n })).toEqual({ id: 1n, body: 'first' })
      expect(await es.count(table)).toBe(3)
      expect((await es.keyRange(table, ['id'])).map((row) => row[EPOCH_COLUMN])).toEqual([3n, 3n, 3n])
      await expect(es.copyIn(table, [{ id: 4n, body: 'x', [EPOCH_COLUMN]: 2n }, { id: 5n, body: 'y', [EPOCH_COLUMN]: 3n }]))
        .rejects.toThrow(/1 were refused \(fail_processor_exception\), 0 were there already and 1 were written/)

      const copy = 'copy-' + randomUUID()
      expect(await es.backfillMarker({ materialization: copy, entity: 'Note' })).toBe(0)
      await es.recordBackfillMarker({ materialization: copy, entity: 'Note', rows: 1000 })
      await es.recordBackfillMarker({ materialization: copy, entity: 'Note', rows: 500 })
      expect(await es.backfillMarker({ materialization: copy, entity: 'Note' })).toBe(1000)
      // The other library reads the same marker from the same index.
      const python = execFileSync(resolve('../python/.venv/bin/python'), [
        resolve('../python/tests/elasticsearch_peer.py'), 'backfill-marker', 'sde_backfill_state',
      ], { input: JSON.stringify({ materialization: copy, entity: 'Note' }), encoding: 'utf8', timeout: 60_000 })
      expect(JSON.parse(python)).toEqual({ rows_copied: 1000 })
      await (es as unknown as Raw).request('POST', '/sde_backfill_state/_delete_by_query?refresh=true',
        { query: { term: { materialization: copy } } })
    })
  }, 60_000)

  it('makes every write before a barrier visible to a search before the barrier returns', async () => {
    await engine(async (es, created, raw) => {
      const table = await index(es, created, { id: 'long', body: 'keyword' }, ['id'])
      expect((await raw.request('PUT', `/${table}/_settings`, { 'index.refresh_interval': '-1' }))[0]).toBe(200)
      const fence = es.writeFence(table, { projectId: PROJECT })
      await fence.prepare(3)
      const [written] = await raw.request('PUT', `/${table}/_doc/a`, { id: 1, body: 'x', [EPOCH_COLUMN]: 3 })
      expect(written).toBeLessThan(300)
      expect(await es.count(table)).toBe(0)
      await fence.freeze(randomBytes(16).toString('hex'))
      expect(await es.count(table)).toBe(1)
      await raw.request('PUT', `/${table}/_settings`, { 'index.blocks.write': false })
    })
  }, 60_000)

  describe.skipIf(PG_DSN === undefined).each(['postgres', 'elasticsearch'])('with %s as the source', (source) => {
    it('backfills a group between the engines and verifies every value', async () => {
      const model = buildModel([entity('Note', {
        fields: {
          id: T.int64, body: T.string, at: T.timestamptz, amount: T.decimal(12, 2), ref: T.uuid,
          day: T.date, ok: T.bool, ratio: T.float32, loose: T.nullable(T.timestamp),
        },
        key: ['id'],
      })])
      const run = randomBytes(6).toString('hex')
      const pgTable = 'notes_' + run
      const esIndex = 'sde_tsmig_' + run
      const columns = {
        postgres: {
          id: 'bigint', body: 'text', at: 'timestamptz', amount: 'numeric(12,2)', ref: 'uuid', day: 'date',
          ok: 'boolean', ratio: 'real', loose: 'timestamp',
        },
        elasticsearch: {
          id: 'long', body: 'keyword', at: 'long/timestamptz', amount: 'long/decimal(12,2)', ref: 'keyword/uuid',
          day: 'date', ok: 'boolean', ratio: 'float', loose: 'long/timestamp',
        },
      }
      const material = (engineName: 'postgres' | 'elasticsearch') => ({
        id: `${engineName}-${run}`, engine: engineName,
        layout: { tables: { Note: engineName === 'postgres' ? pgTable : esIndex }, columns: { Note: columns[engineName] } },
      })
      const target = source === 'postgres' ? 'elasticsearch' : 'postgres'
      const map = (version: number, copy: boolean) => {
        const group: Record<string, unknown> = { write_epoch: 1, source: material(source as 'postgres' | 'elasticsearch') }
        if (copy) {
          group['derived'] = [{ ...material(target), lag_budget_ms: 60_000 }]
          group['also_write'] = [`${target}-${run}`]
        }
        return loadMap({ contract: 4, project_id: PROJECT, model_version: model.version, map_version: version, groups: { Note: group } }, { model })
      }
      const row = (i: number): Row => ({
        id: BigInt(i), body: `note ${i} été`,
        at: Timestamp.fromEpochMicroseconds(1_792_747_800_000_000n + BigInt(i) * 1001n),
        amount: (i / 100 - 7).toFixed(2), ref: randomUUID(), day: `2026-01-${String((i % 28) + 1).padStart(2, '0')}`,
        ok: i % 3 === 0, ratio: Math.fround(i / 7),
        loose: i % 4 === 0 ? Timestamp.fromEpochMicroseconds(-315_619_200_000_000n + BigInt(i % 60) * 1_000_000n) : null,
      })
      const es = new ElasticsearchEngine(DSN as string)
      const pg = new PostgresEngine(PG_DSN as string)
      await es.connect()
      await pg.connect()
      const engines = { postgres: pg, elasticsearch: es }
      try {
        const before = map(1, false)
        await prepareSchema(model, before, engines, { projectId: PROJECT })
        const first = await Session.open(model, before, engines, { projectId: PROJECT })
        const early = Array.from({ length: KEY_PAGE + 250 }, (_, i) => row(i + 1))
        for (let at = 0; at < early.length; at += 500) await first.saveMany('Note', early.slice(at, at + 500))
        const during = map(2, true)
        await prepareSchema(model, during, engines, { projectId: PROJECT })
        const session = await Session.open(model, during, engines, { projectId: PROJECT })
        await session.saveMany('Note', Array.from({ length: 10 }, (_, i) => row(KEY_PAGE + 251 + i)))

        const progress = await backfill(session, 'Note', { chunkRows: 400 })
        expect(progress.complete).toBe(true)
        const report = await verify(session, 'Note', { chunkRows: 400 })
        expect(report.matched, JSON.stringify(report.differences.slice(0, 3))).toBe(true)
        expect([report.rowsSource, report.rowsTarget]).toEqual([KEY_PAGE + 260, KEY_PAGE + 260])
      } finally {
        const { Client } = await import('pg')
        const client = new Client({ connectionString: PG_DSN as string })
        await client.connect()
        await client.query(`DROP TABLE IF EXISTS "${pgTable}"`)
        await client.end()
        const raw = es as unknown as Raw
        await raw.request('DELETE', `/${esIndex}`)
        await raw.request('DELETE', `/_ingest/pipeline/${fencePipeline(esIndex)}`)
        await raw.request('POST', '/sde_backfill_state/_delete_by_query?refresh=true',
          { query: { term: { materialization: `${target}-${run}` } } })
        await es.close()
        await pg.close()
      }
    }, 180_000)
  })
})

/**
 * The TypeScript Elasticsearch adapter against a server, and against the Python one.
 *
 * Set SDE_ELASTICSEARCH_DSN to run (`make es-up` prints it). Each test creates an index under a
 * fresh name and deletes it. The cross-language slice writes with one library and reads with the
 * other, both directions, through the Python peer the CI job installs.
 */
import { execFileSync } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import { buildModel, entity, loadMap, Session, T, Timestamp, type Row, type SearchPage } from '../src/index.js'
import { ElasticsearchEngine } from '../src/engines/elasticsearch.js'

const DSN = process.env['SDE_ELASTICSEARCH_DSN']
const T0 = 1_792_742_400_123_456n

const COLUMNS: Record<string, string> = {
  id: 'long', station: 'keyword', note: 'keyword', at: 'long/timestamptz', local: 'long/timestamp',
  day: 'date', ref: 'keyword/uuid', amount: 'long/decimal(18,4)', level: 'double', count: 'integer',
  ok: 'boolean',
}

function rows(): Row[] {
  const notes = [null, 'Red apple', 'e\u0301clair', '5\u212a run', '\u0130stanbul']
  return Array.from({ length: 12 }, (_, offset) => {
    const number = offset + 1
    return {
      id: BigInt(number),
      station: ['WAW', 'KRK', 'WAW', 'GDN'][number % 4] as string,
      note: notes[number % 5] ?? null,
      at: Timestamp.fromEpochMicroseconds(T0 + BigInt(number)),
      local: number % 3 === 0 ? null : Timestamp.fromEpochMicroseconds(-62135596800000000n + BigInt(number)),
      day: ['0001-01-01', '9999-12-31', '2026-10-23'][number % 3] as string,
      ref: `00000000-0000-0000-0000-${String(number).padStart(12, '0')}`,
      amount: number === 1 ? '-99999999999999.9999' : `${number}.2500`,
      level: number / 10,
      count: -(2 ** 31) + number,
      ok: number % 2 === 0,
    }
  })
}

async function fixture(body: (session: Session, engine: ElasticsearchEngine, index: string) => Promise<void>) {
  const model = buildModel([entity('Reading', {
    fields: {
      id: T.int64, station: T.string, note: T.nullable(T.string), at: T.timestamptz,
      local: T.nullable(T.timestamp), day: T.date, ref: T.uuid, amount: T.decimal(18, 4),
      level: T.float64, count: T.int32, ok: T.bool,
    },
    key: ['id'],
  })])
  const index = 'sde_ts_' + randomBytes(6).toString('hex')
  const placement = loadMap({
    contract: 3, model_version: model.version, map_version: 1,
    groups: { Reading: { source: { id: 'es', engine: 'es', layout: {
      tables: { Reading: index }, columns: { Reading: COLUMNS },
    } } } },
  }, { model })
  const engine = new ElasticsearchEngine(DSN as string)
  await engine.connect()
  try {
    await engine.ensureSchema(placement.groups['Reading']!.source.layout, { keys: { Reading: ['id'] } })
    const session = await Session.open(model, placement, { es: engine })
    await body(session, engine, index)
  } finally {
    await (engine as unknown as { request(method: string, path: string): Promise<unknown> })
      .request('DELETE', '/' + index)
    await engine.close()
  }
}

const ids = (page: SearchPage) => page.rows.map((row) => Number(row['id']))

function wire(row: Row): Record<string, unknown> {
  return Object.fromEntries(Object.entries(row).map(([name, value]) => {
    if (value instanceof Timestamp) return [name, value.toISOString().replace('Z', COLUMNS[name] === 'long/timestamp' ? '' : '+00:00')]
    if (typeof value === 'bigint' || typeof value === 'number') {
      return [name, COLUMNS[name] === 'double' ? value : String(value)]
    }
    return [name, value]
  }))
}

function peer(action: 'read' | 'write', index: string, payload: unknown): unknown {
  const output = execFileSync(resolve('../python/.venv/bin/python'), [
    resolve('../python/tests/elasticsearch_peer.py'), action, index,
  ], { input: JSON.stringify(payload), encoding: 'utf8', timeout: 60_000 })
  return action === 'read' ? JSON.parse(output) : null
}

describe.skipIf(DSN === undefined)('Elasticsearch, live', () => {
  it('returns every value as it was written', async () => {
    await fixture(async (session) => {
      const written = rows()
      await session.save('Reading', written[0]!)
      await session.saveMany('Reading', written.slice(1))
      for (const row of written) expect(await session.get('Reading', { id: row['id'] })).toEqual(row)
      expect(await session.get('Reading', { id: 999n })).toBeNull()
      expect(await session.count('Reading')).toBe(12n)
    })
  }, 60_000)

  it('pages in the contract order with nulls last, and counts what it filters', async () => {
    await fixture(async (session) => {
      const written = rows()
      await session.saveMany('Reading', written)
      for (const descending of [false, true]) {
        const present = written.filter((row) => row['note'] !== null)
          .sort((a, b) => (a['note'] as string) < (b['note'] as string) ? -1 : (a['note'] as string) > (b['note'] as string) ? 1 : Number((a['id'] as bigint) - (b['id'] as bigint)))
        const absent = written.filter((row) => row['note'] === null)
        if (descending) {
          present.reverse()
          absent.reverse()
        }
        for (const limit of [1, 5, 12]) {
          const found: number[] = []
          let after: Readonly<Row> | null = null
          for (let attempt = 0; attempt < 30; attempt += 1) {
            const page: { rows: readonly Row[]; nextAfter: Readonly<Row> | null } =
              await session.scan('Reading', { orderBy: 'note', descending, limit, after })
            found.push(...page.rows.map((row) => Number(row['id'])))
            if (page.nextAfter === null) break
            after = page.nextAfter
          }
          expect(found).toEqual([...present, ...absent].map((row) => Number(row['id'])))
        }
      }
      expect(await session.count('Reading', { where: { station: 'WAW' } })).toBe(6n)
      expect(await session.count('Reading', { where: { note: null } })).toBe(2n)
    })
  }, 120_000)

  it('reads only the rows that hold the tokens of a search', async () => {
    await fixture(async (session) => {
      await session.saveMany('Reading', rows())
      const page = await session.search('Reading', '\u00c9CLAIR')
      expect(ids(page)).toEqual([2, 7, 12])
      expect(page.examined).toBe(3)
      expect(ids(await session.search('Reading', '5k'))).toEqual([3, 8])
      // Row 4 holds both: its note is the dotted capital I's city and its station is WAW.
      expect(ids(await session.search('Reading', 'istanbul waw'))).toEqual([4])
    })
  }, 60_000)

  it('agrees with the Python library, both ways', async () => {
    await fixture(async (session, _engine, index) => {
      const written = rows()
      await session.saveMany('Reading', written.slice(0, 6))
      const read = peer('read', index, written.slice(0, 6).map((row) => ({ id: String(row['id']) })))
      expect(read).toEqual(written.slice(0, 6).map(wire))
      peer('write', index, written.slice(6).map(wire))
      for (const row of written.slice(6)) expect(await session.get('Reading', { id: row['id'] })).toEqual(row)
      expect(ids(await session.search('Reading', 'eclair \u00e9clair'))).toEqual([])
      expect(ids(await session.search('Reading', '\u00e9clair'))).toEqual([2, 7, 12])
    })
  }, 120_000)

  it('returns by key only a document that holds that key', async () => {
    await fixture(async (session, engine, index) => {
      const { documentId } = await import('../src/elasticsearch-document.js')
      const raw = engine as unknown as {
        request(method: string, path: string, body?: unknown): Promise<[number, unknown]>
      }
      const written = rows()[0]!
      await session.save('Reading', written)
      const [, found] = await raw.request('GET', `/${index}/_doc/${documentId([written['id'] as bigint])}`)
      const stray = { ...(found as { _source: Record<string, unknown> })._source, id: 999n }
      const [status] = await raw.request('PUT', `/${index}/_doc/${documentId([7n])}?refresh=wait_for`, stray)
      expect([200, 201]).toContain(status)
      expect(await session.get('Reading', { id: 7n })).toBeNull()
      expect(await session.get('Reading', { id: written['id'] })).toEqual(written)
    })
  }, 60_000)

  it('keeps no listener per request on its pooled connection', async () => {
    // A timer callback added each time a socket went back to the pool stayed attached whenever the
    // socket was reused before it fired: five warnings in one run of this file, before the fix.
    const warnings: string[] = []
    const listener = (warning: Error) => warnings.push(warning.name)
    process.on('warning', listener)
    try {
      await fixture(async (session) => {
        await session.save('Reading', rows()[0]!)
        for (let call = 0; call < 40; call += 1) await session.get('Reading', { id: 1n })
      })
      await new Promise((resolve) => setTimeout(resolve, 50))
    } finally {
      process.off('warning', listener)
    }
    expect(warnings).not.toContain('MaxListenersExceededWarning')
  }, 60_000)

  it('keeps the map watermark', async () => {
    await fixture(async (_session, engine) => {
      const raw = engine as unknown as { request(method: string, path: string): Promise<unknown> }
      await raw.request('DELETE', '/sde_map_state')
      try {
        expect(await engine.mapWatermark()).toBeNull()
        await engine.recordMapVersion(3, { modelVersion: '0'.repeat(16) })
        await engine.recordMapVersion(5, { modelVersion: '0'.repeat(16) })
        expect(await engine.mapWatermark()).toBe(5)
      } finally {
        await raw.request('DELETE', '/sde_map_state')
      }
    })
  }, 60_000)
})

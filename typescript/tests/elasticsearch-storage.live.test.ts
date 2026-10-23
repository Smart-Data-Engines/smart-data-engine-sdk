/**
 * What an API key can read of its own indexes' size, and the window a session builds from it.
 *
 * `_stats` needs the index privilege `monitor`, which nothing else asks of a runtime key. Without it
 * the answer is a 403, and a session's size is unknown, with the reason `refused`; with it the key
 * reads what the cluster's administrator reads. Numbers only - no document is read. Set
 * SDE_ELASTICSEARCH_DSN to run (measured on 9.5.5); the mirror of the reference's
 * `test_elasticsearch_storage_live.py`.
 */
import { randomBytes } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { buildModel, entity, loadMap, Recorder, Session, T, windowRecord, type Row } from '../src/index.js'
import { ElasticsearchEngine, HttpStatus } from '../src/engines/elasticsearch.js'

const DSN = process.env['SDE_ELASTICSEARCH_DSN']
/** What a runtime key holds on its indexes (runtime-roles.md), without the optional `monitor`. */
const RUNTIME = ['read', 'write', 'view_index_metadata']
const COLUMNS = { id: 'long', title: 'keyword' }

type Raw = { request(method: string, path: string, body?: unknown): Promise<[number, unknown]> }

async function administrator(): Promise<ElasticsearchEngine> {
  const engine = new ElasticsearchEngine(DSN as string)
  await engine.connect()
  return engine
}

/**
 * Cleanup on a connection of its own: one that lost a response is dropped, and a test that failed on
 * it must not leave its indexes and keys behind.
 */
async function remove(method: string, path: string, body?: unknown): Promise<void> {
  const engine = await administrator()
  try {
    await (engine as unknown as Raw).request(method, path, body)
  } finally {
    await engine.close()
  }
}

/** An adapter on an API key with `privileges` on `names`, removed afterwards. */
async function keyed<T>(names: string[], privileges: string[], body: (engine: ElasticsearchEngine) => Promise<T>): Promise<T> {
  const admin = await administrator()
  let id: string
  let encoded: string
  try {
    const [status, key] = await (admin as unknown as Raw).request('POST', '/_security/api_key', {
      name: 'sde-ts-storage-' + randomBytes(4).toString('hex'),
      role_descriptors: { runtime: { cluster: ['read_pipeline'], indices: [{ names, privileges }] } },
    })
    expect(status).toBe(200)
    ;({ id, encoded } = key as { id: string; encoded: string })
  } finally {
    await admin.close()
  }
  const url = new URL((DSN as string).replace(/^elasticsearch\+/, ''))
  const caCert = url.searchParams.get('ca_cert')
  const scoped = `elasticsearch+${url.protocol}//${url.host}/?` +
    new URLSearchParams({ ...(caCert === null ? {} : { ca_cert: caCert }), api_key: encoded }).toString()
  const engine = new ElasticsearchEngine(scoped)
  await engine.connect()
  try {
    return await body(engine)
  } finally {
    await engine.close()
    await remove('DELETE', '/_security/api_key', { ids: [id] })
  }
}

/** The map of a group of one entity in `table`. */
function noteMap(model: ReturnType<typeof buildModel>, table: string): ReturnType<typeof loadMap> {
  return loadMap({
    contract: 3, model_version: model.version, map_version: 1,
    groups: { Note: { source: { id: 'es', engine: 'es', layout: { tables: { Note: table }, columns: { Note: COLUMNS } } } } },
  }, { model })
}

/** The administrator, and a group of one entity in an index of its own: its model and map. */
async function notes(body: (admin: ElasticsearchEngine, model: ReturnType<typeof buildModel>,
  placement: ReturnType<typeof loadMap>, table: string) => Promise<void>): Promise<void> {
  const model = buildModel([entity('Note', { fields: { id: T.int64, title: T.string }, key: ['id'] })])
  const table = 'sde_ts_size_' + randomBytes(6).toString('hex')
  const placement = noteMap(model, table)
  const admin = await administrator()
  try {
    await admin.ensureSchema(placement.groups['Note']!.source.layout, { keys: { Note: ['id'] } })
    await body(admin, model, placement, table)
  } finally {
    await admin.close()
    await remove('DELETE', `/${table}*`)
  }
}

/** One segment each, so a background merge cannot change a size between two reads of it. */
async function settle(admin: ElasticsearchEngine, ...indexes: string[]): Promise<void> {
  const names = indexes.join(',')
  for (const path of [`/${names}/_forcemerge?max_num_segments=1`, `/${names}/_refresh`]) {
    const [status] = await (admin as unknown as Raw).request('POST', path)
    expect(status).toBe(200)
  }
}

function rows(start: number, count: number): Row[] {
  return Array.from({ length: count }, (_, offset) => ({ id: BigInt(start + offset), title: `note ${start + offset}` }))
}

describe.skipIf(DSN === undefined)('the store sizes on Elasticsearch, live', () => {
  it('are read by a key with monitor, and refused to one without it', async () => {
    await notes(async (admin, model, placement, filled) => {
      const empty = filled + '_empty'
      const alias = filled + '_alias'
      const missing = filled + '_missing'
      await admin.ensureSchema(noteMap(model, empty).groups['Note']!.source.layout, { keys: { Note: ['id'] } })
      const session = await Session.open(model, placement, { es: admin })
      await session.saveMany('Note', rows(0, 500))
      await settle(admin, filled, empty)
      const [status] = await (admin as unknown as Raw).request('POST', '/_aliases', {
        actions: [{ add: { index: filled, alias } }],
      })
      expect(status).toBe(200)
      const names = [filled, empty, alias, missing]
      await keyed([filled + '*'], RUNTIME, async (plain) => {
        const refused = await plain.storageSizes(names).catch((error: unknown) => error)
        expect((refused as Error).message).toMatch(/storage sizes could not be read/)
        expect((refused as Error).cause).toBeInstanceOf(HttpStatus)
        expect(((refused as Error).cause as HttpStatus).status).toBe(403)
      })
      await keyed([filled + '*'], [...RUNTIME, 'monitor'], async (monitored) => {
        const measured = await monitored.storageSizes(names)
        expect(measured).toEqual(await admin.storageSizes(names))
        // Absent, not zero: an index that does not exist, and an alias - this measures indexes,
        // never what a name resolves to.
        expect([...measured.keys()].sort()).toEqual([filled, empty].sort())
        expect(measured.get(filled)![0]).toBeGreaterThan(measured.get(empty)![0])
        expect(measured.get(filled)![1]).toBe(0)
        expect(measured.get(empty)![1]).toBe(0)
        expect(await monitored.storageSizes([])).toEqual(new Map())
      })
    })
  })

  it('reach the window a session builds', async () => {
    await notes(async (admin, model, placement, table) => {
      await keyed([table], RUNTIME, async (plain) => {
        const session = await Session.open(model, placement, { es: plain }, { recorder: new Recorder(model.version) })
        const refused = await session.measureStorage()
        expect(refused.sizes).toEqual([])
        expect(refused.unavailable).toEqual({ Note: 'refused' })
      })
      await keyed([table], [...RUNTIME, 'monitor'], async (monitored) => {
        const recorder = new Recorder(model.version)
        const session = await Session.open(model, placement, { es: monitored }, { recorder })
        for (const start of [0, 1_000]) await session.saveMany('Note', rows(start, 1_000))
        await settle(admin, table)
        const measured = await session.measureStorage()
        expect(measured.unavailable).toEqual({})
        expect(measured.sizes).toHaveLength(1)
        const size = measured.sizes[0]!
        expect([size.group, size.engine, size.materialization]).toEqual(['Note', 'es', 'es'])
        expect([size.totalBytes, size.secondaryIndexBytes]).toEqual((await admin.storageSizes([table])).get(table))
        expect(size.totalBytes).toBeGreaterThan(0)
        const body = (windowRecord(recorder.roll()!, model)['groups'] as Record<string, Record<string, unknown>>)['Note']!
        expect(body['total_bytes']).toBe(size.totalBytes)
        // No secondary index beside the store, measured: a ratio of 0, not an unknown.
        expect(body['index_to_table_ratio']).toBe(0)
        expect(body['missing']).not.toContain('total_bytes')
        expect(body['missing']).not.toContain('index_to_table_ratio')
      })
    })
  })
})

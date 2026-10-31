/**
 * The write-fence protocol on a live Elasticsearch, from TypeScript (`docs/write-fences.md`).
 *
 * Set SDE_ELASTICSEARCH_DSN to run, as for the adapter's other live tests. The cross-language case
 * provisions a fence with one library and reads it with the other, through the Python peer.
 */
import { execFileSync } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  buildModel, entity, loadMap, MigrationRefused, prepareSchema, Session, T, WRITE_EPOCH_COLUMN as EPOCH_COLUMN,
  type PhysicalLayout,
} from '../src/index.js'
import { ElasticsearchEngine } from '../src/engines/elasticsearch.js'
import { fencePipeline } from '../src/engines/_write-fences.js'

const DSN = process.env['SDE_ELASTICSEARCH_DSN']
const PROJECT = '0'.repeat(31) + '1'

type Raw = { request(method: string, path: string, body?: unknown, ndjson?: string): Promise<[number, unknown]> }

async function fresh(body: (engine: ElasticsearchEngine, index: string, raw: Raw) => Promise<void>) {
  const engine = new ElasticsearchEngine(DSN as string)
  await engine.connect()
  const raw = engine as unknown as Raw
  const index = 'sde_ts_fence_' + randomBytes(6).toString('hex')
  const layout: PhysicalLayout = {
    tables: { Note: index }, columns: { Note: { id: 'long', body: 'keyword' } }, indexes: [], partitionBy: {},
  }
  try {
    await engine.ensureSchema(layout, { keys: { Note: ['id'] } })
    await body(engine, index, raw)
  } finally {
    await raw.request('PUT', `/${index}/_settings`, { 'index.blocks.write': false })
    await raw.request('DELETE', `/${index}`)
    await raw.request('DELETE', `/_ingest/pipeline/${fencePipeline(index)}`)
    await engine.close()
  }
}

async function put(raw: Raw, index: string, id: string, document: Record<string, unknown>): Promise<number> {
  const [status] = await raw.request('PUT', `/${index}/_doc/${id}?refresh=wait_for`, document)
  return status
}

async function count(raw: Raw, index: string): Promise<number> {
  await raw.request('POST', `/${index}/_refresh`)
  const [, body] = await raw.request('GET', `/${index}/_count`)
  return Number((body as { count: number | bigint }).count)
}

function peer(action: string, index: string, payload: unknown): Record<string, unknown> {
  const output = execFileSync(resolve('../python/.venv/bin/python'), [
    resolve('../python/tests/elasticsearch_peer.py'), action, index,
  ], { input: JSON.stringify(payload), encoding: 'utf8', timeout: 60_000 })
  return JSON.parse(output) as Record<string, unknown>
}

describe.skipIf(DSN === undefined)('the Elasticsearch write fence, live', () => {
  it('refuses a write outside the generation, and no request bypasses it', async () => {
    await fresh(async (engine, index, raw) => {
      const fence = engine.writeFence(index, { projectId: PROJECT })
      const state = await fence.prepare(3)
      expect([state.epoch, state.lowerEpoch, state.upperEpoch, state.closed]).toEqual([3, 3, 3, false])
      expect(await put(raw, index, 'a', { id: 1, body: 'x', [EPOCH_COLUMN]: 3 })).toBeLessThan(300)
      for (const epoch of [2, 4, null, '3', 3.5]) {
        const document: Record<string, unknown> = { id: 2, body: 'x' }
        if (epoch !== null) document[EPOCH_COLUMN] = epoch
        expect(await put(raw, index, 'b', document), String(epoch)).toBeGreaterThanOrEqual(400)
      }
      const [bypass] = await raw.request('PUT', `/${index}/_doc/c?pipeline=_none&refresh=wait_for`,
        { id: 3, body: 'x', [EPOCH_COLUMN]: 1 })
      expect(bypass).toBeGreaterThanOrEqual(400)
      expect(await count(raw, index)).toBe(1)
      await expect(fence.prepare(4)).rejects.toThrow(/cannot change an existing write epoch/)
    })
  }, 60_000)

  it('holds a barrier until it is released, moves the generation under it, and never reuses an id', async () => {
    await fresh(async (engine, index, raw) => {
      const fence = engine.writeFence(index, { projectId: PROJECT })
      await fence.prepare(3)
      const lines: string[] = []
      for (let n = 0; n < 60_000; n += 1) {
        lines.push(JSON.stringify({ index: { _id: String(n) } }), JSON.stringify({ id: n, body: 'x'.repeat(30), [EPOCH_COLUMN]: 3 }))
      }
      const writer = new ElasticsearchEngine(DSN as string)
      await writer.connect()
      const bulk = (writer as unknown as Raw).request('POST', `/${index}/_bulk`, undefined, lines.join('\n') + '\n')
      await new Promise((done) => setTimeout(done, 500))
      const hold = randomBytes(16).toString('hex')
      let state = await fence.freeze(hold)
      const atBarrier = await count(raw, index)
      await bulk
      await writer.close()
      expect(state.closed).toBe(true)
      await new Promise((done) => setTimeout(done, 1000))
      expect(await count(raw, index)).toBe(atBarrier)
      expect(await put(raw, index, 'late', { id: -1, body: 'x', [EPOCH_COLUMN]: 3 })).toBeGreaterThanOrEqual(400)

      state = await fence.advance(4)
      expect([state.lowerEpoch, state.upperEpoch, state.closed]).toEqual([4, 4, true])
      state = await fence.release(hold)
      expect([state.closed, state.holds, state.retired]).toEqual([false, [], [hold]])
      expect(await put(raw, index, 'old', { id: -2, body: 'x', [EPOCH_COLUMN]: 3 })).toBeGreaterThanOrEqual(400)
      expect(await put(raw, index, 'new', { id: -3, body: 'x', [EPOCH_COLUMN]: 4 })).toBeLessThan(300)
      await expect(fence.freeze(hold)).rejects.toThrow(MigrationRefused)
    })
  }, 120_000)

  it('reads a block that outlived its hold as an interrupted setup, and resumes it', async () => {
    await fresh(async (engine, index, raw) => {
      const fence = engine.writeFence(index, { projectId: PROJECT })
      await fence.prepare(5)
      const [blocked] = await raw.request('PUT', `/${index}/_block/write`)
      expect(blocked).toBe(200)
      const state = await fence.state()
      expect([state.closed, state.holds]).toEqual([true, ['setup']])
      const resumed = await fence.resumePrepare(5)
      expect([resumed.epoch, resumed.closed]).toEqual([5, false])
    })
  }, 60_000)

  it('refuses a block or a pipeline that is not this library\'s, and takes neither over', async () => {
    await fresh(async (engine, index, raw) => {
      expect((await raw.request('PUT', `/${index}/_block/write`))[0]).toBe(200)
      await expect(engine.writeFence(index, { projectId: PROJECT }).state()).rejects.toThrow(/somebody else blocked it/)
      await raw.request('PUT', `/${index}/_settings`, { 'index.blocks.write': false })
      const fence = engine.writeFence(index, { projectId: PROJECT })
      await fence.prepare(2)
      const [, body] = await raw.request('GET', `/_ingest/pipeline/${fencePipeline(index)}`)
      const pipeline = Object.fromEntries(Object.entries((body as Record<string, Record<string, unknown>>)[fencePipeline(index)] ?? {})
        .filter(([key]) => !key.endsWith('_date_millis'))) as { processors: { fail: { if: string } }[] }
      pipeline.processors[0]!.fail.if = 'false'
      expect((await raw.request('PUT', `/_ingest/pipeline/${fencePipeline(index)}`, pipeline))[0]).toBe(200)
      await expect(fence.state()).rejects.toThrow(/changed outside this library/)
    })
  }, 60_000)

  it('reads a fence the Python library provisioned, and provisions one Python reads', async () => {
    await fresh(async (engine, index) => {
      const provisioned = peer('fence-prepare', index, { project_id: PROJECT, epoch: 9 })
      expect([provisioned['lower_epoch'], provisioned['closed']]).toEqual([9, false])
      const state = await engine.writeFence(index, { projectId: PROJECT }).state()
      expect(state.asRecord()).toEqual(provisioned)
    })
    await fresh(async (engine, index) => {
      const state = await engine.writeFence(index, { projectId: PROJECT }).prepare(11)
      expect(peer('fence-state', index, { project_id: PROJECT })).toEqual(state.asRecord())
    })
  }, 120_000)

  it('stamps a session\'s generation on every write and reads the rows without it', async () => {
    await fresh(async (engine, index, raw) => {
      const model = buildModel([entity('Note', { fields: { id: T.int64, body: T.string }, key: ['id'] })])
      const document = (group: Record<string, unknown>) => ({
        contract: 6, model_version: model.version, map_version: 1, project_id: PROJECT,
        groups: { Note: { ...group, source: { id: 'es', engine: 'es', layout: {
          tables: { Note: index }, columns: { Note: { id: 'long', body: 'keyword' } },
        } } } },
      })
      const placement = loadMap(document({ write_epoch: 7 }), { model })
      await prepareSchema(model, placement, { es: engine }, { projectId: PROJECT })
      const session = await Session.open(model, placement, { es: engine }, { projectId: PROJECT })
      await session.save('Note', { id: 1n, body: 'hello' })
      expect(await session.get('Note', { id: 1n })).toEqual({ id: 1n, body: 'hello' })
      const [, found] = await raw.request('GET', `/${index}/_search`, { query: { match_all: {} } })
      const hit = (found as { hits: { hits: { _source: Record<string, unknown> }[] } }).hits.hits[0]
      expect(Number(hit?._source[EPOCH_COLUMN])).toBe(7)
      expect(await put(raw, index, 'raw', { id: 2, body: 'x' })).toBeGreaterThanOrEqual(400)
      // A contract-6 map that leaves the generation out is refused for an engine with fences.
      const unfenced = loadMap(document({}), { model })
      await expect(Session.open(model, unfenced, { es: engine }, { projectId: PROJECT })).rejects.toThrow(MigrationRefused)
    })
  }, 60_000)

  type Backend = {
    call(method: string, path: string, body?: unknown): Promise<[number, unknown]>
    pipeline(table: string): Promise<[Record<string, unknown> | null, number | null]>
  }
  const backendOf = (fence: unknown) => (fence as { backend: Backend }).backend

  /** A request of the last hold's release: the pipeline written without a hold, or the block lifted. */
  function opensTheIndex(method: string, path: string, body: unknown): boolean {
    if (method !== 'PUT' || body === null || typeof body !== 'object') return false
    if (path.startsWith('/_ingest/pipeline/')) {
      const constraints = (body as { _meta?: { sde?: { constraints?: Record<string, string> } } })._meta?.sde?.constraints ?? {}
      return !Object.keys(constraints).some((name) => name.startsWith('__sde_f_hold_'))
    }
    return (body as Record<string, unknown>)['index.blocks.write'] === false
  }

  it('finishes a release interrupted between its two requests on a retry', async () => {
    // The last hold's release makes two requests. The first is made and its response lost: the
    // process is gone before the second. The block goes first now, so what is left is the hold -
    // still refusing every write a runtime key can make - and a retry removes it. When the pipeline
    // went first, what was left was a block with no hold to say whose it was, which no retry lifted
    // (second review).
    await fresh(async (engine, index, raw) => {
      const fence = engine.writeFence(index, { projectId: PROJECT })
      await fence.prepare(3)
      const hold = randomBytes(16).toString('hex')
      await fence.freeze(hold)
      const backend = backendOf(fence)
      const call = backend.call.bind(backend)
      let lost = false
      backend.call = async (method, path, body) => {
        const answer = await call(method, path, body)
        if (!lost && opensTheIndex(method, path, body)) {
          lost = true
          throw new Error('the response was lost')
        }
        return answer
      }
      await expect(fence.release(hold)).rejects.toThrow(/lost/)
      expect(lost).toBe(true)
      backend.call = call
      let state = await fence.state()
      expect([state.closed, state.holds]).toEqual([true, [hold]])
      expect(await put(raw, index, 'late', { id: 1, body: 'x', [EPOCH_COLUMN]: 3 })).toBeGreaterThanOrEqual(400)
      state = await fence.release(hold)
      expect([state.closed, state.holds, state.retired]).toEqual([false, [], [hold]])
      expect(await put(raw, index, 'open', { id: 2, body: 'x', [EPOCH_COLUMN]: 3 })).toBeLessThan(300)
    })
  }, 60_000)

  /** The write block in `index`'s settings: a lifted one stays there as `false`. */
  async function writeBlock(raw: Raw, index: string): Promise<string> {
    const [status, body] = await raw.request('GET', `/${index}/_settings`)
    expect(status).toBe(200)
    const settings = (body as Record<string, { settings: { index: { blocks?: { write?: unknown } } } }>)[index]!
    return String(settings.settings.index.blocks?.write ?? 'false')
  }

  it('keeps the block while another hold does', async () => {
    // Two holds, released one at a time: the block goes with the last of them and never earlier, so
    // a write that passed an older pipeline on its way is refused at the shard while any hold is there.
    await fresh(async (engine, index, raw) => {
      const fence = engine.writeFence(index, { projectId: PROJECT })
      await fence.prepare(3)
      const [first, second] = [randomBytes(16).toString('hex'), randomBytes(16).toString('hex')]
      await fence.freeze(first)
      await fence.freeze(second)
      let state = await fence.release(first)
      expect([state.closed, state.holds]).toEqual([true, [second]])
      expect(await writeBlock(raw, index)).toBe('true')
      state = await fence.release(second)
      expect([state.closed, state.holds]).toEqual([false, []])
      expect(await writeBlock(raw, index)).toBe('false')
      expect(await put(raw, index, 'open', { id: 1, body: 'x', [EPOCH_COLUMN]: 3 })).toBeLessThan(300)
    })
  }, 60_000)

  it('reads a pipeline that is not the index\'s final one as an interrupted setup', async () => {
    await fresh(async (engine, index, raw) => {
      const fence = engine.writeFence(index, { projectId: PROJECT })
      await fence.prepare(3)
      const [unset] = await raw.request('PUT', `/${index}/_settings`, { 'index.final_pipeline': '_none' })
      expect(unset).toBe(200)
      expect(await put(raw, index, 'stale', { id: 1, body: 'x', [EPOCH_COLUMN]: 2 })).toBeLessThan(300)
      const state = await fence.state()
      expect([state.closed, state.holds]).toEqual([true, ['setup']])
      const resumed = await fence.resumePrepare(3)
      expect([resumed.complete, resumed.epoch, resumed.closed]).toEqual([true, 3, false])
      expect(await put(raw, index, 'stale2', { id: 2, body: 'x', [EPOCH_COLUMN]: 2 })).toBeGreaterThanOrEqual(400)
    })
  }, 60_000)

  it('refuses a pipeline-level on_failure', async () => {
    await fresh(async (engine, index, raw) => {
      const fence = engine.writeFence(index, { projectId: PROJECT })
      await fence.prepare(3)
      const name = fencePipeline(index)
      const [, current] = await raw.request('GET', `/_ingest/pipeline/${name}`)
      const kept = Object.fromEntries(Object.entries((current as Record<string, Record<string, unknown>>)[name]!)
        .filter(([key]) => ['description', 'version', '_meta', 'processors'].includes(key)))
      const [rewritten] = await raw.request('PUT', `/_ingest/pipeline/${name}`, {
        ...kept, on_failure: [{ set: { field: 'body', value: 'admitted' } }],
      })
      expect(rewritten).toBe(200)
      await expect(fence.state()).rejects.toThrow(/on_failure/)
    })
  }, 60_000)

  it('refuses a pipeline another executor moved by name', async () => {
    // Elasticsearch answers a conditional pipeline write that lost the race with a 400 reading
    // "version conflict" (measured on 9.5.5): refused as another executor's, not a failed request.
    await fresh(async (engine, index) => {
      const fence = engine.writeFence(index, { projectId: PROJECT })
      await fence.prepare(3)
      const backend = backendOf(fence)
      const pipeline = backend.pipeline.bind(backend)
      backend.pipeline = async (table) => {
        const [document, version] = await pipeline(table)
        return [document, (version ?? 0) - 1]
      }
      await expect(fence.freeze(randomBytes(16).toString('hex'))).rejects.toThrow(/another executor/)
    })
  }, 60_000)
})


/**
 * A separate Python operator moves a group from PostgreSQL onto Elasticsearch while this TypeScript
 * application keeps an old map, with one of its fan-out writes to Elasticsearch delayed across the
 * switch. The delayed write carries the old generation and is refused by the index's fence, so the
 * newer value written through the new map survives - where an overwrite would have put it back.
 */
import { execFileSync } from 'node:child_process'
import { generateKeyPairSync, randomBytes, randomUUID, sign } from 'node:crypto'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { expect, it } from 'vitest'
import { buildModel, canonicalBytes, entity, loadMap, neutralDeclaration, prepareSchema, Session, T,
  verificationRequest, WATERMARK_TABLE } from '../src/index.js'
import { ElasticsearchEngine } from '../src/engines/elasticsearch.js'
import { fencePipeline } from '../src/engines/_write-fences.js'
import { withRoles } from './_runtime-roles.js'

const ES_DSN = process.env['SDE_ELASTICSEARCH_DSN']
const LIVE = process.env['SDE_POSTGRES_DSN'] !== undefined && ES_DSN !== undefined

type Raw = { request(method: string, path: string, body?: unknown): Promise<[number, unknown]> }

it.skipIf(!LIVE)('survives a Python move onto Elasticsearch with a delayed old TypeScript fan-out', async () => {
  const directory = mkdtempSync(join(process.env['SDE_TEST_TMPDIR'] ?? process.env['RUNNER_TEMP'] ?? tmpdir(), 'sde-cutover-'))
  const admin = new ElasticsearchEngine(ES_DSN as string)
  await admin.connect()
  const raw = admin as unknown as Raw
  const index = 'sde_tsop_' + randomBytes(6).toString('hex')
  const [created, key] = await raw.request('POST', '/_security/api_key', {
    name: 'sde-ts-cutover-' + randomBytes(4).toString('hex'),
    role_descriptors: { runtime: { cluster: ['read_pipeline'], indices: [{
      names: [index, WATERMARK_TABLE], privileges: ['read', 'write', 'view_index_metadata'] }] } },
  })
  expect(created).toBe(200)
  const { id: keyId, encoded } = key as { id: string; encoded: string }
  const url = new URL((ES_DSN as string).replace(/^elasticsearch\+/, ''))
  const caCert = url.searchParams.get('ca_cert')
  const runtimeDsn = `elasticsearch+${url.protocol}//${url.host}/?` +
    new URLSearchParams({ ...(caCert === null ? {} : { ca_cert: caCert }), api_key: encoded }).toString()
  const esRuntime = new ElasticsearchEngine(runtimeDsn)
  await esRuntime.connect()
  try {
    await withRoles('postgres', async pg => {
      const keys = generateKeyPairSync('ed25519'), projectId = '1'.repeat(32)
      const publicKey = keys.publicKey.export({ format: 'der', type: 'spki' }).subarray(-32)
      const model = buildModel([entity('Event', { fields: { id: T.int64, value: T.int32 }, key: ['id'] })])
      function signed(input: Record<string, any>): Record<string, any> {
        const document = structuredClone(input); delete document['signature']
        document['signature'] = { alg: 'ed25519', value: sign(null, canonicalBytes(document), keys.privateKey).toString('base64') }
        return document
      }
      // The cluster's map watermark is shared by every test on it, so versions start from the clock.
      const base = Date.now()
      const source = { id: 'source', engine: 'postgres', layout: { tables: { Event: 'source_events' }, columns: { Event: { id: 'bigint', value: 'integer' } } } }
      const copy = { id: 'copy', engine: 'search', layout: { tables: { Event: index }, columns: { Event: { id: 'long', value: 'integer' } } } }
      const before = signed({ contract: 4, project_id: projectId, model_version: model.version, map_version: base,
        groups: { Event: { write_epoch: 1, source, derived: [{ ...copy, lag_budget_ms: 60000 }], also_write: ['copy'] } } })
      const success = signed({ ...before, map_version: base + 1, groups: { Event: { write_epoch: 3, source: copy } } })
      const abort = signed({ ...before, map_version: base + 2, groups: { Event: { write_epoch: 2, source } } })
      const placement = loadMap(before, { model, publicKey })
      const request = verificationRequest(placement, { projectId, group: 'Event', requestId: randomUUID().replaceAll('-', ''), requestedAt: new Date().toISOString() })
      const packet = signed({ kind: 'sde-cutover', protocol: 1, plan_id: randomUUID().replaceAll('-', ''),
        project_id: projectId, group: 'Event', before, success, abort, verification: request.asRecord(),
        query_impact_digest: 'a'.repeat(64), pause_budget_ms: 30000 })
      const operators = { postgres: pg.operator, search: admin }, runtime = { postgres: pg.runtime, search: esRuntime }
      await prepareSchema(model, placement, operators, { projectId })
      await pg.grant('source_events'); await pg.grant(WATERMARK_TABLE)
      const old = await Session.open(model, placement, runtime, { projectId })
      await old.save('Event', { id: 1n, value: 11 })
      let release: () => void = () => {}, reached: () => void = () => {}
      const gate = new Promise<void>(resolve => { release = resolve })
      const waiting = new Promise<void>(resolve => { reached = resolve })
      const actual = esRuntime.insert.bind(esRuntime)
      esRuntime.insert = async (table, values) => {
        if (values['id'] === 4n) { reached(); await gate }
        await actual(table, values)
      }
      const late = old.save('Event', { id: 4n, value: 44 })
      try {
        await waiting
        const output = execFileSync(resolve('../python/.venv/bin/python'), [resolve('../python/tests/_cutover_driver.py')], {
          input: JSON.stringify({ directory, model: neutralDeclaration(model), project_id: projectId,
            public_key: publicKey.toString('hex'), plan: packet,
            dialects: { postgres: 'postgres', search: 'elasticsearch' },
            operators: { postgres: pg.operatorDsn, search: ES_DSN },
            runtime: { postgres: pg.runtimeDsn, search: runtimeDsn } }), encoding: 'utf8', timeout: 60000,
        })
        const receipt = JSON.parse(output) as { outcome: string; verification: { matched: boolean } }
        expect(receipt.outcome).toBe('success'); expect(receipt.verification.matched).toBe(true)
        const currentMap = loadMap(JSON.parse(readFileSync(join(directory, 'active-map.json'), 'utf8')), { model, publicKey })
        const current = await Session.open(model, currentMap, runtime, { projectId })
        esRuntime.insert = actual
        await current.save('Event', { id: 4n, value: 99 })
        release(); await late
        expect(await current.get('Event', { id: 4n })).toEqual({ id: 4n, value: 99 })
        expect(await current.get('Event', { id: 1n })).toEqual({ id: 1n, value: 11 })
        await expect(old.get('Event', { id: 1n })).rejects.toThrow()
        await expect(old.save('Event', { id: 5n, value: 55 })).rejects.toThrow()
        await current.save('Event', { id: 6n, value: 66 })
        expect(await current.get('Event', { id: 6n })).toEqual({ id: 6n, value: 66 })
      } finally {
        esRuntime.insert = actual; release(); await late.catch(() => {})
      }
    })
  } finally {
    await esRuntime.close()
    await raw.request('DELETE', '/_security/api_key', { ids: [keyId] })
    await raw.request('DELETE', `/${index}`)
    await raw.request('DELETE', `/_ingest/pipeline/${fencePipeline(index)}`)
    await admin.close()
    rmSync(directory, { recursive: true, force: true })
  }
}, 120000)

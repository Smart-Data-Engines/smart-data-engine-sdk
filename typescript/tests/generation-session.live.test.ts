/** Active sessions sharing one connection must not share a mutable write generation. */
import { generateKeyPairSync, sign } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { buildModel, canonicalBytes, entity, loadMap, prepareSchema, Session, T, WRITE_EPOCH_COLUMN } from '../src/index.js'
import { fixture, type Dialect } from './_generation-engines.js'
import { QUOTE } from '../src/schema.js'
import { Client } from 'pg'

const PG = process.env['SDE_POSTGRES_DSN'], CH = process.env['SDE_CLICKHOUSE_DSN']
const PROJECT = '1'.repeat(32), HOLD = '4'.repeat(32)


function document(dialect: Dialect, table: string, epoch = 1, version = 1, signed = false, withLabel = false) {
  const model = buildModel([entity('Record', { fields: { id: T.int64, ...(withLabel ? { label: T.string } : {}) }, key: ['id'] })])
  const raw: Record<string, unknown> = { contract: 4, project_id: PROJECT, model_version: model.version,
    map_version: version, groups: { Record: { write_epoch: epoch, source: { id: 'source', engine: 'db',
      layout: { tables: { Record: table }, columns: { Record: { id: dialect === 'postgres' ? 'bigint' : 'Int64', ...(withLabel ? { label: dialect === 'postgres' ? 'text' : 'String' } : {}) } } } } } } }
  let publicKey: Buffer | undefined
  if (signed) {
    const pair = generateKeyPairSync('ed25519')
    raw['signature'] = { alg: 'ed25519', value: sign(null, canonicalBytes(raw), pair.privateKey).toString('base64') }
    publicKey = pair.publicKey.export({ format: 'der', type: 'spki' }).subarray(-32)
  }
  return { model, placement: loadMap(raw, { model, ...(publicKey === undefined ? {} : { publicKey }) }) }
}

describe.each(['postgres', 'clickhouse'] as const)('generation-bound sessions on %s', (dialect) => {
  const live = dialect === 'postgres' ? PG !== undefined : CH !== undefined
  it.skipIf(!live)('keeps an old session old after a new one opens on the same engine', async () => {
    await fixture(dialect, async (engine, table) => {
      const first = document(dialect, table)
      await prepareSchema(first.model, first.placement, { db: engine }, { projectId: PROJECT })
      const old = await Session.open(first.model, first.placement, { db: engine }, { projectId: PROJECT })
      await old.save('Record', { id: 1n })
      expect(await old.get('Record', { id: 1n })).toEqual({ id: 1n })
      expect((await engine.get(table, { id: 1n }))?.[WRITE_EPOCH_COLUMN]).toBe(1n)
      const fence = engine.writeFence(table, { projectId: PROJECT })
      await fence.freeze(HOLD); await fence.advance(2); await fence.release(HOLD)
      const second = document(dialect, table, 2, 2)
      const current = await Session.open(second.model, second.placement, { db: engine }, { projectId: PROJECT })
      await expect(old.save('Record', { id: 2n })).rejects.toThrow()
      await current.save('Record', { id: 3n })
      expect(await current.get('Record', { id: 3n })).toEqual({ id: 3n })
      expect(await current.get('Record', { id: 1n })).toEqual({ id: 1n })
      expect((await engine.get(table, { id: 3n }))?.[WRITE_EPOCH_COLUMN]).toBe(2n)
      expect(await engine.count(table)).toBe(2)
    })
  })
  it.skipIf(!live)('refuses a future generation before recording its signed-map watermark', async () => {
    await fixture(dialect, async (engine, table) => {
      const first = document(dialect, table, 1, 1, true)
      await prepareSchema(first.model, first.placement, { db: engine }, { projectId: PROJECT })
      await Session.open(first.model, first.placement, { db: engine }, { projectId: PROJECT })
      expect(await engine.mapWatermark()).toBe(1)
      const next = document(dialect, table, 2, 2, true)
      await expect(Session.open(next.model, next.placement, { db: engine }, { projectId: PROJECT })).rejects.toThrow('write generation')
      expect(await engine.mapWatermark()).toBe(1)
      await expect(Session.open(first.model, first.placement, { db: engine }, { projectId: '9'.repeat(32) })).rejects.toThrow('locally configured')
      expect(await engine.mapWatermark()).toBe(1)
    })
  })
  it.skipIf(!live)('does not accept an application-supplied epoch or a copied map', async () => {
    await fixture(dialect, async (engine, table) => {
      const first = document(dialect, table)
      await prepareSchema(first.model, first.placement, { db: engine }, { projectId: PROJECT })
      const session = await Session.open(first.model, first.placement, { db: engine }, { projectId: PROJECT })
      await session.ensureSchema()
      await expect(session.save('Record', { id: 1n, [WRITE_EPOCH_COLUMN]: 1 })).rejects.toThrow('reserved')
      await expect(Session.open(first.model, { ...first.placement }, { db: engine }, { projectId: PROJECT })).rejects.toThrow('immutable loaded')
      expect(await engine.count(table)).toBe(0)
    })
  })
  it.skipIf(!live)('does not let a valid epoch hide changed model columns', async () => {
    await fixture(dialect, async (engine, table, namespace) => {
      const first = document(dialect, table, 1, 1, false, true)
      await prepareSchema(first.model, first.placement, { db: engine }, { projectId: PROJECT })
      const quote = QUOTE[dialect] as (name: string) => string
      const statement = `ALTER TABLE ${quote(namespace)}.${quote(table)} RENAME COLUMN label TO other_label`
      if (dialect === 'postgres') {
        const raw = new Client({ connectionString: PG as string }); await raw.connect()
        try { await raw.query(statement) } finally { await raw.end() }
      } else {
        const url = new URL(CH as string)
        const response = await fetch(`http://${url.host}/?wait_end_of_query=1`, { method: 'POST', body: statement,
          headers: { Authorization: `Basic ${Buffer.from(`${decodeURIComponent(url.username)}:${decodeURIComponent(url.password)}`).toString('base64')}` } })
        expect(response.ok).toBe(true)
      }
      await expect(Session.open(first.model, first.placement, { db: engine }, { projectId: PROJECT })).rejects.toThrow('different shape')
    })
  })

})

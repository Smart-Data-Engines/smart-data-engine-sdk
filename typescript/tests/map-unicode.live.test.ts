/** Valid canonical identifiers must remain usable on the physical engine. */
import { generateKeyPairSync, sign } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { buildModel, canonicalBytes, entity, loadMap, prepareSchema, Session, T, WATERMARK_TABLE } from '../src/index.js'
import { withRoles } from './_runtime-roles.js'

describe.each(['postgres', 'clickhouse'] as const)('Unicode table on %s', dialect => {
  const live = process.env[dialect === 'postgres' ? 'SDE_POSTGRES_DSN' : 'SDE_CLICKHOUSE_DSN'] !== undefined
  it.skipIf(!live)('uses signed NFC and astral table names with restricted runtime credentials', async () => {
    await withRoles(dialect, async roles => {
      const table = 'caf\u00e9_\u{1f6f0}', key = generateKeyPairSync('ed25519')
      const model = buildModel([entity('Record', { fields: { id: T.int64 }, key: ['id'] })])
      const raw: Record<string, unknown> = { contract: 4, project_id: '1'.repeat(32), model_version: model.version, map_version: 1,
        groups: { Record: { write_epoch: 1, source: { id: 'source', engine: 'db', layout: {
          tables: { Record: table }, columns: { Record: { id: dialect === 'postgres' ? 'bigint' : 'Int64' } },
        } } } } }
      raw['signature'] = { alg: 'ed25519', value: sign(null, canonicalBytes(raw), key.privateKey).toString('base64') }
      const publicKey = key.publicKey.export({ format: 'der', type: 'spki' }).subarray(-32)
      const placement = loadMap(raw, { model, publicKey })
      await prepareSchema(model, placement, { db: roles.operator }, { projectId: '1'.repeat(32) })
      await roles.grant(table); await roles.grant(WATERMARK_TABLE)
      const session = await Session.open(model, placement, { db: roles.runtime }, { projectId: '1'.repeat(32) })
      await session.save('Record', { id: 42n })
      expect(await session.get('Record', { id: 42n })).toEqual({ id: 42n })
      expect(await roles.operator.count(table)).toBe(1)
      expect(placement.groups['Record']?.source.layout.tables['Record']).toBe(table)
    })
  })
})

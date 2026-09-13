/** The application gets table access, while provisioning owns DDL and native fence changes. */
import { generateKeyPairSync, sign } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { buildModel, canonicalBytes, entity, loadMap, prepareSchema, Session, T, WATERMARK_TABLE } from '../src/index.js'
import { QUOTE } from '../src/schema.js'
import { type Dialect } from './_generation-engines.js'
import { withRoles, type Roles } from './_runtime-roles.js'

const PROJECT = '1'.repeat(32)
function document(dialect: Dialect, signed = true) {
  const model = buildModel([entity('Event', { fields: { id: T.int64 }, key: ['id'] })])
  const raw: Record<string, unknown> = { contract: 4, project_id: PROJECT, model_version: model.version,
    map_version: 7, groups: { Event: { write_epoch: 1, source: { id: 'source', engine: 'db',
      layout: { tables: { Event: 'events' }, columns: { Event: { id: dialect === 'postgres' ? 'bigint' : 'Int64' } } } } } } }
  let publicKey: Buffer | undefined
  if (signed) {
    const pair = generateKeyPairSync('ed25519')
    raw['signature'] = { alg: 'ed25519', value: sign(null, canonicalBytes(raw), pair.privateKey).toString('base64') }
    publicKey = pair.publicKey.export({ format: 'der', type: 'spki' }).subarray(-32)
  }
  return { model, placement: loadMap(raw, { model, ...(publicKey === undefined ? {} : { publicKey }) }) }
}
async function metadataRows(roles: Roles, dialect: Dialect): Promise<number[]> {
  const quote = QUOTE[dialect] as (value: string) => string
  const rows = await roles.statement(`SELECT map_version FROM ${quote(WATERMARK_TABLE)}`)
  return rows.map(row => Number(row['map_version']))
}
async function exists(roles: Roles, dialect: Dialect): Promise<boolean> {
  const rows = await roles.statement(dialect === 'postgres' ? `SELECT to_regclass('${WATERMARK_TABLE}') AS relation` : `EXISTS TABLE \`${WATERMARK_TABLE}\``)
  return dialect === 'postgres' ? rows[0]?.['relation'] !== null : Number(rows[0]?.['result']) === 1
}

describe.each(['postgres', 'clickhouse'] as const)('restricted runtime on %s', dialect => {
  const live = process.env[dialect === 'postgres' ? 'SDE_POSTGRES_DSN' : 'SDE_CLICKHOUSE_DSN'] !== undefined
  it.skipIf(!live)('opens a signed session with existing metadata and no DDL rights', async () => {
    await withRoles(dialect, async roles => {
      const { model, placement } = document(dialect)
      await prepareSchema(model, placement, { db: roles.operator }, { projectId: PROJECT })
      await roles.operator.mapWatermark()
      await roles.grant('events'); await roles.grant(WATERMARK_TABLE)
      const session = await Session.open(model, placement, { db: roles.runtime }, { projectId: PROJECT })
      await session.save('Event', { id: 1n })
      expect(await session.get('Event', { id: 1n })).toEqual({ id: 1n })
      expect(session.rollbackProtection.protection).toBe('enforced')
      expect(await roles.operator.mapWatermark()).toBe(7)
      const create = dialect === 'postgres' ? 'CREATE TABLE forbidden_table (id bigint)' : 'CREATE TABLE forbidden_table (id Int64) ENGINE=MergeTree ORDER BY id'
      await expect(roles.statement(create, true)).rejects.toThrow(/permission denied|Not enough privileges/)
      await expect(roles.runtime.writeFence('events', { projectId: PROJECT }).freeze('6'.repeat(32))).rejects.toThrow(/must be owner|Not enough privileges/)
      expect((await roles.operator.writeFence('events', { projectId: PROJECT }).state()).holds).toEqual([])
    })
  })
  it.skipIf(!live)('provisions bookkeeping without adopting the signed map', async () => {
    await withRoles(dialect, async roles => {
      const { model, placement } = document(dialect)
      await prepareSchema(model, placement, { db: roles.operator }, { projectId: PROJECT })
      expect(await exists(roles, dialect)).toBe(true)
      expect(await metadataRows(roles, dialect)).toEqual([])
      await roles.operator.recordMapVersion(2, { modelVersion: model.version })
      await prepareSchema(model, placement, { db: roles.operator }, { projectId: PROJECT })
      expect(await metadataRows(roles, dialect)).toEqual([2])
    })
  })
  it.skipIf(!live)('does not treat unreadable metadata as an empty watermark', async () => {
    await withRoles(dialect, async roles => {
      const { model, placement } = document(dialect)
      await prepareSchema(model, placement, { db: roles.operator }, { projectId: PROJECT })
      await roles.operator.mapWatermark(); await roles.operator.recordMapVersion(9, { modelVersion: model.version })
      await roles.grant('events'); await roles.grant(WATERMARK_TABLE); await roles.grant(WATERMARK_TABLE, true)
      await expect(Session.open(model, placement, { db: roles.runtime }, { projectId: PROJECT })).rejects.toThrow(/permission denied|Not enough privileges/)
      expect(await metadataRows(roles, dialect)).toEqual([9])
    })
  })
  it.skipIf(!live)('provisions and runs an unsigned map without bookkeeping', async () => {
    await withRoles(dialect, async roles => {
      const { model, placement } = document(dialect, false)
      await prepareSchema(model, placement, { db: roles.operator }, { projectId: PROJECT })
      expect(await exists(roles, dialect)).toBe(false)
      await roles.grant('events')
      const session = await Session.open(model, placement, { db: roles.runtime }, { projectId: PROJECT })
      await session.save('Event', { id: 1n })
      expect(await session.get('Event', { id: 1n })).toEqual({ id: 1n })
      expect(session.rollbackProtection.protection).toBe('not_applicable')
      expect(await exists(roles, dialect)).toBe(false)
    })
  })
  it.skipIf(!live)('prepares bookkeeping on an unmapped supplied engine too', async () => {
    await withRoles(dialect, async roles => {
      await withRoles(dialect, async spare => {
        const { model, placement } = document(dialect)
        await prepareSchema(model, placement, { db: roles.operator, spare: spare.operator }, { projectId: PROJECT })
        await roles.grant('events'); await roles.grant(WATERMARK_TABLE); await spare.grant(WATERMARK_TABLE)
        const session = await Session.open(model, placement, { db: roles.runtime, spare: spare.runtime }, { projectId: PROJECT })
        await session.save('Event', { id: 1n })
        expect(await session.get('Event', { id: 1n })).toEqual({ id: 1n })
        expect(session.rollbackProtection.participating).toEqual(['db', 'spare'])
        expect(await metadataRows(spare, dialect)).toEqual([7])
      })
    })
  })
})

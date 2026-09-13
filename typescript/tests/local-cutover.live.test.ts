/** A separate Python operator switches storage while this TypeScript application retains an old map. */
import { execFileSync } from 'node:child_process'
import { generateKeyPairSync, randomUUID, sign } from 'node:crypto'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { expect, it } from 'vitest'
import { buildModel, canonicalBytes, entity, loadMap, neutralDeclaration, prepareSchema, Session, T,
  verificationRequest, WATERMARK_TABLE } from '../src/index.js'
import { withRoles } from './_runtime-roles.js'

const LIVE = process.env['SDE_POSTGRES_DSN'] !== undefined && process.env['SDE_CLICKHOUSE_DSN'] !== undefined
it.skipIf(!LIVE)('survives a Python cutover with a delayed old TypeScript fan-out', async () => {
  const directory = mkdtempSync(join(process.env['SDE_TEST_TMPDIR'] ?? process.env['RUNNER_TEMP'] ?? tmpdir(), 'sde-cutover-'))
  try {
    await withRoles('postgres', async pg => {
      await withRoles('clickhouse', async ch => {
        const key = generateKeyPairSync('ed25519'), projectId = '1'.repeat(32)
        const publicKey = key.publicKey.export({ format: 'der', type: 'spki' }).subarray(-32)
        const model = buildModel([entity('Event', { fields: { id: T.int64, value: T.int32 }, key: ['id'] })])
        function signed(input: Record<string, any>): Record<string, any> {
          const document = structuredClone(input); delete document['signature']
          document['signature'] = { alg: 'ed25519', value: sign(null, canonicalBytes(document), key.privateKey).toString('base64') }
          return document
        }
        const source = { id: 'source', engine: 'postgres', layout: { tables: { Event: 'source_events' }, columns: { Event: { id: 'bigint', value: 'integer' } } } }
        const copy = { id: 'copy', engine: 'clickhouse', layout: { tables: { Event: 'copy_events' }, columns: { Event: { id: 'Int64', value: 'Int32' } } } }
        const before = signed({ contract: 4, project_id: projectId, model_version: model.version, map_version: 1,
          groups: { Event: { write_epoch: 1, source, derived: [{ ...copy, lag_budget_ms: 60000 }], also_write: ['copy'] } } })
        const success = signed({ ...before, map_version: 2, groups: { Event: { write_epoch: 3, source: copy } } })
        const abort = signed({ ...before, map_version: 3, groups: { Event: { write_epoch: 2, source } } })
        const placement = loadMap(before, { model, publicKey })
        const request = verificationRequest(placement, { projectId, group: 'Event', requestId: randomUUID().replaceAll('-', ''), requestedAt: new Date().toISOString() })
        const packet = signed({ kind: 'sde-cutover', protocol: 1, plan_id: randomUUID().replaceAll('-', ''),
          project_id: projectId, group: 'Event', before, success, abort, verification: request.asRecord(),
          query_impact_digest: 'a'.repeat(64), pause_budget_ms: 30000 })
        const operators = { postgres: pg.operator, clickhouse: ch.operator }, runtime = { postgres: pg.runtime, clickhouse: ch.runtime }
        await prepareSchema(model, placement, operators, { projectId })
        await pg.grant('source_events'); await ch.grant('copy_events'); await pg.grant(WATERMARK_TABLE); await ch.grant(WATERMARK_TABLE)
        const old = await Session.open(model, placement, runtime, { projectId })
        await old.save('Event', { id: 1n, value: 11 })
        let release: () => void = () => {}, reached: () => void = () => {}
        const gate = new Promise<void>(resolve => { release = resolve })
        const waiting = new Promise<void>(resolve => { reached = resolve })
        const actual = ch.runtime.insert.bind(ch.runtime)
        ch.runtime.insert = async (table, values) => {
          if (values['id'] === 4n) { reached(); await gate }
          await actual(table, values)
        }
        const late = old.save('Event', { id: 4n, value: 44 })
        try {
          await waiting
          const output = execFileSync(resolve('../python/.venv/bin/python'), [resolve('../python/tests/_cutover_driver.py')], {
            input: JSON.stringify({ directory, model: neutralDeclaration(model), project_id: projectId,
              public_key: publicKey.toString('hex'), plan: packet,
              operators: { postgres: pg.operatorDsn, clickhouse: ch.operatorDsn },
              runtime: { postgres: pg.runtimeDsn, clickhouse: ch.runtimeDsn } }), encoding: 'utf8', timeout: 45000,
          })
          const receipt = JSON.parse(output) as { outcome: string; verification: { matched: boolean } }
          expect(receipt.outcome).toBe('success'); expect(receipt.verification.matched).toBe(true)
          const currentMap = loadMap(JSON.parse(readFileSync(join(directory, 'active-map.json'), 'utf8')), { model, publicKey })
          const current = await Session.open(model, currentMap, runtime, { projectId })
          ch.runtime.insert = actual
          await current.save('Event', { id: 4n, value: 99 })
          release(); await late
          expect(await current.get('Event', { id: 4n })).toEqual({ id: 4n, value: 99 })
          await expect(old.get('Event', { id: 1n })).rejects.toThrow()
          await expect(old.save('Event', { id: 5n, value: 55 })).rejects.toThrow()
          await current.save('Event', { id: 6n, value: 66 })
          expect(await current.get('Event', { id: 6n })).toEqual({ id: 6n, value: 66 })
        } finally {
          ch.runtime.insert = actual; release(); await late.catch(() => {})
        }
      })
    })
  } finally { rmSync(directory, { recursive: true, force: true }) }
}, 60000)

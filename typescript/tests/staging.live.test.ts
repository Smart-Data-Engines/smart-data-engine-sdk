/** Old and newly opened TypeScript sessions share the Python operator's staged source. */
import { execFileSync } from 'node:child_process'
import { generateKeyPairSync, randomUUID, sign } from 'node:crypto'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { expect, it } from 'vitest'
import { buildModel, canonicalBytes, entity, loadMap, loadStagingPlan, neutralDeclaration,
  prepareSchema, Session, stagingTableName, T, verificationRequest, WATERMARK_TABLE } from '../src/index.js'
import { withRoles } from './_runtime-roles.js'

const LIVE = process.env['SDE_POSTGRES_DSN'] !== undefined && process.env['SDE_CLICKHOUSE_DSN'] !== undefined
it.skipIf(!LIVE).each(['postgres', 'clickhouse'] as const)('stages and switches a %s source across processes', async sourceName => {
  const directory = mkdtempSync(join(process.env['SDE_TEST_TMPDIR'] ?? process.env['RUNNER_TEMP'] ?? tmpdir(), 'sde-stage-'))
  try {
    await withRoles('postgres', async pg => {
      await withRoles('clickhouse', async ch => {
        const roles = { postgres: pg, clickhouse: ch }, targetName = sourceName === 'postgres' ? 'clickhouse' : 'postgres'
        const key = generateKeyPairSync('ed25519'), projectId = '1'.repeat(32), stageId = randomUUID().replaceAll('-', '')
        const publicKey = key.publicKey.export({ format: 'der', type: 'spki' }).subarray(-32)
        const model = buildModel([entity('Event', { fields: { id: T.int64, value: T.int32 }, key: ['id'] })])
        function signed(input: Record<string, any>): Record<string, any> {
          const document = structuredClone(input); delete document['signature']
          document['signature'] = { alg: 'ed25519', value: sign(null, canonicalBytes(document), key.privateKey).toString('base64') }
          return document
        }
        function material(engine: string, table: string, id: string): Record<string, any> {
          return { id, engine, layout: { tables: { Event: table }, columns: {
            Event: engine === 'postgres' ? { id: 'bigint', value: 'integer' } : { id: 'Int64', value: 'Int32' },
          } } }
        }
        const source = material(sourceName, 'initial_events', 'source'), copy = material(targetName, stagingTableName(stageId, 1), 'copy')
        const before = signed({ contract: 4, project_id: projectId, model_version: model.version, map_version: 1,
          groups: { Event: { write_epoch: 1, source } } })
        const prepared = signed({ ...before, map_version: 2, groups: { Event: { write_epoch: 1, source,
          derived: [{ ...copy, lag_budget_ms: 30000 }], also_write: ['copy'] } } })
        const staging = signed({ kind: 'sde-stage', protocol: 1, stage_id: stageId, project_id: projectId,
          group: 'Event', current: before, prepared })
        const parsed = loadStagingPlan(staging, { model, projectId, publicKey })
        const placement = loadMap(before, { model, publicKey })
        const operators = { postgres: pg.operator, clickhouse: ch.operator }, runtime = { postgres: pg.runtime, clickhouse: ch.runtime }
        await prepareSchema(model, placement, operators, { projectId })
        await roles[sourceName].grant('initial_events'); await pg.grant(WATERMARK_TABLE); await ch.grant(WATERMARK_TABLE)
        const old = await Session.open(model, placement, runtime, { projectId })
        await old.save('Event', { id: 1n, value: 11 })
        const execute = (packet: Record<string, any>): any => JSON.parse(execFileSync(resolve('../python/.venv/bin/python'),
          [resolve('../python/tests/_staging_driver.py')], { input: JSON.stringify({ directory, model: neutralDeclaration(model),
            project_id: projectId, public_key: publicKey.toString('hex'), plan: packet,
            operators: { postgres: pg.operatorDsn, clickhouse: ch.operatorDsn },
            runtime: { postgres: pg.runtimeDsn, clickhouse: ch.runtimeDsn } }), encoding: 'utf8', timeout: 45000 }))
        const staged = execute(staging)
        expect(staged.outcome).toBe('prepared'); expect(staged.map_fingerprint).toBe(parsed.prepared.fingerprint)
        await old.save('Event', { id: 2n, value: 22 })
        const activeMap = () => loadMap(JSON.parse(readFileSync(join(directory, 'active-map.json'), 'utf8')), { model, publicKey })
        const active = await Session.open(model, activeMap(), runtime, { projectId })
        await active.save('Event', { id: 3n, value: 33 })
        expect(await roles[targetName].operator.get(stagingTableName(stageId, 1), { id: 2n })).toBeNull()
        expect(await roles[targetName].operator.get(stagingTableName(stageId, 1), { id: 3n })).not.toBeNull()
        const request = verificationRequest(parsed.prepared, { projectId, group: 'Event', requestId: randomUUID().replaceAll('-', ''), requestedAt: new Date().toISOString() })
        const packet = signed({ kind: 'sde-cutover', protocol: 1, plan_id: randomUUID().replaceAll('-', ''), project_id: projectId,
          group: 'Event', before: prepared,
          success: signed({ ...before, map_version: 3, groups: { Event: { write_epoch: 3, source: copy } } }),
          abort: signed({ ...before, map_version: 4, groups: { Event: { write_epoch: 2, source } } }),
          verification: request.asRecord(), pause_budget_ms: 30000, query_impact_digest: 'a'.repeat(64) })
        expect(execute(packet).outcome).toBe('success')
        const moved = await Session.open(model, activeMap(), runtime, { projectId })
        for (const id of [1n, 2n, 3n]) expect(await moved.get('Event', { id })).toEqual({ id, value: Number(id) * 11 })
        await expect(old.get('Event', { id: 1n })).rejects.toThrow()
        await expect(active.save('Event', { id: 4n, value: 44 })).rejects.toThrow()
      })
    })
  } finally { rmSync(directory, { recursive: true, force: true }) }
}, 60000)

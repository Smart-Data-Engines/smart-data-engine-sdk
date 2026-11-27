/**
 * A live copy verified in TypeScript against the request the Python SDK issues, as the control plane
 * issues it. The request binds the locally configured project, so the map is of contract 4: prepared
 * with its write fences and the copy's mark, and written through a session (format contract §7b,
 * §7r). Each engine is in a namespace of its own, removed afterwards.
 */
import { execFileSync } from 'node:child_process'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  buildModel, entity, loadMap, neutralDeclaration, prepareSchema, Session, T, Timestamp, verify,
  VerificationRequest, verifyRecord,
} from '../src/index.js'
import { fixture } from './_generation-engines.js'

const PG = process.env['SDE_POSTGRES_DSN'], CH = process.env['SDE_CLICKHOUSE_DSN']
const PROJECT = '1'.repeat(32)

describe.skipIf(!PG || !CH)('a verification request the Python SDK issues', () => {
  it('binds the comparison of a live copy, to the microsecond, in TypeScript', async () => {
    await fixture('postgres', async (pg) => {
      await fixture('clickhouse', async (ch) => {
        const model = buildModel([entity('Event', { fields: { id: T.int64, at: T.timestamptz }, key: ['id'] })])
        const document = { contract: 4, project_id: PROJECT, model_version: model.version, map_version: 1,
          groups: { Event: { write_epoch: 1,
            source: { id: 'source', engine: 'pg',
              layout: { tables: { Event: 'records' }, columns: { Event: { id: 'bigint', at: 'timestamptz' } } } },
            derived: [{ id: 'copy', engine: 'ch', lag_budget_ms: 30000,
              layout: { tables: { Event: 'records' }, columns: { Event: { id: 'Int64', at: "DateTime64(6, 'UTC')" } } } }],
            also_write: ['copy'] } } }
        const map = loadMap(document, { model })
        await prepareSchema(model, map, { pg, ch }, { projectId: PROJECT })
        const session = await Session.open(model, map, { pg, ch }, { projectId: PROJECT })
        await session.save('Event', { id: 1n, at: Timestamp.from('2026-09-12T09:30:15.123456Z') })
        await session.save('Event', { id: 2n, at: Timestamp.from('2026-09-12T09:30:15.123457Z') })
        const request = VerificationRequest.fromRecord(JSON.parse(execFileSync(
          resolve('../python/.venv/bin/python'), [resolve('../python/tests/verification_peer.py')], {
            input: JSON.stringify({ model: neutralDeclaration(model), map: document, project_id: PROJECT,
              request_id: '2'.repeat(32), group: 'Event', requested_at: '2000-01-01T00:00:00Z' }),
            encoding: 'utf8', timeout: 30_000,
          },
        )))
        const report = await verify(session, 'Event', { request })
        expect(report.matched).toBe(true)
        expect(report.rowsSource).toBe(2)
        expect(verifyRecord(report)['request']).toEqual(request.asRecord())
      })
    })
  }, 60_000)
})

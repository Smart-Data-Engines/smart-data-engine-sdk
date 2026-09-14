/** Python query bounds must agree with a separate TypeScript writer at adjacent microseconds. */
import { execFileSync } from 'node:child_process'
import { resolve } from 'node:path'
import { expect, it } from 'vitest'
import { Timestamp } from '../src/index.js'
import { withRoles } from './_runtime-roles.js'

it.skipIf(process.env['SDE_CLICKHOUSE_DSN'] === undefined)(
  'Python finds and pages TypeScript-written temporal keys without rounding their bounds', async () => {
    await withRoles('clickhouse', async roles => {
      await roles.operator.ensureSchema({
        tables: { Reading: 'temporal_queries' }, indexes: [], partitionBy: {},
        columns: { Reading: { at: "DateTime64(6, 'UTC')", id: 'Int64', value: 'Int32' } },
      }, { keys: { Reading: ['at', 'id'] } })
      await roles.grant('temporal_queries')
      const times = ['2026-09-14T12:00:00.123456Z', '2026-09-14T12:00:00.123457Z', '2026-09-14T12:00:00.123458Z']
      for (const [index, at] of times.entries()) {
        await roles.operator.insert('temporal_queries', { at: Timestamp.from(at), id: BigInt(index + 1), value: index })
        expect((await roles.runtime.get('temporal_queries', { at: Timestamp.from(at), id: BigInt(index + 1) }))?.['id']).toBe(BigInt(index + 1))
      }
      const result = JSON.parse(execFileSync(resolve('../python/.venv/bin/python'),
        [resolve('../python/tests/datetime_query_peer.py')], {
          input: JSON.stringify({ times }), encoding: 'utf8', timeout: 20000,
          env: { ...process.env, SDE_TEMPORAL_PEER_DSN: roles.runtimeDsn },
        }))
      expect(result).toEqual({ points: [1, 2, 3], range: [1, 2], keyset: [2, 3] })
    })
  }, 30000,
)

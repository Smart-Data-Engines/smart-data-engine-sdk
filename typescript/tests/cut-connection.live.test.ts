/**
 * What each adapter says **after** its connection is gone, and why that is a file of its own.
 *
 * The third connection failure in `docs/failure-semantics.md` is a *message* rather than a bound.
 * Measured in the reference implementation: after the connection is cut, the first failing call
 * reports what the server said - which is right - and every call after it reports only that the
 * connection is closed, which is true and useless. What a reader needs at that point is to be told
 * that this library holds the connection it was handed and does not reopen it, and that nothing was
 * retried, so no write reached the engine twice.
 *
 * The victim has a unique application_name. Separate Vitest workers still share the database;
 * terminating every other backend killed unrelated tests when the interop slice was added.
 * The test also holds a bystander connection and proves it survives the targeted termination.
 *
 * It also found a defect worse than the one it was written for. `pg.Client` is an `EventEmitter`
 * and emits `'error'` when the server terminates the connection between queries; Node's rule for an
 * `'error'` event with no listener is to **throw it**, so the adapter that did not listen would have
 * turned a database restart into an uncatchable error inside somebody's event loop. The adapter
 * listens now, and the sentence it adds afterwards is what this file asserts.
 */

import { randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import type { Row } from '../src/index.js'
import { EngineError, loadMap } from '../src/index.js'
import { PostgresEngine } from '../src/engines/postgres.js'
import { modelFromNeutral } from '../src/testing/loader.js'

const PG_DSN = process.env['SDE_POSTGRES_DSN']

const VECTOR = join(
  __dirname,
  '..',
  '..',
  'conformance',
  'vectors',
  'schema',
  '010-every-type-both-engines-map',
)

function sample(id: string): Row {
  return {
    id,
    label: 'cut',
    small: 1,
    big: 2n,
    approx: 0.5,
    narrow: 0.5,
    money: '1.00',
    flag: false,
    at: new Date('2026-08-27T12:00:00.000Z'),
    naive: new Date('2026-08-27T12:00:00.000Z'),
    day: '2026-08-27',
  }
}

/**
 * Terminate only the named test session, using an independent connection.
 *
 * Through `pg` directly rather than through the adapter, and that is the same choice the reference
 * implementation made for the same test: the subject is what the *adapter* reports after its
 * connection is gone, so the thing doing the killing has to be something else. Reaching into the
 * adapter's private query method would have worked and would have made the executioner share the
 * connection it is executing.
 */
async function terminateVictim(name: string): Promise<void> {
  const { Client } = await import('pg')
  const client = new Client({ connectionString: PG_DSN })
  await client.connect()
  try {
    const killed = await client.query(
      'SELECT pg_terminate_backend(pid) FROM pg_stat_activity ' +
        'WHERE datname = current_database() AND application_name = $1 AND pid <> pg_backend_pid()',
      [name],
    )
    expect(killed.rows).toHaveLength(1)
  } finally {
    await client.end()
  }
}

it.skipIf(!PG_DSN)('has PostgreSQL configured', () => {
  // A marker whose purpose is its skip: vitest fails a file whose every suite was skipped, and
  // "no tests" is not a word CI's did-not-skip guard can look for.
  expect(PG_DSN).toBeTruthy()
})

describe.skipIf(!PG_DSN)('after the connection is cut', () => {
  it('says the connection is gone, and that nothing was retried', async () => {
    // The third connection failure in docs/failure-semantics.md, and the one that is a *message*
    // rather than a bound. Measured in the reference: after the connection is cut, the first
    // failing call reports what the server said, which is right - and every call after it reports
    // only that the connection is closed, which is true and useless. What a reader needs at that
    // point is to be told this library holds the connection it was handed and does not reopen it,
    // and that nothing was retried, so no write reached the engine twice.
    const name = `sde_cut_${randomUUID().replaceAll('-', '')}`
    const dsn = new URL(PG_DSN as string)
    dsn.searchParams.set('application_name', name)
    const own = new PostgresEngine(dsn.toString())
    const { Client } = await import('pg')
    const bystander = new Client({ connectionString: PG_DSN })
    await bystander.connect()
    await own.connect()
    try {
      const model = modelFromNeutral(JSON.parse(readFileSync(join(VECTOR, 'model.json'), 'utf8')))
      const map = loadMap(JSON.parse(readFileSync(join(VECTOR, 'map.json'), 'utf8')), { model })
      const layout = { ...map.groups['Sample']!.source.layout, tables: { Sample: name } }
      await own.ensureSchema(layout, { keys: { Sample: ['id'] } })
      await own.insert(name, sample(randomUUID()))

      await terminateVictim(name)
      const first = await own.insert(name, sample(randomUUID())).catch((error: Error) => error)
      expect(first).toBeInstanceOf(EngineError)
      const second = await own.insert(name, sample(randomUUID())).catch((error: Error) => error)
      expect((second as Error).message).toContain('does not reopen one it was handed')
      expect((second as Error).message).toContain('Nothing was retried')
      expect((await bystander.query('SELECT 1 AS alive')).rows[0].alive).toBe(1)

      // The documented recovery, on the same adapter, without retrying a failed write.
      await own.close()
      await own.connect()
      await own.insert(name, sample(randomUUID()))
    } finally {
      await own.close()
      await bystander.query(`DROP TABLE IF EXISTS "${name}"`)
      await bystander.end()
    }
  })
})

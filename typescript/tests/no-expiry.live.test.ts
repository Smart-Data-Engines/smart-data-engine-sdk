/**
 * No expiry, behaviourally (requirement 15a.1): the full operation set runs against PostgreSQL with
 * the wall clock a year past when the map was written. The reference's
 * `test_the_full_operation_set_runs_with_the_clock_a_year_forward`, ported.
 *
 * The map is hand-written and unsigned, which is the account-free mode, so nothing in it came from
 * us. A map has no age to give it (`no-expiry.test.ts`), and moving the clock is stronger anyway: any
 * expiry check has to ask what time it is now, and here the answer is a year later.
 */

import { afterEach, expect, it, vi } from 'vitest'

import { buildModel, entity, loadMap, Recorder, Session, T } from '../src/index.js'
import { PostgresEngine } from '../src/engines/postgres.js'

const DSN = process.env['SDE_POSTGRES_DSN']

afterEach(() => {
  vi.useRealTimers()
})

it.skipIf(!DSN)('runs the full operation set with the clock a year forward', async () => {
  const table = `no_expiry_${process.pid}_${Date.now()}`
  const model = buildModel([
    entity('Reading', { fields: { id: T.int64, station: T.string, celsius: T.int64 }, key: ['id'] }),
  ])
  const placement = loadMap(
    {
      contract: 2,
      model_version: model.version,
      map_version: 7,
      groups: {
        Reading: {
          source: {
            id: 'source@pg-main',
            engine: 'pg-main',
            layout: {
              tables: { Reading: table },
              columns: { Reading: { id: 'bigint', station: 'text', celsius: 'bigint' } },
            },
          },
        },
      },
      routing: {},
    },
    { model },
  )
  // Only the calendar moves: a timer the driver sets still fires.
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date('2027-09-04T00:00:00Z'))
  expect(new Date().getUTCFullYear()).toBe(2027)

  const engine = new PostgresEngine(DSN!)
  await engine.connect()
  const recorder = new Recorder(model.version)
  try {
    const session = await Session.open(model, placement, { 'pg-main': engine }, { recorder })
    await session.ensureSchema()
    await session.save('Reading', { id: 1n, station: 'WAW', celsius: 21n })
    expect(await session.get('Reading', { id: 1n })).toEqual({ id: 1n, station: 'WAW', celsius: 21n })
    await session.transaction(['Reading'], async (inside) => {
      await inside.save('Reading', { id: 2n, station: 'KRK', celsius: 18n })
    })
    expect(await session.count('Reading')).toBe(2n)
    // The one comparison this library makes against stored state is a version, not a date, and it
    // guards only what claims to come from us: an unsigned map has nothing to check.
    expect(session.rollbackProtection.protection).toBe('not_applicable')

    const window = recorder.roll()
    expect(window).not.toBeNull()
    expect(window!.shapes.reduce((calls, shape) => calls + shape.calls, 0)).toBeGreaterThanOrEqual(3)
    // A window is bounded by a monotonic counter, not by dates: nanoseconds since the epoch would be
    // about 1.8e18, so a window cannot be aged either.
    expect(window!.startedNs).toBeLessThan(1e17)
    expect(window!.endedNs).toBeGreaterThanOrEqual(window!.startedNs)
  } finally {
    vi.useRealTimers()
    await engine.close()
    // Through `pg` directly, as the other live tests drop what they made.
    const { Client } = await import('pg')
    const admin = new Client({ connectionString: DSN })
    await admin.connect()
    try {
      await admin.query(`DROP TABLE IF EXISTS "${table}" CASCADE`)
    } finally {
      await admin.end()
    }
  }
}, 30000)

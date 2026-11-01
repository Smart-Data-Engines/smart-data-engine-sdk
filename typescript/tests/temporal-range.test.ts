/**
 * A day or an instant an engine holds (format contract section 8b, decision 43 of the C++ port).
 *
 * The session's refusals are pinned by `errors/140`-`142`; this file holds the range itself and a
 * migration's copy, which no vector reaches.
 */
import { describe, expect, it } from 'vitest'

import { loadMap, MigrationRefused, Session } from '../src/index.js'
import { backfill } from '../src/migration.js'
import { fromEngineText, TemporalRange } from '../src/temporal-range.js'
import { modelFromNeutral } from '../src/testing/loader.js'
import { MemoryEngine } from '../src/testing/memory.js'
import { Timestamp } from '../src/timestamp.js'

const NARROW = fromEngineText('1900-01-01', '2299-12-31', '1900-01-01 00:00:00.000000', '2299-12-31 23:59:59.999999')!

describe('the range a server holds', () => {
  it('is none for a server holding the contract whole', () => {
    expect(fromEngineText('0001-01-01', '9999-12-31', '0001-01-01 00:00:00.000000', '9999-12-31 23:59:59.999999')).toBeNull()
    expect(NARROW).toBeInstanceOf(TemporalRange)
  })

  it.each([
    ['date', '1900-01-01', true],
    ['date', '2299-12-31', true],
    ['date', '1899-12-31', false],
    ['date', '2300-01-01', false],
    ['timestamp', Timestamp.from('2299-12-31T23:59:59.999999Z'), true],
    ['timestamp', Timestamp.from('2300-01-01T00:00:00Z'), false],
    ['timestamptz', Timestamp.from('1900-01-01T00:00:00Z'), true],
    ['timestamptz', Timestamp.from('1899-12-31T23:59:59.999999Z'), false],
    // A copy's row may hold a timestamp as text.
    ['timestamptz', '2500-06-01T12:00:00Z', false],
  ] as const)('holds %s %s: %s', (kind, value, held) => {
    expect(NARROW.holds(kind, value)).toBe(held)
  })
})

describe('a migration onto a target that holds fewer days', () => {
  const model = modelFromNeutral({
    entities: [{ name: 'Visit', fields: [{ name: 'id', type: 'int32' }, { name: 'on', type: 'date' }], key: ['id'] }],
  })
  const migrating = (range: TemporalRange | null): [Session, MemoryEngine] | Promise<[Session, MemoryEngine]> => {
    const source = new MemoryEngine({ dialect: 'postgres', name: 'pg-main', tables: { visit: [{ id: 1, on: '2026-10-07' }, { id: 2, on: '1800-01-01' }] } })
    const target = new MemoryEngine({ dialect: 'clickhouse', name: 'ch-copy', temporalRange: range })
    const map = loadMap({
      contract: 3, model_version: model.version, map_version: 1,
      groups: { Visit: {
        source: { id: 'src', engine: 'pg-main', layout: { tables: { Visit: 'visit' }, columns: { Visit: { id: 'integer', on: 'date' } } } },
        derived: [{ id: 'dst', engine: 'ch-copy', lag_budget_ms: 30000,
          layout: { tables: { Visit: 'visit_copy' }, columns: { Visit: { id: 'Int32', on: 'Date32' } } } }],
        also_write: ['dst'],
      } },
    }, { model })
    return Session.open(model, map, { 'pg-main': source, 'ch-copy': target }).then((session) => [session, target])
  }

  it('refuses a chunk it cannot hold before copying any of it', async () => {
    const [session, target] = await migrating(NARROW)
    const error = await backfill(session, 'Visit', { chunkRows: 10 }).catch((thrown: unknown) => thrown)
    expect(error).toBeInstanceOf(MigrationRefused)
    expect((error as Error).message.startsWith(
      'ch-copy holds a date from 1900-01-01 to 2299-12-31, and a row of Visit gives on one outside it, ' +
        'so this chunk was not copied.')).toBe(true)
    expect((error as Error).message).not.toContain('1800')
    expect(target.tables['visit_copy'] ?? []).toEqual([])
  })

  it('takes the same chunk onto a target holding the whole range', async () => {
    const [session, target] = await migrating(null)
    expect((await backfill(session, 'Visit', { chunkRows: 10 })).complete).toBe(true)
    expect(target.tables['visit_copy']).toHaveLength(2)
  })
})

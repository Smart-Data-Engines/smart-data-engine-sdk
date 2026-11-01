/**
 * A day or an instant the server holds, against a live ClickHouse (format contract section 8b).
 *
 * What the test expects is the server's: ClickHouse 24.8 holds `Date32` and `DateTime64` from
 * 1900-01-01 to 2299-12-31 and moved 1800-01-01 to 1900-01-01 without an error, and 26.9 holds the
 * contract's whole range. CI runs the live slices on both, so each half runs somewhere.
 */
import { randomUUID } from 'node:crypto'

import { describe, expect, it } from 'vitest'

import { EngineError, loadMap, Session, Timestamp } from '../src/index.js'
import { ClickHouseEngine } from '../src/engines/clickhouse.js'
import { modelFromNeutral } from '../src/testing/loader.js'

const dsn = process.env['SDE_CLICKHOUSE_DSN']

describe.skipIf(!dsn)('a ClickHouse server says the days it holds', () => {
  it('and a value outside them never reaches the server', async () => {
    const engine = new ClickHouseEngine(dsn!)
    const table = 'range_' + randomUUID().replaceAll('-', '').slice(0, 8)
    const model = modelFromNeutral({
      entities: [{
        name: 'Visit',
        fields: [{ name: 'at', type: 'timestamptz' }, { name: 'id', type: 'int32' }, { name: 'on', type: 'date' }],
        key: ['id'],
      }],
    })
    const map = loadMap({
      contract: 3, model_version: model.version, map_version: 1,
      groups: { Visit: { source: { id: 'src', engine: 'ch', layout: {
        tables: { Visit: table }, columns: { Visit: { at: "DateTime64(6, 'UTC')", id: 'Int32', on: 'Date32' } },
      } } } },
    }, { model })
    await engine.connect()
    try {
      const session = await Session.open(model, map, { ch: engine })
      await session.ensureSchema()
      const span = await engine.temporalRange()
      // Both ends of the narrower range are held wherever the server stands.
      await session.save('Visit', { id: 1, on: '1900-01-01', at: Timestamp.from('2299-12-31T23:59:59.999999Z') })
      const ends = await session.get('Visit', { id: 1 })
      expect([ends?.['on'], (ends?.['at'] as Timestamp).toISOString()])
        .toEqual(['1900-01-01', '2299-12-31T23:59:59.999999Z'])
      const before = { id: 2, on: '1800-01-01', at: Timestamp.from('2026-10-07T00:00:00Z') }
      if (span === null) {
        // 26.9: the contract's whole range, and the value comes back as written.
        await session.save('Visit', before)
        expect((await session.get('Visit', { id: 2 }))?.['on']).toBe('1800-01-01')
      } else {
        // 24.8: refused before the server could move it to 1900-01-01.
        expect([span.firstDay, span.lastDay]).toEqual(['1900-01-01', '2299-12-31'])
        const refused = await session.save('Visit', before).catch((thrown: unknown) => thrown)
        expect(refused).toBeInstanceOf(EngineError)
        expect((refused as Error).message).toBe(
          'ch holds a date from 1900-01-01 to 2299-12-31, and this row gives Visit.on one outside it; nothing was sent')
        expect(await session.get('Visit', { id: 2 })).toBeNull()
      }
      expect(await engine.temporalRange()).toBe(span)
    } finally {
      const url = new URL(dsn!)
      await fetch(`http://${url.host}/?database=${encodeURIComponent(url.pathname.slice(1))}`, {
        method: 'POST',
        body: `DROP TABLE IF EXISTS ${table}`,
        headers: { Authorization: `Basic ${Buffer.from(`${decodeURIComponent(url.username)}:${decodeURIComponent(url.password)}`).toString('base64')}` },
      })
      await engine.close()
    }
  })
})

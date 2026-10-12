/**
 * Rules the third library found that no shared vector can reach: the map-stage cases always load
 * with their model, and the errors runner passes one bare key, never a named set.
 */

import { describe, expect, it } from 'vitest'

import { MapError } from '../src/errors.js'
import { loadMap } from '../src/placement.js'
import { enumerateShapes, shapeId } from '../src/shapes.js'
import { modelFromNeutral } from '../src/testing/loader.js'

function eventMap(copyEngine: string): Record<string, unknown> {
  const layout = { tables: { Event: 'event' }, columns: { Event: { id: 'uuid', name: 'text' } } }
  return {
    contract: 3,
    model_version: 'm',
    map_version: 1,
    groups: {
      Event: {
        source: { id: 'event@pg', engine: 'pg-main', layout },
        derived: [{ id: 'event@ch', engine: copyEngine, layout, lag_budget_ms: 30000 }],
      },
    },
  }
}

describe('the third implementation', () => {
  it('refuses a copy that is the source under another name without a model too', () => {
    // Explicit layouts have their tables without a model, so the rule needs none.
    expect(() => loadMap(eventMap('pg-main'))).toThrow(MapError)
    expect(() => loadMap(eventMap('pg-main'))).toThrow(
      'is in the same engine as the source and reuses its tables',
    )
    expect(loadMap(eventMap('ch-1')).groups['Event']!.derived[0]!.id).toBe('event@ch')
  })

  it('tries named keys in code point order, as the reference does', () => {
    // U+E000 sorts before an astral character by code point and after it by UTF-16 unit, which is
    // what `<` compares. The order shows in the refusal that lists the keys tried.
    const signed = {
      ...eventMap('ch-1'),
      signature: { alg: 'ed25519', value: Buffer.alloc(64).toString('base64') },
    }
    const keys = { '\u{1D49C}': new Uint8Array(32).fill(1), '': new Uint8Array(32).fill(2) }
    expect(() => loadMap(signed, { publicKey: keys })).toThrow(
      `(${JSON.stringify(['', '\u{1D49C}'])})`,
    )
  })

  it('accepts a write shape routed at the source, and refuses one routed at a copy', () => {
    const model = modelFromNeutral({
      entities: [
        {
          name: 'Event',
          fields: [
            { name: 'id', type: 'uuid' },
            { name: 'name', type: 'string' },
          ],
          key: ['id'],
        },
      ],
    })
    const write = enumerateShapes(model).find((shape) => shape.kind === 'write')!
    const map = (target: string): Record<string, unknown> => ({
      ...eventMap('ch-1'),
      model_version: model.version,
      routing: { [shapeId(write)]: target },
    })
    expect(loadMap(map('event@pg'), { model }).routing[shapeId(write)]).toBe('event@pg')
    expect(() => loadMap(map('event@ch'), { model })).toThrow(
      'Writes go to the source whatever this table says',
    )
  })
})

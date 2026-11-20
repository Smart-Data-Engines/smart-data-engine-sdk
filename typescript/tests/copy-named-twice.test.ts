// From contract 4, a copy's table is named by nothing else in the map (format contract §7, §7r).
// The refusal itself is the shared vector errors/151; these are the variants around it, as in the
// reference's test_copy_named_twice.py: what else it refuses, and what it leaves as it was.
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { loadMap, MapError } from '../src/index.js'
import { modelFromNeutral } from '../src/testing/loader.js'

const CASE = resolve('../conformance/vectors/errors/151-a-copy-named-like-another-groups-source')
const TWICE = 'is named twice in the map'

function document(): { model: ReturnType<typeof modelFromNeutral>; raw: any } {
  const model = modelFromNeutral(JSON.parse(readFileSync(resolve(CASE, 'model.json'), 'utf8')))
  return { model, raw: JSON.parse(readFileSync(resolve(CASE, 'map.json'), 'utf8')) }
}

describe("a copy's table named for anything else too", () => {
  it('is the vector, refused for the reason it names', () => {
    const { model, raw } = document()
    expect(() => loadMap(raw, { model })).toThrow(MapError)
    expect(() => loadMap(raw, { model })).toThrow(TWICE)
  })
  it('leaves two sources naming one table as they were', () => {
    const { model, raw } = document()
    const event = raw.groups.Event
    delete event.derived
    delete event.also_write
    event.source = { id: 'Event@pg', engine: 'pg-main', layout: { tables: { Event: 'order' } } }
    expect(() => loadMap(raw, { model })).not.toThrow()
  })
  it("refuses a copy named like another group's copy", () => {
    const { model, raw } = document()
    const line = raw.groups.Line
    line.source.engine = 'ch-main'
    line.source.id = 'Line@ch'
    line.source.layout.tables.Order = 'order_source'
    line.derived = [{
      id: 'Line@pg', engine: 'pg-main',
      layout: { tables: { Line: 'line_copy', Order: 'order' } }, lag_budget_ms: 30000,
    }]
    line.also_write = ['Line@pg']
    expect(() => loadMap(raw, { model })).toThrow("by group 'Event', materialisation 'Event@pg'")
  })
  it('refuses a copy naming one table for two entities', () => {
    const { model, raw } = document()
    raw.groups.Event.derived[0].layout.tables.Event = 'event_copy'
    const line = raw.groups.Line
    line.derived = [{
      id: 'Line@pg2', engine: 'pg-copy',
      layout: { tables: { Line: 'lines', Order: 'lines' } }, lag_budget_ms: 30000,
    }]
    line.also_write = ['Line@pg2']
    expect(() => loadMap(raw, { model })).toThrow("materialisation 'Line@pg2' (entity 'Line'), and by")
  })
  it('takes a map below contract 4 as it was', () => {
    const { model, raw } = document()
    raw.contract = 3
    delete raw.project_id
    for (const group of Object.values(raw.groups) as Record<string, unknown>[]) delete group['write_epoch']
    expect(() => loadMap(raw, { model })).not.toThrow()
  })
})

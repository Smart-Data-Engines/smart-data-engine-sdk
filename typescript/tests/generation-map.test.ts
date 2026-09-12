import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { loadMap, MapError } from '../src/index.js'
import { modelFromNeutral } from '../src/testing/loader.js'

const CASE = resolve('../conformance/vectors/migration/022-verification-is-bound-to-its-request')
const PROJECT = '1'.repeat(32)
function document() {
  const model = modelFromNeutral(JSON.parse(readFileSync(resolve(CASE, 'model.json'), 'utf8')))
  const raw = JSON.parse(readFileSync(resolve(CASE, 'map.json'), 'utf8'))
  raw.contract = 4; raw.project_id = PROJECT
  for (const group of Object.values(raw.groups) as Record<string, unknown>[]) group['write_epoch'] = 1
  return { model, raw }
}

describe('generation-bearing placement maps', () => {
  it('binds an immutable project and group epoch', () => {
    const { model, raw } = document()
    const loaded = loadMap(raw, { model })
    expect(loaded.projectId).toBe(PROJECT)
    expect(loaded.groups['Reading']!.writeEpoch).toBe(1)
    expect(loaded.fingerprint).toBeDefined()
    raw.groups.Reading.write_epoch = 2
    expect(loaded.groups['Reading']!.writeEpoch).toBe(1)
  })
  it.each([null, false, 0, -1, 1.5, '1', 2 ** 53])('refuses invalid write epoch %s', (epoch) => {
    const { model, raw } = document()
    raw.groups.Reading.write_epoch = epoch
    expect(() => loadMap(raw, { model })).toThrow('positive safe write_epoch')
  })
  it.each([null, '', 'A'.repeat(32), '1'.repeat(31), false])('requires an explicit project: %s', (project) => {
    const { model, raw } = document()
    raw.project_id = project
    expect(() => loadMap(raw, { model })).toThrow('project_id')
  })
  it('uses canonical JSON integers and refuses a fractional annotation', () => {
    const { model, raw } = document()
    const first = loadMap(raw, { model })
    const encoded = JSON.stringify(raw).replace('"write_epoch":1', '"write_epoch":1.0')
    expect(loadMap(JSON.parse(encoded), { model }).fingerprint).toBe(first.fingerprint)
    raw.annotation = 1.5
    expect(() => loadMap(raw, { model })).toThrow('canonically encodable')
  })
  it('cannot hide generation fields in an older contract', () => {
    const { model, raw } = document()
    raw.contract = 3
    expect(() => loadMap(raw, { model })).toThrow('project_id requires')
    delete raw.project_id
    expect(() => loadMap(raw, { model })).toThrow('write_epoch requires')
  })
  it('reserves the physical generation column', () => {
    const { model, raw } = document()
    raw.groups.Reading.source.layout.columns.Reading.__sde_write_epoch = 'bigint'
    expect(() => loadMap(raw, { model })).toThrow(MapError)
  })
  it('preserves the group epoch through automatic layout resolution', () => {
    const { model, raw } = document()
    raw.groups.Reading.source.layout = { auto: true }
    expect(loadMap(raw, { model }).groups['Reading']!.writeEpoch).toBe(1)
  })
})

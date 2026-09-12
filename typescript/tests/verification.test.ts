import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { loadMap, VerificationRequest, verificationRequest } from '../src/index.js'
import { modelFromNeutral } from '../src/testing/loader.js'

const directory = resolve('../conformance/vectors/migration/022-verification-is-bound-to-its-request')

function fixture() {
  const raw = JSON.parse(readFileSync(resolve(directory, 'map.json'), 'utf8'))
  const model = modelFromNeutral(JSON.parse(readFileSync(resolve(directory, 'model.json'), 'utf8')))
  const want = JSON.parse(readFileSync(resolve(directory, 'verification.json'), 'utf8'))
  return { raw, map: loadMap(raw, { model }), request: VerificationRequest.fromRecord(want.request) }
}

describe('a map fingerprint describes an immutable loaded artifact', () => {
  it('freezes the layout and owns its input independently', () => {
    const { raw, map } = fixture()
    const tables = map.groups['Reading']!.source.layout.tables
    expect(Object.isFrozen(tables)).toBe(true)
    expect(() => { (tables as Record<string, string>)['Reading'] = 'other' }).toThrow(TypeError)
    raw.groups.Reading.derived[0].layout.columns.Reading.celsius = 'text'
    expect(map.groups['Reading']!.derived[0]!.layout.columns['Reading']!['celsius']).toBe('integer')
  })

  it('does not let an object spread copy verified provenance', () => {
    const { map, request } = fixture()
    request.checkSession(map, request.projectId, request.group)
    const copied = { ...map, routing: {} }
    expect(copied.fingerprint).toBe(map.fingerprint)
    expect(() => request.checkSession(copied, request.projectId, request.group)).toThrow('canonically encodable')
  })

  it('reproduces the canonical request without accepting mutable nested targets', () => {
    const { map, request } = fixture()
    const copy = verificationRequest(map, { projectId: request.projectId, requestId: request.requestId,
      group: request.group, requestedAt: request.requestedAt })
    expect(copy.asRecord()).toEqual(request.asRecord())
    expect(Object.isFrozen(copy.targets[0])).toBe(true)
  })
})

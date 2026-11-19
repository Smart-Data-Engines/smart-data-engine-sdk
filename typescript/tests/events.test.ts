/**
 * Events through a sink the application passes: the vocabulary is exactly what the library emits,
 * the C++ library's with one stated difference, and a sink that throws changes nothing.
 *
 * Without a sink the library says nothing - tests/silence.test.ts still holds that - so every test
 * here passes one.
 */
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

import { afterEach, beforeEach, expect, it } from 'vitest'

import { EVENTS } from '../src/events.js'
import { internalFailures, loadMap, MapError, resetInternalFailures, Session } from '../src/index.js'
import { modelFromNeutral } from '../src/testing/loader.js'
import { MemoryEngine } from '../src/testing/memory.js'

const ROOT = join(import.meta.dirname, '..', '..')

function sources(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name)
    if (entry.isDirectory()) return sources(path)
    return entry.name.endsWith('.ts') ? [path] : []
  })
}

const model = modelFromNeutral({
  entities: [{ name: 'Note', fields: [{ name: 'id', type: 'string' }, { name: 'body', type: 'string' }], key: ['id'] }],
  relations: [],
  atomic: [],
})
const MAP = { contract: 3, model_version: model.version, map_version: 7,
  groups: { Note: { source: { id: 'notes@pg', engine: 'pg-main', layout: { auto: true } } } } }

beforeEach(() => resetInternalFailures())
afterEach(() => resetInternalFailures())

it('names exactly what the sources emit, in both directions', () => {
  const emitted = new Set<string>()
  for (const path of sources(join(ROOT, 'typescript', 'src'))) {
    for (const match of readFileSync(path, 'utf8').matchAll(/\bemit\([^,]+,\s*'(sde\.[a-z_]+\.[a-z_]+)'/g)) {
      emitted.add(match[1]!)
    }
  }
  expect([...emitted].sort()).toEqual([...EVENTS].sort())
})

it('is the C++ vocabulary but for the one difference it states, and every name is the reference\'s', () => {
  const cpp = readFileSync(join(ROOT, 'cpp', 'include', 'sde', 'log.hpp'), 'utf8')
  const listed = /LOG_EVENTS\[\] = \{([\s\S]*?)\};/.exec(cpp)![1]!
  const theirs = [...listed.matchAll(/"(sde\.[a-z_.]+)"/g)].map((match) => match[1]!)
  expect([...EVENTS].sort()).toEqual(theirs.filter((name) => name !== 'sde.schema.text_collation').sort())
  const reference = readFileSync(join(ROOT, 'python', 'src', 'sde', 'logging.py'), 'utf8')
  for (const name of EVENTS) expect(reference).toContain(`"${name}"`)
})

it('says a map was loaded, with what an operator asks about it', () => {
  const seen: [string, Readonly<Record<string, unknown>>][] = []
  loadMap(MAP, { model, events: (event, fields) => seen.push([event, fields]) })
  expect(seen).toEqual([['sde.map.loaded',
    { model_version: model.version, map_version: 7, signed: false, forward_only: false, key: null }]])
  expect(Object.isFrozen(seen[0]![1])).toBe(true)
})

it('says a map was refused, with its class and the reason cut at 200 code points', () => {
  const seen: [string, Readonly<Record<string, unknown>>][] = []
  expect(() => loadMap({ ...MAP, model_version: 'another' }, { model, events: (event, fields) => seen.push([event, fields]) }))
    .toThrow(MapError)
  expect(seen).toHaveLength(1)
  expect(seen[0]![0]).toBe('sde.map.rejected')
  expect(seen[0]![1]['error']).toBe('MapError')
  expect([...String(seen[0]![1]['reason'])].length).toBeLessThanOrEqual(200)
})

it('lets a sink that throws change nothing, and counts it', () => {
  const placement = loadMap(MAP, { model, events: () => { throw new Error("the application's log is down") } })
  expect(placement.mapVersion).toBe(7)
  expect(internalFailures()).toEqual({ 'events.sink': 1 })
})

it('says a session applied the schema', async () => {
  const seen: string[] = []
  const placement = loadMap(MAP, { model })
  const session = await Session.open(model, placement, { 'pg-main': new MemoryEngine({ name: 'pg-main' }) },
    { events: (event) => seen.push(event) })
  await session.ensureSchema()
  expect(seen).toEqual(['sde.schema.applied'])
})

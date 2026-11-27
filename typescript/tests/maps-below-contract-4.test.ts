/**
 * A map below contract 4 is refused where a project is configured (format contract §7r): it carries
 * no project and no write generation, so neither a write fence nor a copy's mark applies to it.
 * Without a project it is taken as before.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { loadMap, MigrationRefused, prepareSchema, Session } from '../src/index.js'
import type { Engine } from '../src/session.js'
import { modelFromNeutral } from '../src/testing/loader.js'
import { enginesFrom, Recorded } from '../src/testing/memory.js'

const CASE = resolve('../conformance/vectors/migration/017-a-verify-that-matches')
const PROJECT = '1'.repeat(32)

function fixture() {
  const model = modelFromNeutral(JSON.parse(readFileSync(resolve(CASE, 'model.json'), 'utf8')))
  const map = loadMap(JSON.parse(readFileSync(resolve(CASE, 'map.json'), 'utf8')), { model })
  expect(map.contract).toBeLessThan(4)
  const journal = new Recorded()
  const engines = enginesFrom(JSON.parse(readFileSync(resolve(CASE, 'engines.json'), 'utf8')), journal)
  return { model, map, journal, engines: engines as unknown as Record<string, Engine> }
}

describe('a map below contract 4', () => {
  it('is refused by a session with a project configured, before an engine is asked anything', async () => {
    const { model, map, journal, engines } = fixture()
    await expect(Session.open(model, map, engines, { projectId: PROJECT })).rejects.toThrow(MigrationRefused)
    await expect(Session.open(model, map, engines, { projectId: PROJECT }))
      .rejects.toThrow('carries no project and no write generation')
    expect(journal.calls).toEqual([])
  })

  it('opens without a project, as before', async () => {
    const { model, map, engines } = fixture()
    const session = await Session.open(model, map, engines)
    expect(await session.get('Reading', { id: 1 })).toEqual({ id: 1, celsius: 2 })
  })

  it('is refused by prepareSchema with a project configured, before an engine is asked anything', async () => {
    const { model, map, journal, engines } = fixture()
    await expect(prepareSchema(model, map, engines, { projectId: PROJECT }))
      .rejects.toThrow('carries no project and no write generation')
    expect(journal.calls).toEqual([])
  })

  it('is prepared without a project, as before', async () => {
    const { model, map, journal, engines } = fixture()
    await prepareSchema(model, map, engines)
    expect(new Set(journal.calls.map((call) => call.call))).toEqual(new Set(['ensure_schema']))
  })
})

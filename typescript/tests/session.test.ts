/**
 * The session's refusals, and the one mode the shared vectors cannot carry.
 *
 * Everything about *when* a fan-out happens is pinned by `migration/013`-`016`, against the same
 * in-memory engine the reference uses, so it is not repeated here. What is here is what those
 * cannot reach:
 *
 * **The refusals**, which are about a call that should never have been made - a transaction across
 * two colocation groups, a point read with half a key, a map naming an engine nobody supplied. Each
 * of them is a design error that belongs in a test run rather than in production at the moment a
 * customer triggers that code path, which is the whole reason they are refusals and not fallbacks.
 *
 * **The hashing boundary.** A vector cannot carry it because a hashed model's identifiers depend on
 * a salt, and the salt is the client's. What matters is that the application keeps saying
 * `save('Reading', { celsius })` while everything downstream speaks digests - without that, the
 * mode is unusable, because a client would have to write the digests in their own source.
 */

import { randomBytes } from 'node:crypto'

import { beforeEach, describe, expect, it } from 'vitest'

import type { LogicalModel } from '../src/index.js'
import {
  buildModel,
  colocationGroups,
  EngineError,
  entity,
  hashIdentifiers,
  loadMap,
  ModelPlanningError,
  ref,
  Session,
  T,
} from '../src/index.js'
import { MemoryEngine } from '../src/testing/memory.js'

/**
 * Two colocation groups, and one of them has a relation.
 *
 * Both halves are needed. **Two groups**, because the refusal that matters most here is a
 * transaction spanning them and a one-group model cannot reach it. **A relation**, because it is
 * what unions Reading and Station into one group by declaration rather than by a flag.
 *
 * Declared explicitly rather than read from types, which is this language's whole reason for being
 * the second implementation: TypeScript's types are erased before the code runs, so anything the
 * format contract left implicit had nowhere to hide.
 */
function model(): LogicalModel {
  return buildModel([
    entity('Station', { fields: { id: T.uuid, label: T.string } }),
    entity('Reading', {
      fields: { id: T.uuid, celsius: T.int32 },
      relations: { at: ref('Station') },
      atomicWith: ['Station'],
    }),
    entity('Note', { fields: { id: T.uuid, body: T.string } }),
  ])
}

function mapFor(built: LogicalModel): Record<string, unknown> {
  const groups: Record<string, unknown> = {}
  for (const group of colocationGroups(built)) {
    const tables: Record<string, string> = {}
    const columns: Record<string, Record<string, string>> = {}
    for (const member of group.members) {
      tables[member] = member.toLowerCase()
      columns[member] = { id: 'uuid' }
    }
    groups[group.name] = {
      source: {
        id: `${group.name}@pg`,
        engine: 'pg-main',
        layout: { tables, columns },
      },
    }
  }
  return {
    contract: 1,
    model_version: built.version,
    map_version: 1,
    groups,
  }
}

describe('a session refuses what it cannot answer', () => {
  let built: LogicalModel

  beforeEach(() => {
    built = model()
  })

  it('will not open against a map naming an engine nobody supplied', async () => {
    const placement = loadMap(mapFor(built), { model: built })
    await expect(Session.open(built, placement, {})).rejects.toThrow(EngineError)
    await expect(Session.open(built, placement, {})).rejects.toThrow(
      'refers to engines that were not supplied',
    )
  })

  it('will not span colocation groups in one transaction', async () => {
    const placement = loadMap(mapFor(built), { model: built })
    const session = await Session.open(built, placement, { 'pg-main': new MemoryEngine() })
    // Reading and Station are one group by declaration; Note is its own.
    await expect(
      session.transaction(['Reading', 'Note'], async () => undefined),
    ).rejects.toThrow(ModelPlanningError)
    await expect(
      session.transaction(['Reading', 'Note'], async () => undefined),
    ).rejects.toThrow('there is no distributed transaction here and there will not be one')
  })

  it('takes a transaction over one group, and over the whole model only when there is one', async () => {
    const placement = loadMap(mapFor(built), { model: built })
    const session = await Session.open(built, placement, { 'pg-main': new MemoryEngine() })
    // One group named explicitly is fine.
    await expect(session.transaction(['Reading'], async () => 7)).resolves.toBe(7)
    // No entities means the whole model, which this one cannot have: two groups.
    await expect(session.transaction([], async () => undefined)).rejects.toThrow(
      'cannot span colocation groups',
    )
  })

  it('will not answer a point read with half a key', async () => {
    const placement = loadMap(mapFor(built), { model: built })
    const session = await Session.open(built, placement, { 'pg-main': new MemoryEngine() })
    await expect(session.get('Reading', {})).rejects.toThrow(
      'A partial key is a range read, which is a different shape',
    )
  })

  it('names an adapter it does not have rather than returning undefined', async () => {
    const placement = loadMap(mapFor(built), { model: built })
    const session = await Session.open(built, placement, { 'pg-main': new MemoryEngine() })
    expect(session.engineNames()).toEqual(['pg-main'])
    expect(() => session.engineNamed('ch-1')).toThrow('has no adapter named')
  })
})

describe('the hashing boundary', () => {
  it('lets the application keep its own names while everything below speaks digests', async () => {
    const built = model()
    const { model: hashed, names } = hashIdentifiers(built, randomBytes(32))
    const placement = loadMap(mapFor(hashed), { model: hashed })
    const engine = new MemoryEngine()
    const session = await Session.open(hashed, placement, { 'pg-main': engine }, { names })

    const id = '11111111-1111-1111-1111-111111111111'
    await session.save('Reading', { id })
    const back = await session.get('Reading', { id })

    // The client's vocabulary on the way in and on the way out.
    expect(back).toEqual({ id })
    // And the digests underneath: the table is named after the hashed entity, and the column after
    // the hashed field. Nothing in the client's source has to know either.
    const table = Object.keys(engine.tables).find((name) => engine.tables[name]?.length === 1)
    expect(table).toBeDefined()
    expect(table).not.toBe('reading')
    const stored = engine.tables[table as string]?.[0] as Record<string, unknown>
    expect(Object.keys(stored)).not.toContain('id')
    expect(Object.values(stored)).toEqual([id])
  })

  it('puts a refusal back into the client vocabulary too', async () => {
    // With hashing on, an error naming digests tells a client nothing about their own code.
    const built = model()
    const { model: hashed, names } = hashIdentifiers(built, randomBytes(32))
    const placement = loadMap(mapFor(hashed), { model: hashed })
    const session = await Session.open(
      hashed,
      placement,
      { 'pg-main': new MemoryEngine() },
      { names },
    )
    await expect(session.get('Reading', {})).rejects.toThrow(/needs exactly its key \[id\]/)
  })
})

describe('ensureSchema reaches every materialisation the map names', () => {
  it('applies to the source of each group', async () => {
    const built = model()
    const placement = loadMap(mapFor(built), { model: built })
    const engine = new MemoryEngine()
    const session = await Session.open(built, placement, { 'pg-main': engine })
    await session.ensureSchema()
    const applied = engine.recorded.calls.filter((call) => call.call === 'ensure_schema')
    // One per group, and both groups are placed in this engine.
    expect(applied).toHaveLength(colocationGroups(built).length)
  })
})

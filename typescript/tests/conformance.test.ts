/**
 * The conformance runner, TypeScript side.
 *
 * The same vectors the Python library runs, in vitest instead of pytest. This file is the entire
 * mechanism by which two implementations stay identical, and it is short on purpose: a runner with
 * logic of its own would be a third implementation to keep true.
 *
 * `ir.json` is compared as **bytes**. Parsing it and comparing structures would pass two libraries
 * that agree on the structure while disagreeing on key order or Unicode normalisation - which is
 * precisely the failure these vectors exist to catch, and it is invisible the moment you parse.
 */

import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  BUCKET_COUNT,
  canonicalBytes,
  CanonicalError,
  colocationGroups,
  compatibilityViews,
  CONTRACT,
  DeclarationError,
  DIALECTS,
  EngineError,
  enumerateShapes,
  featuresRecord,
  hashIdentifiers,
  hasTimeDimension,
  Histogram,
  loadMap,
  MapError,
  MEASURED_FIELDS,
  placementOf,
  Recorder,
  resolve,
  schemaIsFixed,
  schemaStatements,
  SHAPE_KINDS,
  shapeId,
  shapeIr,
  windowCopies,
  windowFeatures,
  windowRecord,
} from '../src/index.js'
import {
  backfill,
  backfillRecord,
  compareCodePoints,
  copyFreshnessRecord,
  enforceForwardOnly,
  MapRolledBack,
  MigrationRefused,
  Session,
  verify,
  verifyRecord,
  watermarkRecord,
} from '../src/index.js'
import type { MemoryEngine } from '../src/testing/memory.js'
import { enginesFrom } from '../src/testing/memory.js'
import type { LogicalModel, Materialization, PlacementMap } from '../src/index.js'
import { modelFromNeutral } from '../src/testing/loader.js'

const CONFORMANCE = join(import.meta.dirname ?? __dirname, '..', '..', 'conformance')
const VECTORS = join(CONFORMANCE, 'vectors')

function cases(kind: string): string[] {
  return readdirSync(join(VECTORS, kind), { withFileTypes: true })
    .filter((e) => e.isDirectory())
    .map((e) => e.name)
    .sort()
}

function readJson<T>(path: string): T {
  return JSON.parse(readFileSync(path, 'utf8')) as T
}

it('implements the contract version the vectors describe', () => {
  const declared = Number(readFileSync(join(CONFORMANCE, 'contract-version.txt'), 'utf8').trim())
  expect(declared).toBe(CONTRACT)
})

it('found vectors at all', () => {
  // A green suite that ran zero vectors is worse than a red one.
  expect(cases('model').length).toBeGreaterThan(0)
  expect(cases('routing').length).toBeGreaterThan(0)
  expect(cases('errors').length).toBeGreaterThan(0)
})

describe('model vectors', () => {
  for (const name of cases('model')) {
    it(name, () => {
      const dir = join(VECTORS, 'model', name)
      const model = modelFromNeutral(readJson(join(dir, 'model.json')))

      const expected = readFileSync(join(dir, 'ir.json'))
      const actual = canonicalBytes(model.ir)
      if (!actual.equals(expected)) {
        // Show the first differing byte: the difference is almost always key order or
        // normalisation, and a diff of two long single-line strings is unreadable otherwise.
        const limit = Math.min(actual.length, expected.length)
        let at = limit
        for (let i = 0; i < limit; i += 1) {
          if (actual[i] !== expected[i]) {
            at = i
            break
          }
        }
        throw new Error(
          `canonical IR differs at byte ${at}\n` +
            `expected: ...${expected.subarray(Math.max(0, at - 40), at + 40).toString('utf8')}...\n` +
            `actual:   ...${actual.subarray(Math.max(0, at - 40), at + 40).toString('utf8')}...`,
        )
      }

      expect(model.version).toBe(readFileSync(join(dir, 'version.txt'), 'utf8').trim())

      const groups = colocationGroups(model).map((g) => ({ name: g.name, members: [...g.members] }))
      expect(groups).toEqual(readJson(join(dir, 'groups.json')))

      let shapesFile: unknown
      try {
        shapesFile = readJson(join(dir, 'shapes.json'))
      } catch {
        return
      }
      const shapes = enumerateShapes(model).map((s) => ({ ...shapeIr(s), id: shapeId(s) }))
      expect(shapes).toEqual(shapesFile)
    })
  }
})

describe('routing vectors', () => {
  for (const name of cases('routing')) {
    it(name, () => {
      const dir = join(VECTORS, 'routing', name)
      const model = modelFromNeutral(readJson(join(dir, 'model.json')))
      const map = loadMap(readJson(join(dir, 'map.json')), { model })

      const byId = new Map(enumerateShapes(model).map((s) => [shapeId(s), s]))
      const expectations = readJson<
        Array<{
          shape: string
          expect: string
          fresh?: boolean
          in_write_transaction?: boolean
        }>
      >(join(dir, 'cases.json'))

      for (const expectation of expectations) {
        const shape = byId.get(expectation.shape)
        expect(
          shape,
          `the vector refers to shape ${expectation.shape}, which this library does not enumerate. ` +
            'Either the enumeration diverged or the vector is stale.',
        ).toBeDefined()
        const got = resolve(map, shape!, {
          inWriteTransaction: expectation.in_write_transaction === true,
          fresh: expectation.fresh === true,
        })
        expect(got.id, `${shape!.entity}.${shape!.kind}`).toBe(expectation.expect)
      }

      // Optional, and present only for a map that fans writes out. Asserted here rather than in a
      // TypeScript-only test because a fan-out target read differently in two languages is a row
      // written to one copy and not the other - the divergence class this suite exists for, and
      // the one that produces no error at the time it happens.
      const fanOutFile = join(dir, 'also_write.json')
      if (existsSync(fanOutFile)) {
        const expected = readJson<Record<string, string[]>>(fanOutFile)
        for (const group of Object.keys(expected).sort()) {
          const got = placementOf(map, group).alsoWrite.map((m) => m.id)
          expect(got, `group ${group} fan-out`).toEqual(expected[group])
        }
      }
    })
  }
})

// One mapping from the name a vector writes to the class, shared by every family, so that a
// family added later cannot introduce a second spelling of "which error".
const ERRORS: Record<string, new (...args: never[]) => Error> = {
  DeclarationError,
  EngineError,
  MapError,
  MapRolledBack,
  MigrationRefused,
}

interface ErrorExpectation {
  readonly error: string
  readonly stage: string
  readonly match: string
  readonly load?: { readonly require_signature?: boolean; readonly public_key?: string }
}

describe('error vectors', () => {
  for (const name of cases('errors')) {
    it(name, () => {
      const dir = join(VECTORS, 'errors', name)
      const expected = readJson<ErrorExpectation>(join(dir, 'expected.json'))
      const ctor = ERRORS[expected.error]
      expect(ctor, `unknown error class ${expected.error}`).toBeDefined()

      // The stage matters as much as the class. A library that raises the right error when a query
      // runs, rather than when the model is built, has a different bug that a type-only assertion
      // cannot see.
      expect(
        ['model', 'map'],
        `${name} expects the error at stage '${expected.stage}', which this runner does not know ` +
          'how to exercise yet. Failing rather than skipping: a stage nobody runs is a rule ' +
          'nobody checks.',
      ).toContain(expected.stage)

      if (expected.stage === 'model') {
        expect(() => modelFromNeutral(readJson(join(dir, 'model.json')))).toThrow(
          new RegExp(expected.match),
        )
        return
      }

      // A map-stage vector has a *valid* model, and building it happens outside the assertion. A
      // vector whose model was broken by accident would otherwise throw at the model stage and
      // satisfy an assertion that only looks at the class and the message - failing at the wrong
      // stage entirely, which is the bug the `stage` field exists to catch.
      const model = modelFromNeutral(readJson(join(dir, 'model.json')))
      const raw = readJson<unknown>(join(dir, 'map.json'))
      const load = expected.load ?? {}
      // Built up rather than passed inline: `exactOptionalPropertyTypes` distinguishes an absent
      // key from one set to undefined, and the library's contract is "no key was provided", which
      // is the absent case.
      const options: { model: LogicalModel; publicKey?: Uint8Array; requireSignature?: boolean } = {
        model,
        requireSignature: load.require_signature === true,
      }
      if (load.public_key) options.publicKey = Buffer.from(load.public_key, 'base64')
      expect(() => loadMap(raw, options)).toThrow(new RegExp(expected.match))
    })
  }
})

it('covers both error stages with actual vectors', () => {
  // A stage the runner supports and no vector uses is a rule that reads as covered. Before the map
  // stage existed, every refusal in section 7 of the contract was checked in Python's own tests and
  // in nothing shared - which is how one message came to render a literal '{CONTRACT}' in Python
  // and the number here.
  const stages = new Set(
    cases('errors').map(
      (name) => readJson<ErrorExpectation>(join(VECTORS, 'errors', name, 'expected.json')).stage,
    ),
  )
  expect([...stages].sort()).toEqual(['map', 'model'])
})


// --- signature vectors ------------------------------------------------------------------------
//
// Accepting a **set** of public keys is what makes rotating our signing key possible without
// breaking a client, and an acceptance rule that holds in one runtime and not another is one map
// with two meanings, with nothing compiling differently. This family is also the only one whose
// expectations were produced by openssl rather than by either library - see
// conformance/tools/signature_vectors.py.

interface SignatureExpectation {
  readonly verified_with?: string | null
  readonly error?: string
  readonly match?: string
}

describe('signature vectors', () => {
  for (const name of cases('signature')) {
    it(name, () => {
      const dir = join(VECTORS, 'signature', name)
      const model = modelFromNeutral(readJson(join(dir, 'model.json')))
      const raw = readJson<unknown>(join(dir, 'map.json'))
      const encoded = readJson<Record<string, string>>(join(dir, 'keys.json'))
      const expected = readJson<SignatureExpectation>(join(dir, 'expected.json'))

      // A single entry under the empty name is the bare-key form. It is not the same call as a
      // one-entry mapping, and the difference is what the library reports back afterwards.
      const names = Object.keys(encoded)
      const publicKey: Uint8Array | Record<string, Uint8Array> =
        names.length === 1 && names[0] === ''
          ? Buffer.from(encoded[''] as string, 'base64')
          : Object.fromEntries(
              Object.entries(encoded).map(([id, value]) => [id, Buffer.from(value, 'base64')]),
            )

      const options = { model, publicKey, requireSignature: true }
      if (expected.error !== undefined) {
        expect(ERRORS[expected.error], `unknown error class ${expected.error}`).toBeDefined()
        expect(() => loadMap(raw, options)).toThrow(new RegExp(expected.match as string))
        return
      }

      const placement = loadMap(raw, options)
      expect(placement.signed).toBe(true)
      expect(placement.verifiedWith).toEqual(expected.verified_with ?? null)
    })
  }
})

it('covers both signature outcomes with actual vectors', () => {
  // A family of nothing but refusals proves a library can refuse, never that it can accept - the
  // reason 12.11 needed its second half.
  const outcomes = new Set(
    cases('signature').map(
      (name) =>
        readJson<SignatureExpectation>(join(VECTORS, 'signature', name, 'expected.json')).error !==
        undefined,
    ),
  )
  expect([...outcomes].sort()).toEqual([false, true])
})


// --- canonical vectors ------------------------------------------------------------------------
//
// These feed a value straight into the encoder rather than going through a model, and they exist
// because of a mutation that should have failed and did not. Every object key in the model IR is
// fixed ASCII, so the object-key comparator was never exercised: swapping code point ordering for
// JavaScript's default UTF-16 comparison passed the whole suite. Field names do reach the IR, but as
// array elements, which is a different call site.

describe('canonical vectors', () => {
  const names = cases('canonical')

  it('found some', () => {
    expect(names.length).toBeGreaterThan(0)
  })

  for (const name of names) {
    it(name, () => {
      const dir = join(VECTORS, 'canonical', name)
      const raw = readFileSync(join(dir, 'value.json'), 'utf8')
      const value: unknown = JSON.parse(raw)

      let expectedError: { error: string; match: string } | null = null
      try {
        expectedError = readJson<{ error: string; match: string }>(join(dir, 'expected.json'))
      } catch {
        expectedError = null
      }

      if (expectedError) {
        expect(expectedError.error).toBe('CanonicalError')
        // A parser that collapses two keys differing only in composition cannot present this case to
        // the encoder at all. Skipping loudly beats passing for the wrong reason.
        if (name.includes('duplicate-key')) {
          const keys = Object.keys(value as Record<string, unknown>)
          if (keys.length < 2) {
            expect(
              keys.length,
              "this runtime's JSON parser collapsed the two spellings, so the encoder never sees " +
                'the duplicate. Vector skipped rather than passed.',
            ).toBe(1)
            return
          }
        }
        expect(() => canonicalBytes(value)).toThrow(CanonicalError)
        expect(() => canonicalBytes(value)).toThrow(new RegExp(expectedError.match))
        return
      }

      const expected = readFileSync(join(dir, 'bytes.json'))
      const actual = canonicalBytes(value)
      expect(
        actual.toString('utf8'),
        `see why.txt in ${name}: every expectation here was written by hand from the format ` +
          'contract, so a mismatch means this implementation drifted from the document.',
      ).toBe(expected.toString('utf8'))
    })
  }
})

// --- hashing vectors ---------------------------------------------------------------------------
//
// Only run by a library that offers hashing (section 2a), which this one now does. What these pin is
// not the HMAC - anything computes an HMAC - but the message: NFC first, U+0000 as the separator, the
// prefix outside, fields hashed with their entity. All four are invisible in an ASCII-only test, and
// three of them are what a line-by-line translation of the Python would plausibly get wrong.

describe('hashing vectors', () => {
  const kind = cases('hashing')

  it('found vectors to run', () => {
    // A library that claims section 2a and silently runs zero of these is the failure the vectors
    // exist to make impossible.
    expect(kind.length).toBeGreaterThan(0)
  })

  for (const name of kind) {
    it(name, () => {
      const dir = join(VECTORS, 'hashing', name)
      const salt = Buffer.from(readFileSync(join(dir, 'salt.hex'), 'utf8').trim(), 'hex')
      const model = modelFromNeutral(readJson(join(dir, 'model.json')))
      const { model: hashed, names } = hashIdentifiers(model, salt)

      const expected = readJson<{
        entities: Record<string, string>
        fields: Record<string, Record<string, string>>
        relations: Record<string, Record<string, string>>
      }>(join(dir, 'names.json'))

      expect(names.entities).toEqual(expected.entities)
      expect(names.fields).toEqual(expected.fields)
      expect(names.relations).toEqual(expected.relations)

      expect(canonicalBytes(hashed.ir)).toEqual(readFileSync(join(dir, 'ir.json')))
      expect(hashed.version).toBe(readFileSync(join(dir, 'version.txt'), 'utf8').trim())
      expect(
        colocationGroups(hashed).map((g) => ({ name: g.name, members: [...g.members] })),
      ).toEqual(readJson(join(dir, 'groups.json')))

      // Where the case carries the same identifiers in a second normal form, the two must agree. A
      // library that hashes before normalising passes everything above and fails here.
      const decomposedPath = join(dir, 'model-decomposed.json')
      if (existsSync(decomposedPath)) {
        expect(readFileSync(join(dir, 'model.json'))).not.toEqual(readFileSync(decomposedPath))
        const other = hashIdentifiers(modelFromNeutral(readJson(decomposedPath)), salt)
        expect(other.model.version).toBe(
          readFileSync(join(dir, 'version-decomposed.txt'), 'utf8').trim(),
        )
        expect(other.model.version).toBe(hashed.version)
      }
    })
  }
})

/**
 * Tier 2, first half: the DDL a layout renders to, byte for byte.
 *
 * The input reaches the renderer through the same map loader production uses - a whole `map.json`
 * rather than a bare layout document - so a case can only pin DDL for a layout this library would
 * accept in the first place, and there is no second parser written for the vectors.
 *
 * Statements are compared **exactly**, because they are bytes an engine receives. Refusals are
 * compared by substring, because they are diagnostics, exactly as the `errors/` family does.
 */

interface SchemaCase {
  readonly materialization: string
  readonly dialect: string
  readonly fixed?: boolean
  readonly fixed_error?: { readonly error: string; readonly match: string }
  readonly statements?: readonly string[]
  readonly error?: string
  readonly match?: string
  readonly keys?: Readonly<Record<string, readonly string[]>>
  readonly layout_columns?: Readonly<Record<string, Readonly<Record<string, string>>>>
  readonly views?: {
    readonly was: Readonly<Record<string, string>>
    readonly create: readonly string[]
    readonly drop: readonly string[]
    readonly complete: boolean
    readonly not_possible: readonly { readonly entity: string; readonly match: readonly string[] }[]
  }
}

/**
 * The materialisation with this id, and the group it belongs to.
 *
 * Searched across every group rather than taken from a field in the case, because a map's ids are
 * unique across the whole document - `errors/013` pins that - so a case naming the group as well
 * would carry a fact the map already carries.
 */
function materialization(map: PlacementMap, id: string): [string, Materialization] {
  for (const group of Object.keys(map.groups).sort()) {
    const placement = placementOf(map, group)
    for (const found of [placement.source, ...placement.derived]) {
      if (found.id === id) return [group, found]
    }
  }
  throw new Error(`the vector names materialisation ${id}, which this map does not have`)
}

function refuses(fn: () => unknown, name: string, match: string): void {
  let thrown: unknown
  try {
    fn()
  } catch (error) {
    thrown = error
  }
  const expected = ERRORS[name]
  expect(thrown, `expected a ${name} containing ${match}`).toBeInstanceOf(expected)
  expect((thrown as Error).message).toContain(match)
}

describe('schema vectors', () => {
  for (const name of cases('schema')) {
    it(name, () => {
      const dir = join(VECTORS, 'schema', name)
      const model = modelFromNeutral(readJson(join(dir, 'model.json')))
      const map = loadMap(readJson(join(dir, 'map.json')), { model })

      for (const expectation of readJson<SchemaCase[]>(join(dir, 'cases.json'))) {
        const [group, found] = materialization(map, expectation.materialization)
        // The one place a case edits its own input. `columns` is optional in the document, so a map
        // naming tables alone loads cleanly - and once it has loaded there is no other way to
        // express "the map said nothing about columns".
        const layout =
          expectation.layout_columns === undefined
            ? found.layout
            : { ...found.layout, columns: expectation.layout_columns }
        const members = colocationGroups(model).find((g) => g.name === group)!
        const keys =
          expectation.keys ??
          Object.fromEntries(
            members.members.map((entity) => [
              entity,
              model.entities.find((e) => e.name === entity)!.key,
            ]),
          )
        const dialect = expectation.dialect

        if (expectation.fixed !== undefined) {
          expect(schemaIsFixed(dialect), `schemaIsFixed(${dialect})`).toBe(expectation.fixed)
        } else {
          const want = expectation.fixed_error!
          refuses(() => schemaIsFixed(dialect), want.error, want.match)
        }

        if (expectation.error !== undefined) {
          refuses(
            () => schemaStatements(layout, { keys, dialect }),
            expectation.error,
            expectation.match!,
          )
          continue
        }

        expect(
          [...schemaStatements(layout, { keys, dialect })],
          `${found.id} as ${dialect}: the DDL differs from the vector. Two libraries that agree ` +
            'on a map and disagree here place one entity in tables with different columns, and ' +
            'each of them created a table successfully.',
        ).toEqual(expectation.statements)

        const views = expectation.views
        if (views !== undefined) {
          const rendered = compatibilityViews(layout, { was: views.was, dialect })
          expect([...rendered.create]).toEqual(views.create)
          expect([...rendered.drop]).toEqual(views.drop)
          expect(rendered.complete).toBe(views.complete)
          expect(rendered.notPossible.map(([entity]) => entity)).toEqual(
            views.not_possible.map((entry) => entry.entity),
          )
          rendered.notPossible.forEach(([entity, why], index) => {
            for (const fragment of views.not_possible[index]!.match) {
              expect(
                why,
                `the reason ${entity} cannot have a view has to contain ${fragment}. A reason may ` +
                  'say more than the vector, in any language, and may not say less.',
              ).toContain(fragment)
            }
          })
        }
      }
    })
  }

  it('found schema vectors at all', () => {
    // This library reaches Tier 2, so these do not get to be skipped.
    expect(cases('schema').length).toBeGreaterThan(0)
  })

  it('covers every dialect this library renders', () => {
    // Totality, in the direction that rots. A dialect with no vector is one where two libraries can
    // disagree and nothing shared would notice.
    const covered = new Set(
      cases('schema').flatMap((name) =>
        readJson<SchemaCase[]>(join(VECTORS, 'schema', name, 'cases.json')).map((c) => c.dialect),
      ),
    )
    expect(DIALECTS.filter((d) => !covered.has(d))).toEqual([])
  })
})

/**
 * Tier 1: the window document, and every derivation behind it.
 *
 * **Compared as numbers rather than as bytes, and that is the one exception in this suite.**
 * Section 1 of the contract rejects floating point outright because a float's textual form differs
 * between languages, and almost every number in a window is a float. The document is not signed,
 * not hashed and never compared for equality, so the rule does not apply - and the property that
 * makes this family checkable is narrower: every number here is either a ratio of two integers or a
 * bucket edge divided by a million, and IEEE 754 requires division to be correctly rounded. Two
 * languages compute the same double; only their printing differs.
 */

interface Operation {
  readonly shape: string
  readonly ns: number
  readonly rows?: number
  readonly failed?: boolean
}

interface FanOutEntry {
  readonly group: string
  readonly materialization: string
  readonly ns: number
  readonly failed?: boolean
}

/**
 * Feed a case's operations to a recorder and close the window.
 *
 * Shapes are named by **identifier**, like the `routing/` cases, so the runner has to enumerate the
 * model and look them up - which means the group, the entity and the kind reach the recorder from
 * this library's own enumeration rather than from the vector. A case cannot therefore pin a
 * classification by asserting it in its own input.
 */
function recorded(model: LogicalModel, operations: readonly Operation[], dir: string) {
  const byId = new Map(enumerateShapes(model).map((shape) => [shapeId(shape), shape]))
  const recorder = new Recorder(model.version)
  for (const operation of operations) {
    const shape = byId.get(operation.shape)
    expect(
      shape,
      `the vector refers to shape ${operation.shape}, which this library does not enumerate`,
    ).toBeDefined()
    recorder.record({
      shapeId: shapeId(shape!),
      group: shape!.group,
      entity: shape!.entity,
      kind: shape!.kind,
      nanoseconds: operation.ns,
      rows: operation.rows ?? 0,
      failed: operation.failed === true,
    })
  }
  const fanOutFile = join(dir, 'fan_out.json')
  if (existsSync(fanOutFile)) {
    for (const entry of readJson<FanOutEntry[]>(fanOutFile)) {
      recorder.recordFanOut({
        group: entry.group,
        materialization: entry.materialization,
        nanoseconds: entry.ns,
        failed: entry.failed === true,
      })
    }
  }
  const window = recorder.roll()
  expect(window, 'the case recorded nothing, so it pins nothing').toBeDefined()
  return window!
}

/**
 * Tier 2, second half: taking part in a migration.
 *
 * Two things are pinned and the second is the unusual one. **The record** is what a gate on our side
 * reads - a backfill's progress, a verify's seven counts. **The calls** are how that record was
 * obtained: a library that reached the same counts by scanning the whole table and filtering in
 * memory would satisfy every number and be unusable on a real one.
 *
 * The fixture is the library's own in-memory engine rather than one written here, because a runner
 * that writes its own is a runner whose *fixture* can be the thing that differs - and then a red
 * vector says "one of two tables disagreed" instead of "one of two libraries disagreed".
 */

interface MigrationLoad {
  readonly require_signature?: boolean
  readonly public_key?: string
}

async function refusesAsync(fn: () => Promise<unknown>, name: string, match: string): Promise<void> {
  let thrown: unknown
  try {
    await fn()
  } catch (error) {
    thrown = error
  }
  const expected = ERRORS[name]
  expect(thrown, `expected a ${name} containing ${match}`).toBeInstanceOf(expected)
  expect((thrown as Error).message).toContain(match)
}

/**
 * How a case asks its transaction to roll back.
 *
 * An error thrown by the runner rather than a flag on the session, because a rollback is what an
 * application's own failure looks like: the guarantee under test is that a transaction which does
 * not complete leaves nothing in the copy, and the only honest way to reach it is to fail.
 */
class Rollback extends Error {}

interface SessionStep {
  readonly op: string
  readonly entity?: string
  readonly values?: Record<string, unknown>
  readonly entities?: readonly string[]
  readonly body?: readonly SessionStep[]
  readonly rollback?: boolean
}

/**
 * Run a case's session operations and compare what each engine ended up holding.
 *
 * The heart of Tier 2 and the part no document transformation can reach: a fan-out is a second
 * write in the client's own process, and every rule about it is about *when* it happens.
 */
async function driveSession(
  dir: string,
  model: LogicalModel,
  map: PlacementMap,
  engines: Record<string, MemoryEngine>,
): Promise<void> {
  const recorder = new Recorder(model.version)
  const session = await Session.open(model, map, engines, { recorder })

  const run = async (steps: readonly SessionStep[]): Promise<void> => {
    for (const step of steps) {
      if (step.op === 'save') {
        await session.save(step.entity as string, step.values as Record<string, unknown>)
      } else if (step.op === 'transaction') {
        try {
          await session.transaction(step.entities ?? [], async () => {
            await run(step.body ?? [])
            if (step.rollback === true) throw new Rollback()
          })
        } catch (error) {
          if (!(error instanceof Rollback)) throw error
        }
      } else {
        throw new Error(`unknown operation ${step.op}`)
      }
    }
  }
  await run(readJson<SessionStep[]>(join(dir, 'operations.json')))

  const gotTables: Record<string, unknown> = {}
  for (const name of Object.keys(engines).sort()) {
    const engine = engines[name] as MemoryEngine
    const tables: Record<string, unknown> = {}
    for (const table of Object.keys(engine.tables).sort()) {
      tables[table] = [...(engine.tables[table] as Record<string, unknown>[])].sort((a, b) =>
        JSON.stringify(sortedKeys(a)) < JSON.stringify(sortedKeys(b)) ? -1 : 1,
      )
    }
    gotTables[name] = tables
  }
  expect(
    gotTables,
    'the engines do not hold what the vector says. A fan-out written at the wrong moment loses ' +
      'exactly the rows a migration exists not to lose, and nothing raises.',
  ).toEqual(readJson(join(dir, 'tables.json')))

  const window = recorder.roll()
  const group = Object.keys(map.groups).sort()[0] as string
  const copies = window === undefined ? [] : windowCopies(window, group)
  const wanted = readJson<Record<string, unknown>[]>(join(dir, 'copies.json'))
  expect(copies).toHaveLength(wanted.length)
  copies.forEach((copy, index) => {
    const record = copyFreshnessRecord(copy)
    const want = wanted[index] as Record<string, unknown>
    for (const measured of ['lag_p50_ms', 'lag_p99_ms']) {
      // Elapsed time, so the vector carries a placeholder. What is a property of the library rather
      // than of the clock is that the field is there and is a number or null.
      expect(want[measured]).toBe('<any>')
      const value = record[measured]
      expect(value === null || typeof value === 'number').toBe(true)
      record[measured] = '<any>'
    }
    expect(record).toEqual(want)
  })
}

/** A row with its keys in order, so two rows compare the same way in both languages. */
function sortedKeys(row: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const key of Object.keys(row).sort(compareCodePoints)) out[key] = row[key]
  return out
}


describe('migration vectors', () => {
  for (const name of cases('migration')) {
    it(name, async () => {
      const dir = join(VECTORS, 'migration', name)
      const model = modelFromNeutral(readJson(join(dir, 'model.json')))
      const load: MigrationLoad = existsSync(join(dir, 'load.json'))
        ? readJson(join(dir, 'load.json'))
        : {}
      const keys: Record<string, string> = existsSync(join(dir, 'keys.json'))
        ? readJson(join(dir, 'keys.json'))
        : {}
      const named = load.public_key
      const map = loadMap(readJson(join(dir, 'map.json')), {
        model,
        ...(named === undefined
          ? {}
          : { publicKey: Buffer.from(keys[named] as string, 'base64') }),
        requireSignature: load.require_signature === true,
      })
      const engines = enginesFrom(readJson(join(dir, 'engines.json')))

      const watermarkFile = join(dir, 'watermark.json')
      if (existsSync(watermarkFile)) {
        const want = readJson<{
          error?: string
          match?: string
          expect?: unknown
          why_match?: string[]
        }>(watermarkFile)
        if (want.error !== undefined) {
          await refusesAsync(
            () => enforceForwardOnly(map, engines),
            want.error,
            want.match as string,
          )
        } else {
          const got = await enforceForwardOnly(map, engines)
          const { why, ...record } = watermarkRecord(got)
          expect(
            record,
            'the forward-only check disagrees with the vector. This decides whether a client can ' +
              'be silently reverted to a previous placement.',
          ).toEqual(want.expect)
          // `why` is prose, so it is pinned by substring like every other diagnostic here.
          for (const fragment of want.why_match ?? []) {
            expect(String(why)).toContain(fragment)
          }
        }
      }

      let session: Session | undefined
      const backfillFile = join(dir, 'backfill.json')
      if (existsSync(backfillFile)) {
        const want = readJson<{
          group: string
          options?: { chunk_rows?: number; stop_after?: number }
          error?: string
          match?: string
          progress?: unknown
        }>(backfillFile)
        session = await Session.open(model, map, engines)
        const options = {
          ...(want.options?.chunk_rows === undefined ? {} : { chunkRows: want.options.chunk_rows }),
          ...(want.options?.stop_after === undefined ? {} : { stopAfter: want.options.stop_after }),
        }
        if (want.error !== undefined) {
          await refusesAsync(
            () => backfill(session as Session, want.group, options),
            want.error,
            want.match as string,
          )
        } else {
          const progress = await backfill(session, want.group, options)
          expect(backfillRecord(progress)).toEqual(want.progress)
        }
      }

      const verifyFile = join(dir, 'verify.json')
      if (existsSync(verifyFile)) {
        const want = readJson<{
          group: string
          options?: { chunk_rows?: number }
          report: Record<string, unknown>
          matched: boolean
          differences: unknown[]
        }>(verifyFile)
        session ??= await Session.open(model, map, engines)
        const report = await verify(session, want.group, {
          ...(want.options?.chunk_rows === undefined ? {} : { chunkRows: want.options.chunk_rows }),
        })
        // `at` is a clock reading, so the vector carries a placeholder rather than an instant: a
        // vector with a timestamp in it is a vector that expires.
        expect({ ...verifyRecord(report), at: '<any>' }).toEqual(want.report)
        expect(report.matched).toBe(want.matched)
        expect(
          report.differences.map((difference) => ({
            entity: difference.entity,
            table: difference.table,
            key: difference.key,
            columns: [...difference.columns],
          })),
          'the differences differ. These hold the client\'s own key values and are deliberately ' +
            'absent from the record that crosses the boundary.',
        ).toEqual(want.differences)
      }

      const operationsFile = join(dir, 'operations.json')
      if (existsSync(operationsFile)) {
        await driveSession(dir, model, map, engines)
      }

      const callsFile = join(dir, 'calls.json')
      if (existsSync(callsFile)) {
        // **One sequence for the whole engine set.** Per-engine lists cannot express the guarantee
        // the dual-write cases are about - a row reaches the source before anything is attempted
        // against the copy - and reversing those two lines passed every vector while one of them
        // claimed in writing that the ordering was what it pinned.
        const first = Object.values(engines)[0] as MemoryEngine
        expect(
          first.recorded.calls,
          'the calls this library made to the engines differ from the vector. The counts can be ' +
            'right and the calls wrong - that is a library that works on a fixture and not on a ' +
            'table.',
        ).toEqual(readJson(callsFile))
      }
    })
  }

  it('found migration vectors at all', () => {
    expect(cases('migration').length).toBeGreaterThan(0)
  })

  it('pins an engine the no-account mode must not touch', () => {
    // Section 12.6 promises that in the no-account mode this library does nothing at all - no
    // table, no query, no cost. That is a claim about calls that were *not* made, and a vector
    // holding an empty list is easy to satisfy by accident: a runner that never built the engines
    // would pass it. So the same document is read from the other side.
    const empty: string[] = []
    const busy: string[] = []
    for (const name of cases('migration')) {
      const document = join(VECTORS, 'migration', name, 'calls.json')
      if (!existsSync(document)) continue
      const made = readJson<unknown[]>(document).length
      ;(made === 0 ? empty : busy).push(name)
    }
    expect(empty.length, 'no migration vector pins an engine this library must not touch')
      .toBeGreaterThan(0)
    expect(busy.length, 'every migration vector expects zero calls, so the runner may not run')
      .toBeGreaterThan(0)
  })
})


describe('telemetry vectors', () => {
  for (const name of cases('telemetry')) {
    it(name, () => {
      const dir = join(VECTORS, 'telemetry', name)

      const boundaries = join(dir, 'buckets.json')
      if (existsSync(boundaries)) {
        const edges = readJson<{ edges_ms: number[] }>(join(dir, 'percentiles.json')).edges_ms
        expect(edges).toHaveLength(BUCKET_COUNT)
        for (const [nanoseconds, index] of readJson<[number, number][]>(boundaries)) {
          const histogram = new Histogram()
          histogram.record(nanoseconds)
          const landed = histogram.buckets
            .map((hits, at) => (hits > 0 ? at : -1))
            .filter((at) => at >= 0)
          expect(
            landed,
            `${nanoseconds} ns has to land in bucket ${index}. An implementation computing this ` +
              'with a logarithm agrees until a libm rounds the last bit differently, and then ' +
              'disagrees on exactly the boundary rows.',
          ).toEqual([index])
          expect(histogram.percentileMs(0.5)).toBe(edges[index])
        }
      }

      const operationsFile = join(dir, 'operations.json')
      if (!existsSync(operationsFile)) return

      const model = modelFromNeutral(readJson(join(dir, 'model.json')))
      const operations = readJson<Operation[]>(operationsFile)
      const window = recorded(model, operations, dir)

      const expectedError = join(dir, 'expected.json')
      if (existsSync(expectedError)) {
        const want = readJson<{ match: string }>(expectedError)
        const against = modelFromNeutral(readJson(join(dir, 'against.json')))
        // The class is deliberately not pinned - see the vector's own note. A caller handing this
        // the wrong model is a caller mistake rather than a document the library was given.
        expect(() => windowRecord(window, against)).toThrow(want.match)
        return
      }

      const documentFile = join(dir, 'window.json')
      if (existsSync(documentFile)) {
        expect(
          windowRecord(window, model),
          'the window document differs from the vector. Two libraries disagreeing here hand the ' +
            'planner different features for identical traffic, and nothing raises - the numbers ' +
            'are plausible either way.',
        ).toEqual(readJson(documentFile))
      }

      const explicitFile = join(dir, 'features_for.json')
      if (existsSync(explicitFile)) {
        const explicit = readJson<Record<string, unknown>>(explicitFile)
        for (const group of Object.keys(explicit).sort()) {
          const members = colocationGroups(model).find((g) => g.name === group)!
          const got = windowFeatures(window, group, {
            hasTimeDimension: hasTimeDimension(model, members),
          })
          expect(featuresRecord(got), `features for ${group}`).toEqual(explicit[group])
        }
      }
    })
  }

  it('exercises every shape kind', () => {
    // Totality over the operation kinds, and it is here because a mutation survived. Removing
    // `bulk_write` from the set of kinds that count as writes passed the whole shared suite: no
    // vector recorded a bulk write, so nothing measured the classification. Kinds are resolved
    // through each family's *model*, so a case cannot satisfy this by naming a kind in its own text.
    const seen = new Set<string>()
    for (const [family, field] of [
      ['telemetry', 'operations.json'],
      ['routing', 'cases.json'],
    ] as const) {
      for (const name of cases(family)) {
        const dir = join(VECTORS, family, name)
        if (!existsSync(join(dir, field))) continue
        const model = modelFromNeutral(readJson(join(dir, 'model.json')))
        const byId = new Map(enumerateShapes(model).map((shape) => [shapeId(shape), shape]))
        for (const entry of readJson<{ shape?: string }[]>(join(dir, field))) {
          const shape = entry.shape === undefined ? undefined : byId.get(entry.shape)
          if (shape !== undefined) seen.add(shape.kind)
        }
      }
    }
    expect(SHAPE_KINDS.filter((kind) => !seen.has(kind))).toEqual([])
  })

  it('found telemetry vectors at all', () => {
    // This library reaches Tier 1, so these do not get to be skipped either.
    expect(cases('telemetry').length).toBeGreaterThan(0)
  })

  it('reaches every measured field', () => {
    // Totality over the feature vector. A field this family never emits and never names as missing
    // is one where two libraries can disagree with nothing shared to notice.
    const seen = new Set<string>()
    for (const name of cases('telemetry')) {
      const dir = join(VECTORS, 'telemetry', name)
      const documentFile = join(dir, 'window.json')
      if (existsSync(documentFile)) {
        const document = readJson<{ groups: Record<string, Record<string, unknown>> }>(documentFile)
        for (const body of Object.values(document.groups)) {
          for (const key of Object.keys(body)) if (key !== 'copies') seen.add(key)
          for (const key of (body['missing'] as string[] | undefined) ?? []) seen.add(key)
        }
      }
      const explicitFile = join(dir, 'features_for.json')
      if (existsSync(explicitFile)) {
        for (const body of Object.values(readJson<Record<string, Record<string, unknown>>>(explicitFile))) {
          for (const key of Object.keys(body)) seen.add(key)
          for (const key of (body['missing'] as string[] | undefined) ?? []) seen.add(key)
        }
      }
    }
    const unreached = MEASURED_FIELDS.map(([key]) => key).filter((key) => !seen.has(key))
    expect(unreached).toEqual([])
  })
})

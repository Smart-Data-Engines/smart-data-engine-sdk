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

import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  canonicalBytes,
  CanonicalError,
  colocationGroups,
  CONTRACT,
  DeclarationError,
  enumerateShapes,
  loadMap,
  MapError,
  resolve,
  shapeId,
  shapeIr,
} from '../src/index.js'
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
    })
  }
})

const ERRORS: Record<string, new (...args: never[]) => Error> = {
  DeclarationError,
  MapError,
}

describe('error vectors', () => {
  for (const name of cases('errors')) {
    it(name, () => {
      const dir = join(VECTORS, 'errors', name)
      const expected = readJson<{ error: string; stage: string; match: string }>(
        join(dir, 'expected.json'),
      )
      const ctor = ERRORS[expected.error]
      expect(ctor, `unknown error class ${expected.error}`).toBeDefined()

      // The stage matters as much as the class. A library that raises the right error when a query
      // runs, rather than when the model is built, has a different bug that a type-only assertion
      // cannot see.
      expect(expected.stage).toBe('model')

      expect(() => modelFromNeutral(readJson(join(dir, 'model.json')))).toThrow(
        new RegExp(expected.match),
      )
    })
  }
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

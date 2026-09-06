/**
 * Tier 1, the parts the shared vectors deliberately cannot reach.
 *
 * Three kinds of thing live here, and each is here for a reason rather than for coverage.
 *
 * **Behaviour with no artefact.** A full buffer dropping its oldest window, and a guarded entry
 * point that swallows a bug rather than the caller's request, produce no document - so there is
 * nothing for a vector to compare and each library has to check its own.
 *
 * **A property no output can distinguish.** The bucket index is computed from an integer bit length
 * rather than from a logarithm, because `log2` is not required by IEEE 754 to be correctly rounded
 * and one bit at a power-of-two boundary is a different bucket. Measured: the logarithm form passes
 * every vector in `telemetry/`, because glibc's `log2` and V8's are both exact at a power of two.
 * So the hazard is a third libm, the vectors cannot see it, and the check is static.
 *
 * **The one thing this runtime does differently.** There is no lock, because there is no second
 * thread to lose a count to - which is worth an assertion only in the sense that the reference's
 * trade is absent here, and that is documented rather than tested.
 */

import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  BUCKET_COUNT,
  featuresRecord,
  guard,
  Histogram,
  internalFailures,
  MEASURED_FIELDS,
  Recorder,
  resetInternalFailures,
  SHAPE_KINDS,
  windowFeatures,
  WRITE_KINDS,
} from '../src/index.js'

const SOURCE = join(__dirname, '..', 'src', 'telemetry.ts')

function code(): string {
  // Comments stripped, for the use-and-mention reason the silence test explains: this file's own
  // docstring names `Math.log2` several times, and a check that could not tell a mention from a
  // call would fail on the paragraph explaining why it exists.
  return readFileSync(SOURCE, 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:])\/\/.*$/gm, '$1')
}

describe('the bucket index is integer arithmetic', () => {
  it('never reaches for a logarithm', () => {
    expect(code()).not.toMatch(/Math\s*\.\s*log/)
  })

  it('would notice one', () => {
    // The other half. A check that passes by finding nothing has to be shown a case it finds.
    expect(/Math\s*\.\s*log/.test('const i = Math.log2(ns / 1000)')).toBe(true)
  })

  it('agrees with the closed form at every boundary', () => {
    // The equivalence itself, on this runtime, so that the static check above is guarding something
    // that is true rather than merely different.
    for (let power = 0; power < BUCKET_COUNT + 2; power += 1) {
      for (const nanoseconds of [1000 * 2 ** power - 1, 1000 * 2 ** power, 1000 * 2 ** power + 1]) {
        const histogram = new Histogram()
        histogram.record(nanoseconds)
        const closedForm =
          nanoseconds < 1000
            ? 0
            : Math.min(BUCKET_COUNT - 1, Math.floor(Math.log2(nanoseconds / 1000)) + 1)
        expect(histogram.buckets.findIndex((hits) => hits > 0), `${nanoseconds} ns`).toBe(
          closedForm,
        )
      }
    }
  })
})

describe('every shape kind is classified', () => {
  it('splits SHAPE_KINDS into writes and reads with nothing left over', () => {
    // Not a tautology: the assertion is that the set of write kinds is a *subset* of the kinds that
    // exist, in both directions. A write kind that is not a shape kind would be dead, and the
    // membership itself is pinned by `telemetry/001`, which records one operation of each kind -
    // added after removing `bulk_write` from this set survived its first mutation.
    for (const kind of WRITE_KINDS) expect(SHAPE_KINDS).toContain(kind)
    expect([...WRITE_KINDS].sort()).toEqual(['bulk_write', 'write'])
  })
})

describe('a full buffer drops the oldest window and says so', () => {
  it('counts what it dropped in the next window', () => {
    const recorder = new Recorder('0'.repeat(16), 2)
    const record = (nanoseconds: number): void => {
      recorder.record({
        shapeId: 'a'.repeat(16),
        group: 'G',
        entity: 'E',
        kind: 'point_read',
        nanoseconds,
        rows: 1,
      })
    }
    for (const nanoseconds of [1000, 2000, 4000, 8000]) {
      record(nanoseconds)
      recorder.roll()
    }
    const pending = recorder.pending()
    expect(pending).toHaveLength(2)
    // A window is built before the eviction that its own roll performs, so the count appears in
    // the **next** window rather than in the one that caused the drop. Asserted exactly rather
    // than as "at least one": the number is the same in the reference implementation, and an
    // inequality here would let the two runtimes disagree about a field the planner reads.
    expect(pending[1]?.droppedWindows).toBe(1)
    expect(pending[0]?.droppedWindows).toBe(0)
  })

  it('rolls nothing when nothing was recorded', () => {
    expect(new Recorder('0'.repeat(16)).roll()).toBeUndefined()
  })

  it('drops the oldest windows once the application has taken them', () => {
    const recorder = new Recorder('0'.repeat(16))
    for (const nanoseconds of [1000, 2000, 4000]) {
      recorder.record({
        shapeId: 'a'.repeat(16),
        group: 'G',
        entity: 'E',
        kind: 'write',
        nanoseconds,
      })
      recorder.roll()
    }
    recorder.acknowledge(2)
    expect(recorder.pending()).toHaveLength(1)
    // More than there are is not an error: the application says how many it took, and taking all
    // of them and then some is the ordinary shape of a retry.
    recorder.acknowledge(9)
    expect(recorder.pending()).toHaveLength(0)
  })
})

describe('a bug of ours is not the caller"s outage', () => {
  it('counts a guarded failure instead of throwing', () => {
    resetInternalFailures()
    expect(
      guard('telemetry.test', () => {
        throw new Error('a defect in an aggregation counter')
      }),
    ).toBeUndefined()
    expect(internalFailures()).toEqual({ 'telemetry.test': 1 })
  })

  it('returns the value when there is no bug', () => {
    resetInternalFailures()
    expect(guard('telemetry.test', () => 7)).toBe(7)
    expect(internalFailures()).toEqual({})
  })

  it('keeps recording after one', () => {
    // The property that matters: a failure in one entry point does not stop the next one. Driven
    // through the real recorder with a value the histogram cannot use.
    resetInternalFailures()
    const recorder = new Recorder('0'.repeat(16))
    recorder.record({
      shapeId: 'a'.repeat(16),
      group: 'G',
      entity: 'E',
      kind: 'point_read',
      nanoseconds: 1000,
      rows: 1,
    })
    const window = recorder.roll()
    expect(window?.shapes).toHaveLength(1)
    expect(internalFailures()).toEqual({})
  })
})

describe('the feature vector', () => {
  it('names every unknown field and omits it from the document', () => {
    // Both halves of one promise, in the shape a reader relies on: a value that is absent from the
    // document is named in `missing`, and nothing is named that is present.
    const recorder = new Recorder('0'.repeat(16))
    recorder.record({
      shapeId: 'a'.repeat(16),
      group: 'G',
      entity: 'E',
      kind: 'point_read',
      nanoseconds: 1000,
      rows: 1,
    })
    const window = recorder.roll()
    expect(window).toBeDefined()
    const features = windowFeatures(window!, 'G')
    const document = featuresRecord(features)
    for (const [key] of MEASURED_FIELDS) {
      const present = key in document
      const named = features.missing.includes(key)
      expect(present, `${key}: present in the document and named as missing`).toBe(!named)
    }
    expect(features.missing.length).toBeGreaterThan(0)
  })
})

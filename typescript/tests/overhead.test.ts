/**
 * What routing costs per operation.
 *
 * There is no engine adapter here yet, so there is no round trip to compare against - this measures
 * throughput instead and asserts a floor that only a digest-free path can reach.
 *
 * The floor is deliberately loose. A machine-specific absolute number would be a test that fails on
 * whoever has the slowest laptop, and a test that fails for reasons unrelated to the code teaches
 * people to rerun the suite until it passes. What it does catch is the mistake this exists for: the
 * Python implementation computed a SHA-256 over a canonically encoded object on every route
 * resolution, which measured 41 microseconds median. At that cost, no machine reaches even a tenth of
 * the floor below.
 */

import { describe, expect, it } from 'vitest'

import {
  buildModel,
  colocationGroups,
  CONTRACT,
  entity,
  enumerateShapes,
  loadMap,
  resolve,
  shapeId,
  T,
} from '../src/index.js'
import type { PlacementMap } from '../src/index.js'

const FLOOR_PER_SECOND = 200_000
const ITERATIONS = 200_000

function fixture(): { map: PlacementMap; shapes: ReturnType<typeof enumerateShapes> } {
  const Reading = entity('Reading', {
    fields: { id: T.uuid, sensor: T.string, value: T.float64 },
  })
  const model = buildModel([Reading])
  const groups = colocationGroups(model)
  const map = loadMap(
    {
      contract: CONTRACT,
      model_version: model.version,
      map_version: 1,
      groups: Object.fromEntries(
        groups.map((g) => [
          g.name,
          { source: { id: `${g.name}@pg`, engine: 'pg', layout: { auto: true } } },
        ]),
      ),
    },
    { model },
  )
  return { map, shapes: enumerateShapes(model) }
}

describe('routing overhead', () => {
  it('resolves faster than a digest-per-call path could', () => {
    const { map, shapes } = fixture()
    const shape = shapes.find((s) => s.kind === 'point_read')!

    // Warm up, so the first call's memoisation and the JIT are not in the measurement.
    for (let i = 0; i < 1000; i += 1) resolve(map, shape)

    const started = process.hrtime.bigint()
    for (let i = 0; i < ITERATIONS; i += 1) resolve(map, shape)
    const elapsedNs = Number(process.hrtime.bigint() - started)

    const perSecond = (ITERATIONS / elapsedNs) * 1e9
    const perCallNs = elapsedNs / ITERATIONS

    console.log(
      `\n  ${perCallNs.toFixed(0)} ns per resolve, ${(perSecond / 1000).toFixed(0)}k/s ` +
        `(floor ${FLOOR_PER_SECOND / 1000}k/s)`,
    )

    expect(
      perSecond,
      `routing resolved ${(perSecond / 1000).toFixed(0)}k/s, under the floor. The usual cause is a ` +
        'shape identifier being recomputed per call - it is a SHA-256 over a canonically encoded ' +
        'object, and it belongs in a cache, not on the hot path.',
    ).toBeGreaterThan(FLOOR_PER_SECOND)
  })

  it('memoisation does not change the answer', () => {
    // The first version of this test compared an expression to itself and asserted nothing, which is
    // the exact failure this project keeps catching elsewhere. What matters about a cache here is not
    // that it is fast: a cache keyed by the wrong thing would route operations to the wrong
    // materialisation, which is far worse than being slow.
    const { shapes } = fixture()

    // Repeated calls agree with each other.
    for (const shape of shapes) {
      expect(shapeId(shape)).toBe(shapeId(shape))
    }

    // Distinct shapes get distinct identifiers - so the cache is not returning one entry for all.
    const ids = shapes.map(shapeId)
    expect(new Set(ids).size).toBe(shapes.length)

    // And a freshly built object with the same content gets the same identifier as the enumerated
    // one, which is what proves the cache is keyed by the object without the *value* depending on
    // object identity. Two libraries must agree on a shape's id; a memo that made the id depend on
    // which object you happened to hold would break that silently.
    const original = shapes.find((s) => s.kind === 'point_read')!
    const rebuilt = {
      group: original.group,
      kind: original.kind,
      entity: original.entity,
      fields: [...original.fields],
      target: original.target,
    }
    expect(shapeId(rebuilt)).toBe(shapeId(original))
  })
})

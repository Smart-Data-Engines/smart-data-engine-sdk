/**
 * What routing costs per operation, in this runtime.
 *
 * Task 19.2 asks for the overhead of requirement 3.5 to be measured **separately in each language**,
 * because a garbage collection pause in one runtime and a naive async wrapper in another are
 * different problems with the same threshold. So this is not a port of the Python measurement, and
 * the denominator is deliberately not the same one.
 *
 * **This library is Tier 0: it performs no operation at all.** It opens no socket, so there is no
 * round trip of its own for the added work to be one percent *of*. Borrowing the Python side's
 * PostgreSQL round trip would be a number from another runtime and another protocol presented as
 * this one's - the same mistake as putting our laptop's copy speed into a client's migration
 * estimate. What is available here, and is a stronger claim than a typical round trip, is the
 * **floor**: the cheapest round trip this runtime can make at all, measured against a loopback
 * socket the test starts itself. Anything a real engine does is slower, so one percent of the floor
 * implies one percent of every real operation.
 *
 * Measured on an i3-7100U, three runs: the loopback round trip's p50 was 114.7-118.1 µs and its p99
 * 257-397 µs, while a ClickHouse query over HTTP from the same process took 3.3-3.8 ms at p50. The
 * floor is therefore about thirty times stricter than the nearest real engine, which is the point of
 * using it.
 *
 * The floor is deliberately loose. A machine-specific absolute number would be a test that fails on
 * whoever has the slowest laptop, and a test that fails for reasons unrelated to the code teaches
 * people to rerun the suite until it passes. What it does catch is the mistake this exists for: the
 * Python implementation computed a SHA-256 over a canonically encoded object on every route
 * resolution, which measured 41 microseconds median. At that cost, no machine reaches even a tenth of
 * the floor below.
 */

import net from 'node:net'

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

// Requirement 3.5, the same number the Python side uses.
const BUDGET = 0.01
// How far past the budget the *tail* ratio may go before it means something rather than being a
// garbage collection pause. Same reasoning as the Python file, and this is the runtime 19.2 names
// the pause in: a batch that hits a collection carries it, which is what makes the tail visible at
// all at this scale.
const TAIL_ALLOWANCE = 5
const ROUND_TRIPS = 300
const BATCH = 100
const BATCHES = 2_000

const percentile = (xs: readonly number[], q: number): number => {
  const sorted = [...xs].sort((a, b) => a - b)
  const at = sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * q))]
  if (at === undefined) throw new Error('no samples')
  return at
}

/** The cheapest round trip this runtime can make: a loopback socket, echoing one byte. */
async function loopbackRoundTrips(count: number): Promise<number[]> {
  const server = net.createServer((connection) => {
    connection.on('data', (chunk) => connection.write(chunk))
  })
  await new Promise<void>((done) => server.listen(0, '127.0.0.1', () => done()))
  const address = server.address()
  if (address === null || typeof address === 'string') throw new Error('no port')

  const socket = net.connect(address.port, '127.0.0.1')
  await new Promise<void>((done) => socket.once('connect', () => done()))
  socket.setNoDelay(true)

  const trip = (): Promise<number> =>
    new Promise((resolveTrip) => {
      const started = process.hrtime.bigint()
      socket.once('data', () => resolveTrip(Number(process.hrtime.bigint() - started)))
      socket.write('x')
    })

  for (let i = 0; i < 50; i += 1) await trip()
  const measured: number[] = []
  for (let i = 0; i < count; i += 1) measured.push(await trip())

  socket.destroy()
  await new Promise<void>((done) => server.close(() => done()))
  return measured
}

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

  it('adds under one percent of the cheapest round trip this runtime can make', async () => {
    const { map, shapes } = fixture()
    const shape = shapes.find((s) => s.kind === 'point_read')!

    // What the library adds per operation: resolving a shape and finding the table name. The same
    // two steps the Python measurement times, so the numerators are comparable even though the
    // denominators cannot be.
    const work = (): string => {
      const materialization = resolve(map, shape)
      return materialization.layout.tables[shape.entity]!
    }
    for (let i = 0; i < 50_000; i += 1) work()

    // Timed in batches, and that is not a shortcut. A single call is a few hundred nanoseconds,
    // which is the same order as reading the clock twice, so per-call timing would mostly measure
    // the clock. A batch that hits a garbage collection carries it, so the tail is still visible -
    // which is what this runtime's half of 19.2 is about.
    const perCall: number[] = []
    for (let b = 0; b < BATCHES; b += 1) {
      const started = process.hrtime.bigint()
      for (let i = 0; i < BATCH; i += 1) work()
      perCall.push(Number(process.hrtime.bigint() - started) / BATCH)
    }

    const trips = await loopbackRoundTrips(ROUND_TRIPS)
    const addedP50 = percentile(perCall, 0.5)
    const addedP99 = percentile(perCall, 0.99)
    const tripP50 = percentile(trips, 0.5)
    const tripP99 = percentile(trips, 0.99)
    const ratio = addedP50 / tripP50
    const tailRatio = addedP99 / tripP99
    const breakEven = addedP50 / BUDGET

    console.log(
      `\n  loopback round trip p50  ${(tripP50 / 1000).toFixed(1)} µs  (n=${ROUND_TRIPS})` +
        `\n  loopback round trip p99  ${(tripP99 / 1000).toFixed(1)} µs` +
        `\n  library added p50        ${addedP50.toFixed(1)} ns  ` +
        `(n=${BATCHES * BATCH}, batched by ${BATCH})` +
        `\n  library added p99        ${addedP99.toFixed(1)} ns` +
        `\n  ratio p50/p50            ${(ratio * 100).toFixed(3)} %  ` +
        `budget ${BUDGET * 100} %` +
        `\n  ratio p99/p99            ${(tailRatio * 100).toFixed(3)} %  ` +
        `ceiling ${BUDGET * TAIL_ALLOWANCE * 100} %` +
        `\n  break-even operation     ${(breakEven / 1000).toFixed(1)} µs  ` +
        '(an operation faster than this would make the library cost 1%)',
    )

    expect(
      ratio,
      `routing adds ${(ratio * 100).toFixed(2)}% of the cheapest round trip this runtime can ` +
        'make, over the 1% budget in requirement 3.5. Everything a real engine does is slower ' +
        'than this floor, so a failure here is a failure everywhere.',
    ).toBeLessThan(BUDGET)

    expect(
      tailRatio,
      `the library's own p99 is ${(tailRatio * 100).toFixed(2)}% of the floor's p99, past the ` +
        'allowance. Far enough above a collection pause to be a code path.',
    ).toBeLessThan(BUDGET * TAIL_ALLOWANCE)

    // A floor on the denominator, so a broken socket cannot make the ratio look good by being slow,
    // and cannot make it look bad by not being a round trip at all.
    expect(tripP50).toBeGreaterThan(10_000)
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

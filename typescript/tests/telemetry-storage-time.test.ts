/**
 * The window features the control plane was built on and never received until now: `total_bytes`,
 * `index_to_table_ratio`, `daily_growth_bytes`, `time_filtered_share` and `write_burstiness`. The
 * mirror of the reference's `test_telemetry_storage_time.py`, case for case, so a derivation that
 * drifts in one language fails here as well as in the shared vectors.
 */

import { describe, expect, it } from 'vitest'

import {
  colocationGroups,
  DAY_NS,
  enumerateShapes,
  type LogicalModel,
  type OperationShape,
  Recorder,
  shapeId,
  timeFields,
  windowFeatures,
  windowRecord,
} from '../src/index.js'
import { modelFromNeutral } from '../src/testing/loader.js'

const HOUR_NS = 3_600_000_000_000
const SECOND_NS = 1_000_000_000

const MODEL = modelFromNeutral({
  entities: [
    {
      name: 'Reading',
      fields: [
        { name: 'at', type: 'timestamptz' },
        { name: 'station', type: 'string' },
        { name: 'temperature', type: 'int64' },
      ],
      key: ['station', 'at'],
    },
  ],
})
const TIMELESS = modelFromNeutral({
  entities: [
    {
      name: 'Station',
      fields: [
        { name: 'code', type: 'string' },
        { name: 'height', type: 'int64' },
      ],
      key: ['code'],
    },
  ],
})

class Clock {
  now = 0
  readonly read = (): number => this.now
}

function shape(model: LogicalModel, kind: string, fields: readonly string[] = []): OperationShape {
  const found = enumerateShapes(model).find(
    (s) => s.kind === kind && JSON.stringify(s.fields) === JSON.stringify(fields),
  )
  if (found === undefined) throw new Error(`no ${kind} shape over ${fields.join(',')}`)
  return found
}

function record(
  recorder: Recorder,
  found: OperationShape,
  options: { rows?: number; failed?: boolean; equal?: string[]; ranged?: string } = {},
): void {
  recorder.record({
    shapeId: shapeId(found),
    group: found.group,
    entity: found.entity,
    kind: found.kind,
    nanoseconds: 1_000,
    rows: options.rows ?? 1,
    failed: options.failed === true,
    ...(options.equal === undefined ? {} : { equal: options.equal, ranged: options.ranged ?? null }),
  })
}

function body(recorder: Recorder, model: LogicalModel = MODEL): Record<string, unknown> {
  const window = recorder.roll()
  expect(window).toBeDefined()
  const group = colocationGroups(model)[0]!.name
  const groups = windowRecord(window!, model)['groups'] as Record<string, Record<string, unknown>>
  return groups[group]!
}

function missing(features: Record<string, unknown>): string[] {
  return features['missing'] as string[]
}

describe('time_filtered_share', () => {
  it('counts a call that bounds or compares a time field', () => {
    const recorder = new Recorder(MODEL.version)
    const ranged = shape(MODEL, 'range_read', ['at'])
    const aggregate = shape(MODEL, 'aggregate')
    for (let i = 0; i < 3; i += 1) record(recorder, ranged, { equal: ['station'], ranged: 'at' })
    record(recorder, aggregate, { equal: ['at'] })
    record(recorder, aggregate, { equal: ['station'] })
    record(recorder, aggregate, { equal: [] })
    record(recorder, shape(MODEL, 'point_read', ['at', 'station']))
    record(recorder, shape(MODEL, 'write'))
    const features = body(recorder)
    expect(features['time_filtered_share']).toBe(4 / 8)
    expect(missing(features)).not.toContain('time_filtered_share')
  })

  it('does not count a range over a field that is not a time', () => {
    const recorder = new Recorder(MODEL.version)
    record(recorder, shape(MODEL, 'range_read', ['temperature']), { equal: [], ranged: 'temperature' })
    expect(body(recorder)['time_filtered_share']).toBe(0)
  })

  it('measures a group without a time field at zero, not unknown', () => {
    const recorder = new Recorder(TIMELESS.version)
    record(recorder, shape(TIMELESS, 'full_scan'), { equal: ['height'] })
    const features = body(recorder, TIMELESS)
    expect(features['time_filtered_share']).toBe(0)
    expect(features['has_time_dimension']).toBe(false)
  })

  it('stays unknown when the caller does not say which fields are times', () => {
    const recorder = new Recorder(MODEL.version)
    record(recorder, shape(MODEL, 'range_read', ['at']), { equal: [], ranged: 'at' })
    const window = recorder.roll()!
    const bare = windowFeatures(window, 'Reading', { hasTimeDimension: true })
    expect(bare.timeFilteredShare).toBeNull()
    expect(bare.missing).toContain('time_filtered_share')
    const given = windowFeatures(window, 'Reading', {
      hasTimeDimension: true,
      timeFields: new Map([['Reading', new Set(['at'])]]),
    })
    expect(given.timeFilteredShare).toBe(1)
  })

  it('finds time fields by type, never by name', () => {
    const named = modelFromNeutral({
      entities: [
        {
          name: 'Log',
          fields: [
            { name: 'created_at', type: 'string' },
            { name: 'seen', type: 'date' },
          ],
          key: ['created_at'],
        },
      ],
    })
    const found = timeFields(named, colocationGroups(named)[0]!)
    expect([...found.entries()].map(([k, v]) => [k, [...v]])).toEqual([['Log', ['seen']]])
  })
})

describe('storage: total_bytes and index_to_table_ratio', () => {
  it('takes the size and the index ratio from a sample in the window', () => {
    const clock = new Clock()
    const recorder = new Recorder(MODEL.version, 64, clock.read)
    record(recorder, shape(MODEL, 'write'))
    clock.now = 2 * SECOND_NS
    recorder.recordStorage({ group: 'Reading', totalBytes: 700_000, secondaryIndexBytes: 100_000 })
    const features = body(recorder)
    expect(features['total_bytes']).toBe(700_000)
    expect(features['index_to_table_ratio']).toBe(100_000 / 600_000)
  })

  it('takes the latest sample of the window', () => {
    const clock = new Clock()
    const recorder = new Recorder(MODEL.version, 64, clock.read)
    record(recorder, shape(MODEL, 'write'))
    recorder.recordStorage({ group: 'Reading', totalBytes: 500, secondaryIndexBytes: 0 })
    clock.now = SECOND_NS
    recorder.recordStorage({ group: 'Reading', totalBytes: 900, secondaryIndexBytes: 300 })
    const features = body(recorder)
    expect(features['total_bytes']).toBe(900)
    expect(features['index_to_table_ratio']).toBe(300 / 600)
  })

  it('leaves the size unknown without a sample in the window', () => {
    const clock = new Clock()
    const recorder = new Recorder(MODEL.version, 64, clock.read)
    record(recorder, shape(MODEL, 'write'))
    recorder.recordStorage({ group: 'Reading', totalBytes: 500, secondaryIndexBytes: 0 })
    body(recorder)
    clock.now = 10 * SECOND_NS
    record(recorder, shape(MODEL, 'write'))
    const features = body(recorder)
    expect(features).not.toHaveProperty('total_bytes')
    expect(missing(features)).toContain('total_bytes')
    expect(missing(features)).toContain('index_to_table_ratio')
  })

  it('reports an index ratio over nothing as unknown', () => {
    const recorder = new Recorder(MODEL.version, 64, new Clock().read)
    record(recorder, shape(MODEL, 'write'))
    recorder.recordStorage({ group: 'Reading', totalBytes: 0, secondaryIndexBytes: 0 })
    const features = body(recorder)
    expect(features['total_bytes']).toBe(0)
    expect(missing(features)).toContain('index_to_table_ratio')
  })

  it('never throws on a bad sample', () => {
    const recorder = new Recorder(MODEL.version, 64, new Clock().read)
    recorder.recordStorage({ group: 'Reading', totalBytes: 1.5, secondaryIndexBytes: 0 })
    recorder.recordStorage({ group: 'Reading', totalBytes: -1, secondaryIndexBytes: 0 })
    record(recorder, shape(MODEL, 'write'))
    expect(missing(body(recorder))).toContain('total_bytes')
  })

  it('attributes a sample taken at the instant of a roll to one window', () => {
    const recorder = new Recorder(MODEL.version, 64, new Clock().read)
    record(recorder, shape(MODEL, 'write'))
    recorder.recordStorage({ group: 'Reading', totalBytes: 123, secondaryIndexBytes: 0 })
    expect(body(recorder)['total_bytes']).toBe(123)
    record(recorder, shape(MODEL, 'write'))
    expect(missing(body(recorder))).toContain('total_bytes')
  })
})

describe('daily_growth_bytes', () => {
  it('projects to a day from samples an hour or more apart', () => {
    const clock = new Clock()
    const recorder = new Recorder(MODEL.version, 64, clock.read)
    record(recorder, shape(MODEL, 'write'))
    recorder.recordStorage({ group: 'Reading', totalBytes: 1_000_000, secondaryIndexBytes: 0 })
    clock.now = 2 * HOUR_NS
    recorder.recordStorage({ group: 'Reading', totalBytes: 1_200_000, secondaryIndexBytes: 0 })
    expect(body(recorder)['daily_growth_bytes']).toBe(200_000 * 12)
  })

  it('leaves growth from less than an hour unknown', () => {
    const clock = new Clock()
    const recorder = new Recorder(MODEL.version, 64, clock.read)
    record(recorder, shape(MODEL, 'write'))
    recorder.recordStorage({ group: 'Reading', totalBytes: 1_000, secondaryIndexBytes: 0 })
    clock.now = HOUR_NS - 1
    recorder.recordStorage({ group: 'Reading', totalBytes: 9_000, secondaryIndexBytes: 0 })
    const features = body(recorder)
    expect(missing(features)).toContain('daily_growth_bytes')
    expect(features['total_bytes']).toBe(9_000)
  })

  it('truncates toward zero in both directions', () => {
    for (const [delta, expected] of [
      [1, 3],
      [-1, -3],
    ] as const) {
      const clock = new Clock()
      const recorder = new Recorder(MODEL.version, 64, clock.read)
      record(recorder, shape(MODEL, 'write'))
      recorder.recordStorage({ group: 'Reading', totalBytes: 100, secondaryIndexBytes: 0 })
      clock.now = 7 * HOUR_NS
      recorder.recordStorage({ group: 'Reading', totalBytes: 100 + delta, secondaryIndexBytes: 0 })
      expect(body(recorder)['daily_growth_bytes']).toBe(expected)
    }
  })

  it('reaches back across windows but not past a day', () => {
    const clock = new Clock()
    const recorder = new Recorder(MODEL.version, 64, clock.read)
    record(recorder, shape(MODEL, 'write'))
    recorder.recordStorage({ group: 'Reading', totalBytes: 1_000, secondaryIndexBytes: 0 })
    body(recorder)
    clock.now = 3 * HOUR_NS
    record(recorder, shape(MODEL, 'write'))
    recorder.recordStorage({ group: 'Reading', totalBytes: 4_000, secondaryIndexBytes: 0 })
    expect(body(recorder)['daily_growth_bytes']).toBe(3_000 * 8)

    clock.now = 3 * HOUR_NS + DAY_NS - 1
    record(recorder, shape(MODEL, 'write'))
    recorder.recordStorage({ group: 'Reading', totalBytes: 4_400, secondaryIndexBytes: 0 })
    expect(body(recorder)['daily_growth_bytes']).toBe(400)

    clock.now = 3 * HOUR_NS + 2 * DAY_NS
    record(recorder, shape(MODEL, 'write'))
    recorder.recordStorage({ group: 'Reading', totalBytes: 9_999, secondaryIndexBytes: 0 })
    const features = body(recorder)
    expect(missing(features)).toContain('daily_growth_bytes')
    expect(features['total_bytes']).toBe(9_999)
  })
})

describe('write_burstiness', () => {
  it('is one for even writes', () => {
    const clock = new Clock()
    const recorder = new Recorder(MODEL.version, 64, clock.read)
    const write = shape(MODEL, 'write')
    for (let second = 0; second < 4; second += 1) {
      clock.now = second * SECOND_NS
      record(recorder, write, { rows: 10 })
    }
    clock.now = 4 * SECOND_NS
    expect(body(recorder)['write_burstiness']).toBe(1)
  })

  it('is how many times the busiest second exceeds the mean', () => {
    const clock = new Clock()
    const recorder = new Recorder(MODEL.version, 64, clock.read)
    const bulk = shape(MODEL, 'bulk_write')
    record(recorder, bulk, { rows: 90 })
    clock.now = 3 * SECOND_NS + 1
    record(recorder, bulk, { rows: 10 })
    clock.now = 10 * SECOND_NS
    expect(body(recorder)['write_burstiness']).toBe((90 * 10) / 100)
  })

  it('counts no rows for a failed write', () => {
    const clock = new Clock()
    const recorder = new Recorder(MODEL.version, 64, clock.read)
    const write = shape(MODEL, 'write')
    record(recorder, write, { rows: 5 })
    clock.now = SECOND_NS
    record(recorder, write, { rows: 500, failed: true })
    clock.now = 2 * SECOND_NS
    expect(body(recorder)['write_burstiness']).toBe((5 * 2) / 5)
  })

  it('is unknown with no rows written', () => {
    const recorder = new Recorder(MODEL.version, 64, new Clock().read)
    record(recorder, shape(MODEL, 'range_read', ['at']), { equal: [], ranged: 'at' })
    expect(missing(body(recorder))).toContain('write_burstiness')
  })

  it('counts a write past the measured end in its own second', () => {
    const clock = new Clock()
    const recorder = new Recorder(MODEL.version, 64, clock.read)
    clock.now = 5 * SECOND_NS
    record(recorder, shape(MODEL, 'write'), { rows: 6 })
    clock.now = 2 * SECOND_NS
    expect(body(recorder)['write_burstiness']).toBe((6 * 6) / 6)
  })
})

describe('the five features', () => {
  for (const model of [MODEL, TIMELESS]) {
    it(`are no longer missing when measured (${colocationGroups(model)[0]!.name})`, () => {
      const clock = new Clock()
      const recorder = new Recorder(model.version, 64, clock.read)
      const group = colocationGroups(model)[0]!.name
      record(recorder, shape(model, 'write'), { rows: 3 })
      recorder.recordStorage({ group, totalBytes: 10_000, secondaryIndexBytes: 1_000 })
      clock.now = 2 * HOUR_NS
      recorder.recordStorage({ group, totalBytes: 12_000, secondaryIndexBytes: 1_000 })
      const features = body(recorder, model)
      for (const name of [
        'total_bytes',
        'index_to_table_ratio',
        'daily_growth_bytes',
        'time_filtered_share',
        'write_burstiness',
      ]) {
        expect(features, name).toHaveProperty(name)
        expect(missing(features), name).not.toContain(name)
      }
    })
  }
})

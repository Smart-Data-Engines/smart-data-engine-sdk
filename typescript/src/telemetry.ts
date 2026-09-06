/**
 * Measuring what the application actually does, without ever seeing what it does it to.
 *
 * This is the input a placement decision is made from, so its shape matters more than its
 * precision. Three constraints shaped everything here.
 *
 * **It carries no values.** A record is keyed by an operation shape, which is assembled from the
 * structure of a call and never sees its arguments. There is no code path by which a customer's row
 * reaches a telemetry record, which is why this file can be read by a client and believed.
 *
 * **It cannot cost anything.** Routing already has a one percent budget for the whole library and
 * recording happens on the same path. So: no string formatting per record, no stack walking, and a
 * histogram rather than a list of samples. There is no lock, and that is not a shortcut - this
 * runtime has one thread per record, so the trade the reference implementation makes (a lock taken
 * only when a shape is first seen) has nothing to buy here.
 *
 * **It cannot fail the caller.** Every entry point goes through `guard`. A bug in an aggregation
 * counter must not take down somebody's request - and the failure is counted, so it is not
 * invisible either.
 *
 * The histogram deserves a word, because it is the one deliberate loss of precision. Latency lands
 * in exponential buckets, so a percentile read out of it is approximate - within one bucket width,
 * which is a factor of two at the extremes. That is ample for the decision it feeds: a planner
 * cares whether a group's reads are microseconds or milliseconds, not whether p99 is 412 or 431
 * microseconds.
 */

import { compareCodePoints } from './canonical.js'
import type { Group } from './groups.js'
import { colocationGroups } from './groups.js'
import { guard } from './internal.js'
import type { LogicalModel } from './model.js'
import type { ShapeKind } from './shapes.js'
import { WRITE_KINDS } from './shapes.js'

/** 1 microsecond to about 17 s, doubling. Enough to tell a cache hit from a full scan. */
export const BUCKET_COUNT = 25
export const BUCKET_BASE_NS = 1000

/** Exponential-bucket histogram. Fixed memory, O(1) record, approximate percentiles. */
export class Histogram {
  readonly buckets: number[] = new Array<number>(BUCKET_COUNT).fill(0)
  count = 0
  total = 0

  /**
   * Put one duration in its bucket. **Integer arithmetic only, and that is the point.**
   *
   * The obvious form is `Math.floor(Math.log2(ns / BUCKET_BASE_NS)) + 1`, which is the same
   * function and the wrong way to compute it in a library that has to agree with another
   * implementation. `log2` is not required by IEEE 754 to be correctly rounded, so two runtimes may
   * differ in the last bit - and one bit at a power-of-two boundary is a different bucket, which is
   * a different p99 for identical traffic, in a number a placement decision is made from.
   *
   * The bit length of the integer quotient is exact everywhere. `Math.clz32` is defined on the
   * 32-bit value, and the quotient is below 2^24 in every case that is not clamped, so the clamp is
   * checked first rather than relying on the coercion.
   *
   * **The vectors cannot see the difference, and that is worth saying rather than implying.**
   * Measured: the logarithm form passes every vector in `telemetry/`, because glibc's `log2` and
   * V8's are both exact at a power of two - the two runtimes we have agree, and the hazard is a
   * *third* libm that does not. A property no output can distinguish on the machines available is
   * not one a vector can hold, so it is held statically: `telemetry.test.ts` refuses a logarithm in
   * this file.
   */
  record(nanoseconds: number): void {
    this.count += 1
    this.total += nanoseconds
    if (nanoseconds < BUCKET_BASE_NS) {
      this.buckets[0] = (this.buckets[0] ?? 0) + 1
      return
    }
    const scaled = Math.floor(nanoseconds / BUCKET_BASE_NS)
    const bits = scaled >= 2 ** (BUCKET_COUNT - 1) ? BUCKET_COUNT : 32 - Math.clz32(scaled)
    const index = Math.min(BUCKET_COUNT - 1, bits)
    // `?? 0` rather than `+= 1`: `noUncheckedIndexedAccess` types an indexed read as possibly
    // undefined, and the array is allocated full - so this is the compiler's price for a setting
    // that has caught real bugs elsewhere in this library, paid on a hot path, deliberately.
    this.buckets[index] = (this.buckets[index] ?? 0) + 1
  }

  /**
   * Approximate percentile in milliseconds, or null if nothing was recorded.
   *
   * Returns the *upper* edge of the bucket the percentile falls in. Rounding up rather than
   * interpolating is deliberate: a placement decision made on an optimistic latency figure is the
   * wrong kind of wrong.
   */
  percentileMs(fraction: number): number | null {
    if (this.count === 0) return null
    const target = fraction * this.count
    let seen = 0
    for (let index = 0; index < BUCKET_COUNT; index += 1) {
      seen += this.buckets[index] ?? 0
      if (seen >= target) return (BUCKET_BASE_NS * 2 ** index) / 1_000_000
    }
    return null
  }

  merge(other: Histogram): void {
    for (let index = 0; index < BUCKET_COUNT; index += 1) {
      this.buckets[index] = (this.buckets[index] ?? 0) + (other.buckets[index] ?? 0)
    }
    this.count += other.count
    this.total += other.total
  }
}

/** What was observed for one operation shape. No values, by construction. */
export class ShapeStats {
  calls = 0
  rows = 0
  errors = 0
  readonly latency = new Histogram()

  constructor(
    readonly shapeId: string,
    readonly group: string,
    readonly entity: string,
    readonly kind: ShapeKind,
  ) {}

  record(nanoseconds: number, rows: number, failed: boolean): void {
    this.calls += 1
    this.rows += rows
    if (failed) this.errors += 1
    this.latency.record(nanoseconds)
  }
}

/**
 * What was observed writing one row to one derived copy. No values, by construction.
 *
 * Deliberately **not** a `ShapeStats`, and that is the load-bearing decision. A fan-out is not an
 * operation the application asked for - it is the library keeping a copy current - so recording it
 * as a shape would add a write to the very counters a placement is scored on: `read_write_ratio`
 * would move because a copy exists, and a group with one copy would look twice as write-heavy as
 * the same group without one. It is also not part of `GroupFeatures`: a copy's freshness does not
 * score a placement, it reports the health of one already made.
 */
export class FanOutStats {
  writes = 0
  /** Rows that did not reach the copy. Absence, not lateness - see `CopyFreshness`. */
  failures = 0
  readonly latency = new Histogram()

  constructor(
    readonly group: string,
    readonly materialization: string,
  ) {}

  record(nanoseconds: number, failed: boolean): void {
    this.writes += 1
    if (failed) this.failures += 1
    // Recorded either way. A failed fan-out took time too, and dropping it would make the measured
    // window look better precisely when the copy is in trouble.
    this.latency.record(nanoseconds)
  }
}

/**
 * How far behind one derived copy is, measured.
 *
 * A derived copy in this library is maintained by the fan-out in a write, in the client's own
 * process, straight after the source. There is no asynchronous replication anywhere, so there is no
 * queue to fall behind in. Two things can therefore be true of a copy, and only two: it is **late**
 * by at most the duration of that one write, or the write **failed** and the row is absent rather
 * than late. Both are reported, because a copy missing a thousand rows can have an excellent p99
 * and a client told only the percentile reads "0.9 ms behind" off a copy that is missing yesterday.
 */
export interface CopyFreshness {
  readonly group: string
  readonly materialization: string
  readonly writes: number
  readonly failures: number
  readonly lagP50Ms: number | null
  readonly lagP99Ms: number | null
  /** Whether every write reached the copy in this window. */
  readonly complete: boolean
}

export function copyFreshnessRecord(copy: CopyFreshness): Record<string, unknown> {
  return {
    group: copy.group,
    materialization: copy.materialization,
    writes: copy.writes,
    failures: copy.failures,
    lag_p50_ms: copy.lagP50Ms,
    lag_p99_ms: copy.lagP99Ms,
    complete: copy.complete,
  }
}

/**
 * The contract between telemetry and the planner.
 *
 * `null` means *unknown*, which is not zero and is treated differently by the planner. Anything
 * unknown also appears in `missing`, so a reader never has to infer absence from a null.
 */
export interface GroupFeatures {
  /**
   * How many operations were observed. A count, never a value.
   *
   * Present because a planner comparing two groups has to know which one carries the traffic:
   * without it, an idle group and the group serving every request are equally important. It is also
   * evidence about the features themselves - a read/write ratio derived from twelve calls is
   * arithmetic, not a measurement.
   */
  readonly calls: number
  readonly readWriteRatio: number | null
  readonly shapeMix: Readonly<Record<string, number>>
  readonly latencyP50Ms: number | null
  readonly latencyP99Ms: number | null
  readonly resultCardinalityP50: number | null
  readonly resultCardinalityP99: number | null
  readonly totalBytes: number | null
  readonly dailyGrowthBytes: number | null
  readonly indexToTableRatio: number | null
  readonly pkAccessShare: number | null
  readonly hasTimeDimension: boolean
  readonly timeFilteredShare: number | null
  readonly distinctShapes: number
  readonly writeBurstiness: number | null
  readonly errorShare: number | null
  readonly missing: readonly string[]
  readonly complete: boolean
}

/**
 * The document key for every measured field, and the property that carries it.
 *
 * Two spellings because two things are being named: the **document** is the format, shared with
 * every other implementation and therefore `snake_case`; the **property** is this language's, and a
 * library that reads like transliterated Python is a worse library in TypeScript and no better an
 * SDE one.
 *
 * The reference implementation derives its equivalent from the dataclass at runtime, which is not
 * available here - types are erased before the code runs. So the list is the source and the type is
 * checked against it, which is the same guarantee in the other direction and is enforced by the
 * compiler rather than by a test: see `FIELD_LIST_IS_TOTAL`.
 */
export const MEASURED_FIELDS = [
  ['calls', 'calls'],
  ['read_write_ratio', 'readWriteRatio'],
  ['shape_mix', 'shapeMix'],
  ['latency_p50_ms', 'latencyP50Ms'],
  ['latency_p99_ms', 'latencyP99Ms'],
  ['result_cardinality_p50', 'resultCardinalityP50'],
  ['result_cardinality_p99', 'resultCardinalityP99'],
  ['total_bytes', 'totalBytes'],
  ['daily_growth_bytes', 'dailyGrowthBytes'],
  ['index_to_table_ratio', 'indexToTableRatio'],
  ['pk_access_share', 'pkAccessShare'],
  ['has_time_dimension', 'hasTimeDimension'],
  ['time_filtered_share', 'timeFilteredShare'],
  ['distinct_shapes', 'distinctShapes'],
  ['write_burstiness', 'writeBurstiness'],
  ['error_share', 'errorShare'],
] as const

type MeasuredProperty = (typeof MEASURED_FIELDS)[number][1]
type Bookkeeping = 'missing' | 'complete'

/**
 * A compile-time ratchet in both directions.
 *
 * Add a field to `GroupFeatures` and forget the list, and the second element stops being
 * assignable; put a name in the list that is not a field, and the first does. `tsc` fails either
 * way, which is where this belongs: the control plane's reader had a hand-written field list
 * against a dataclass where every field has a default, so the next field added to the library would
 * have been recorded as *measured* rather than missing. That defect had to be found by mutation.
 * This one cannot exist.
 */
export const FIELD_LIST_IS_TOTAL: [
  MeasuredProperty extends keyof GroupFeatures ? true : never,
  Exclude<keyof GroupFeatures, Bookkeeping> extends MeasuredProperty ? true : never,
] = [true, true]

const EMPTY_FEATURES: GroupFeatures = {
  calls: 0,
  readWriteRatio: null,
  shapeMix: {},
  latencyP50Ms: null,
  latencyP99Ms: null,
  resultCardinalityP50: null,
  resultCardinalityP99: null,
  totalBytes: null,
  dailyGrowthBytes: null,
  indexToTableRatio: null,
  pkAccessShare: null,
  hasTimeDimension: false,
  timeFilteredShare: null,
  distinctShapes: 0,
  writeBurstiness: null,
  errorShare: null,
  missing: [],
  complete: true,
}

/**
 * The same features with `missing` **derived** from which values came out unknown.
 *
 * Derived rather than listed alongside them, so the two cannot disagree - and in the reference they
 * did: `time_filtered_share` is a field neither library measures and it was left null and absent
 * from this set, which breaks the one promise the set makes.
 *
 * What is unknown and why, because a derivation hides the reasons. Engine-side sizes need a
 * catalogue read, which is an adapter capability rather than a measurement. Growth and burstiness
 * need two samples over time and a window is one. `time_filtered_share` would need the *arguments*
 * of a call, and this library records shapes and never values.
 *
 * `also` carries reasons that are not field names - `no_traffic` is the only one - because a reader
 * needs the difference between "this group was idle" and "this field is not measurable".
 */
function withMissing(measured: GroupFeatures, also: readonly string[] = []): GroupFeatures {
  const unknown = MEASURED_FIELDS.filter(([, property]) => measured[property] === null).map(
    ([key]) => key,
  )
  return { ...measured, missing: [...also, ...unknown].sort(compareCodePoints) }
}

/**
 * The feature vector as the document that crosses the boundary to the control plane.
 *
 * **A field with no value is omitted, and `missing` is what says so.** Emitting a null would work
 * too - the reader treats absent and null alike - but omitting is the honest spelling of "not
 * measured", and it keeps this function from having an opinion about what a null means.
 */
export function featuresRecord(features: GroupFeatures): Record<string, unknown> {
  const body: Record<string, unknown> = {}
  for (const [key, property] of MEASURED_FIELDS) {
    const value = features[property]
    if (value === null) continue
    body[key] = value
  }
  body['missing'] = [...features.missing]
  body['complete'] = features.complete
  return body
}

/**
 * One aggregation period, ready to send.
 *
 * `complete` is false when the application could not collect part of the period, or when the buffer
 * dropped windows. A window that is not complete is still sent - the planner needs to know traffic
 * existed - but it may not be used to justify a migration.
 */
export interface Window {
  readonly modelVersion: string
  readonly startedNs: number
  readonly endedNs: number
  readonly shapes: readonly ShapeStats[]
  readonly complete: boolean
  readonly droppedWindows: number
  /**
   * What the fan-out to each derived copy did. Empty when the group has no copy, which is the
   * ordinary case - a copy exists during a migration and while a derived materialisation is in the
   * map, not otherwise.
   */
  readonly fanned: readonly FanOutStats[]
}

/**
 * How far behind each of this group's derived copies ran, sorted by materialisation.
 *
 * Read out of the histogram rather than stored, like every other percentile here. A stored
 * percentile is a second copy of a fact that changes when the samples do.
 */
export function windowCopies(window: Window, group: string): CopyFreshness[] {
  return window.fanned
    .filter((stats) => stats.group === group)
    .sort((a, b) => compareCodePoints(a.materialization, b.materialization))
    .map((stats) => ({
      group: stats.group,
      materialization: stats.materialization,
      writes: stats.writes,
      failures: stats.failures,
      lagP50Ms: stats.latency.percentileMs(0.5),
      lagP99Ms: stats.latency.percentileMs(0.99),
      complete: stats.failures === 0,
    }))
}

/**
 * Nearest-rank: the smallest sample at least `fraction` of the data is not above.
 *
 * **The same rank rule as `Histogram.percentileMs`, and it did not used to be.** This took the
 * floor while the histogram takes the first bucket whose cumulative count reaches
 * `fraction * count`, which is the ceiling. The two agree on every sample set with an odd count,
 * and every case in `telemetry/` had one, so one window document carried two percentile
 * conventions and nothing could see it. They differ exactly when `fraction * length` is an
 * integer, which is what two samples at p50 is: `[3, 300]` reported 300 here and the *lower*
 * bucket in the histogram. `telemetry/007` pins it.
 */
function at(ordered: readonly number[], fraction: number): number | null {
  if (ordered.length === 0) return null
  const rank = Math.max(1, Math.ceil(fraction * ordered.length)) - 1
  return ordered[Math.min(ordered.length - 1, rank)] ?? null
}

export interface FeatureOptions {
  readonly hasTimeDimension?: boolean
}

/** Fold this window's records for one group into the planner's feature vector. */
export function windowFeatures(
  window: Window,
  group: string,
  options: FeatureOptions = {},
): GroupFeatures {
  const timeDimension = options.hasTimeDimension === true
  const records = window.shapes.filter((stats) => stats.group === group)
  if (records.length === 0) {
    // `no_traffic` is the *reason*, and the unknown fields are named too, by the same derivation as
    // below - two branches of one function computing `missing` by different rules is the defect
    // this set exists to prevent.
    return withMissing({ ...EMPTY_FEATURES, complete: window.complete }, ['no_traffic'])
  }

  let writes = 0
  let reads = 0
  for (const stats of records) {
    if (WRITE_KINDS.has(stats.kind)) writes += stats.calls
    else reads += stats.calls
  }
  const calls = writes + reads

  const latency = new Histogram()
  for (const stats of records) latency.merge(stats.latency)

  const counts = new Map<string, number>()
  for (const stats of records) counts.set(stats.kind, (counts.get(stats.kind) ?? 0) + stats.calls)
  const shapeMix: Record<string, number> = {}
  if (calls > 0) {
    for (const kind of [...counts.keys()].sort(compareCodePoints)) {
      shapeMix[kind] = (counts.get(kind) ?? 0) / calls
    }
  }

  // A failed call counts in the latency histogram and **not** here, and the two answers have
  // different reasons rather than one convention. A failure took time, so dropping it from the
  // histogram would flatter a window precisely when the engine is in trouble. It returned no rows
  // because it failed rather than because the data is sparse, so averaging that zero in
  // understates how many rows a read of this shape returns - and `errorShare` already carries the
  // failure rate, so smearing it into a second feature is one fact in two places. A shape whose
  // every call failed contributes nothing rather than a zero. `telemetry/008`.
  const readRecords = records.filter(
    (stats) => !WRITE_KINDS.has(stats.kind) && stats.calls > stats.errors,
  )
  // A numeric comparator, because the default one sorts lexicographically and would put 10 before
  // 2. `telemetry/003` has cardinalities that expose it.
  const cardinalities = readRecords
    .map((stats) => stats.rows / (stats.calls - stats.errors))
    .sort((a, b) => a - b)

  const pkCalls = records
    .filter((stats) => stats.kind === 'point_read')
    .reduce((sum, stats) => sum + stats.calls, 0)
  const errors = records.reduce((sum, stats) => sum + stats.errors, 0)

  return withMissing({
    ...EMPTY_FEATURES,
    calls,
    readWriteRatio: writes > 0 ? reads / writes : null,
    shapeMix,
    latencyP50Ms: latency.percentileMs(0.5),
    latencyP99Ms: latency.percentileMs(0.99),
    resultCardinalityP50: at(cardinalities, 0.5),
    resultCardinalityP99: at(cardinalities, 0.99),
    pkAccessShare: calls > 0 ? pkCalls / calls : null,
    hasTimeDimension: timeDimension,
    distinctShapes: records.length,
    errorShare: calls > 0 ? errors / calls : null,
    complete: window.complete,
  })
}

/**
 * This window as the document the control plane reads. Numbers, never rows.
 *
 * The model is required and it is checked. Only one fact is read from it - whether a group carries a
 * time dimension, which is decided by declared *type* and never by a field's name - but a window
 * serialised against the wrong model would attach that fact to the wrong groups and claim
 * `has_time_dimension: false` for a group that has one. False is a claim; a measurement this
 * library cannot make has to be absent.
 *
 * **This document is deliberately not canonical, and that needs saying because section 1 of the
 * format contract rejects floating point outright.** Its reason is that a float's textual form
 * differs between languages, and almost every number here is a float. This document is not signed,
 * not hashed and never compared for equality, so the rule it breaks does not apply - but the thing
 * that makes the `telemetry/` family checkable is narrower and worth stating: every number here is
 * either **a ratio of two integers** or **a bucket edge divided by a million**, and IEEE 754
 * requires division to be correctly rounded. So two languages compute the same double from the same
 * traffic even where they would print it differently, which is why those vectors compare numbers
 * rather than bytes.
 */
export function windowRecord(window: Window, model: LogicalModel): Record<string, unknown> {
  if (model.version !== window.modelVersion) {
    throw new Error(
      `this window measured model version ${window.modelVersion} and it is being serialised ` +
        `against ${model.version}. Only one fact is read from the model here - whether a group ` +
        `has a time dimension - and reading it from another model would attach it to the wrong ` +
        `groups. Keep the model the recorder was created for, or drop the window: a window ` +
        `measured against a model that no longer exists cannot be scored against the one that ` +
        `does.`,
    )
  }
  const byName = new Map(colocationGroups(model).map((group) => [group.name, group]))
  const named = [
    ...new Set([
      ...window.shapes.map((stats) => stats.group),
      ...window.fanned.map((stats) => stats.group),
    ]),
  ].sort(compareCodePoints)
  const unknown = named.filter((name) => !byName.has(name))
  if (unknown.length > 0) {
    throw new Error(
      `this window has records for groups this model does not have: [${unknown.join(', ')}]. The ` +
        `recorder is given a model version and the groups come from the operations it observed, ` +
        `so this is a session routing a model other than the one measured.`,
    )
  }

  const groups: Record<string, unknown> = {}
  for (const name of named) {
    const group = byName.get(name)
    if (group === undefined) continue
    const record = featuresRecord(
      windowFeatures(window, name, { hasTimeDimension: hasTimeDimension(model, group) }),
    )
    const copies = windowCopies(window, name).map(copyFreshnessRecord)
    if (copies.length > 0) {
      // Absent rather than empty when the group has no derived copy, for the reason `also_write` is
      // absent rather than empty in a map: absent says "not doing this" and an empty list says
      // "considered and found none", which is a stronger claim.
      record['copies'] = copies
    }
    groups[name] = record
  }

  return {
    model_version: window.modelVersion,
    complete: window.complete,
    dropped_windows: window.droppedWindows,
    groups,
  }
}

/** Types that make a field a time dimension. Recognised by **type**, never by name. */
const TIME_TYPES: ReadonlySet<string> = new Set(['date', 'timestamp', 'timestamptz'])

/**
 * Does any entity in this group carry a time dimension?
 *
 * Decided by the declared type and never by the field's name. A name is not evidence: `created_at`
 * typed as a string is a string, and treating it as a timestamp would have the planner recommend
 * time partitioning on a column no engine can range-scan usefully. And a client may hash identifier
 * names so that we never see them - a derivation that read names would silently produce different
 * answers with hashing on and off, which is the property the hashed-model vectors forbid.
 */
export function hasTimeDimension(model: LogicalModel, group: Group): boolean {
  for (const member of group.members) {
    const spec = model.entities.find((entity) => entity.name === member)
    if (spec === undefined) continue
    if (spec.fields.some((field) => TIME_TYPES.has(field.type))) return true
  }
  return false
}

export interface RecordOptions {
  readonly shapeId: string
  readonly group: string
  readonly entity: string
  readonly kind: ShapeKind
  readonly nanoseconds: number
  readonly rows?: number
  readonly failed?: boolean
}

export interface FanOutOptions {
  readonly group: string
  readonly materialization: string
  readonly nanoseconds: number
  readonly failed?: boolean
}

/**
 * Accumulates records, rolls windows, and drops telemetry rather than anything else.
 *
 * No lock, because this runtime does not need one - and that is worth stating rather than leaving
 * as an absence. The reference implementation takes one only when a shape is first seen or a window
 * rolls, and accepts that two threads racing on the same shape can lose a call from a count. Here
 * there is nothing to race: the cost this class is allowed is zero either way.
 */
export class Recorder {
  private current = new Map<string, ShapeStats>()
  private fanned = new Map<string, FanOutStats>()
  private startedNs = now()
  private windows: Window[] = []
  private dropped = 0
  private incomplete = false

  constructor(
    private readonly modelVersion: string,
    private readonly maxWindows = 64,
  ) {}

  /** Record one operation. Never throws. */
  record(options: RecordOptions): void {
    guard('telemetry.record', () => {
      let stats = this.current.get(options.shapeId)
      if (stats === undefined) {
        stats = new ShapeStats(options.shapeId, options.group, options.entity, options.kind)
        this.current.set(options.shapeId, stats)
      }
      stats.record(options.nanoseconds, options.rows ?? 0, options.failed === true)
    })
  }

  /**
   * Record one write to one derived copy. Never throws.
   *
   * A separate entry point from `record` rather than a shape kind, because a fan-out is not an
   * operation the application asked for - see `FanOutStats`.
   */
  recordFanOut(options: FanOutOptions): void {
    guard('telemetry.record_fan_out', () => {
      const key = `${options.group} ${options.materialization}`
      let stats = this.fanned.get(key)
      if (stats === undefined) {
        stats = new FanOutStats(options.group, options.materialization)
        this.fanned.set(key, stats)
      }
      stats.record(options.nanoseconds, options.failed === true)
    })
  }

  /**
   * Close the current period and queue it.
   *
   * Returns the window, or undefined if nothing was recorded in it.
   */
  roll(): Window | undefined {
    return guard('telemetry.roll', () => {
      if (this.current.size === 0 && this.fanned.size === 0) {
        // The fan-out half is not redundant. A window holding only fan-out records cannot arise
        // from an application write - a write records a shape and then fans out - but it can from a
        // backfill replaying rows, and a window silently discarded is the shape of gap that makes a
        // client's copy look healthier than it is.
        return undefined
      }
      const window: Window = {
        modelVersion: this.modelVersion,
        startedNs: this.startedNs,
        endedNs: now(),
        shapes: [...this.current.values()],
        complete: !this.incomplete,
        droppedWindows: this.dropped,
        fanned: [...this.fanned.values()],
      }
      this.current = new Map()
      this.fanned = new Map()
      this.startedNs = now()
      this.incomplete = false

      // A full buffer drops the oldest window and says so in the next one. Telemetry is the thing
      // that gets lost when we run out of room - never a write, never an operation.
      if (this.windows.length === this.maxWindows) {
        this.windows.shift()
        this.dropped += 1
      }
      this.windows.push(window)
      return window
    })
  }

  pending(): readonly Window[] {
    return [...this.windows]
  }

  /**
   * Called by the application when part of the current period was not recorded.
   *
   * The flag travels in `Window.complete` and the planner reads it, because a window missing a
   * slice of the traffic must not be scored as though it were the whole period. Nothing here
   * decides when that happened: the application collects these windows and hands them over, and
   * only it knows whether a collection was skipped.
   */
  markIncomplete(): void {
    this.incomplete = true
  }

  /**
   * Drop the oldest `count` windows once the application has taken them.
   *
   * The delivering party is the client's own process, and there is no other. This library never
   * opens a connection to us - the buffer is read with `pending`, written wherever the application
   * writes it, and that file is what we are handed.
   */
  acknowledge(count: number): void {
    this.windows = this.windows.slice(Math.min(count, this.windows.length))
  }
}

/**
 * A monotonic clock in nanoseconds.
 *
 * `process.hrtime.bigint()` rather than `Date.now()`: a window's start and end are used to compute
 * durations, and a wall clock can move backwards. Converted to a number because everything that
 * consumes it is arithmetic on durations well inside the safe integer range - 2^53 ns is 104 days.
 */
function now(): number {
  return Number(process.hrtime.bigint())
}

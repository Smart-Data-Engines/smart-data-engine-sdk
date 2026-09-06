/**
 * An in-memory engine, for the `migration/` conformance vectors and for anybody's adapter tests.
 *
 * **Why this is in the library rather than in a test file.** The `migration/` vectors pin behaviour
 * that only happens against an engine: the order of the calls a backfill makes, the arithmetic of
 * the resume marker, which side of the marker a verify counter lands on. Every implementation
 * therefore needs an engine to run them against, and the two options were for each runner to write
 * its own or for the fixture to be shared. A runner that writes its own is a runner whose *fixture*
 * can be the thing that differs, and then a red vector means "one of two tables disagreed" rather
 * than "one of two libraries disagreed" - the failure the whole suite exists to avoid, one level
 * down.
 *
 * **What it is not.** It is not an implementation of anything in the format contract. It stores rows
 * in an array and answers questions about them; every rule the vectors check lives in `migration.ts`
 * and in `watermark.ts`. The one property it has to get right is a keyset scan, and the vectors pin
 * the *calls* as well as the results, so a fixture that scanned differently shows up as a different
 * call sequence rather than as a plausible wrong answer.
 */

import { compareCodePoints } from '../canonical.js'
import { EngineError } from '../errors.js'
import { keyColumns, sameWidth } from '../migration.js'
import type { PhysicalLayout } from '../placement.js'
import type { Row } from '../session.js'

export interface RecordedCall {
  readonly engine: string
  readonly call: string
  readonly [field: string]: unknown
}

/**
 * The calls a set of engines received, in **one** sequence, each entry naming its engine.
 *
 * One journal for the whole set, and that is a fix. The first version kept a list per engine, which
 * cannot express the guarantee the dual-write cases are about: a row reaches the source *before*
 * anything is attempted against the copy. Reversing those two lines passed every vector, because
 * each engine's own list was still in order - and the vector's own note claimed that ordering was
 * what it pinned. A guarantee across two engines needs one sequence.
 */
export class Recorded {
  readonly calls: RecordedCall[] = []

  note(engine: string, call: string, fields: Record<string, unknown> = {}): void {
    this.calls.push({ engine, call, ...fields })
  }
}

export interface MemoryOptions {
  readonly dialect?: string
  readonly tables?: Readonly<Record<string, readonly Row[]>>
  /**
   * Whether this engine offers the two bookkeeping calls and the seven migration ones.
   *
   * False **removes** them rather than making them fail, because that is what an adapter without
   * the capability actually is - and the check both refusals make is "will this object answer these
   * calls".
   */
  readonly canKeepBookkeeping?: boolean
  readonly canMigrate?: boolean
  readonly watermark?: number | null
  readonly markers?: Readonly<Record<string, number>>
  /**
   * How many of the next inserts into each table must fail.
   *
   * The one thing a fake has to be able to do that a real engine does on its own: a fan-out that
   * does not reach the copy is the case the whole dual-write design is about, and it cannot be
   * reached by writing correct rows to a working table.
   */
  readonly failInserts?: Readonly<Record<string, number>>
  readonly name?: string
  readonly journal?: Recorded
}

/**
 * A total order over the value kinds a vector may use, with the kind first.
 *
 * Comparing a string to a number raises in Python and coerces in JavaScript, and neither is a key
 * order. Vectors use one kind per column, so this never has to decide *between* kinds for a real
 * comparison - the tag is there so a vector that accidentally mixed them fails in both languages
 * rather than in one.
 */
function rank(value: unknown): number {
  if (value === null || value === undefined) return 0
  if (typeof value === 'boolean') return 1
  if (typeof value === 'number') return 2
  return 3
}

function compareValues(left: unknown, right: unknown): number {
  const order = rank(left) - rank(right)
  if (order !== 0) return order
  switch (rank(left)) {
    case 0:
      return 0
    case 1:
      return Number(left) - Number(right)
    case 2:
      return (left as number) - (right as number)
    default:
      return compareCodePoints(String(left), String(right))
  }
}

function compareKeys(order: readonly string[], left: Row, right: Row): number {
  for (const column of order) {
    const result = compareValues(left[column], right[column])
    if (result !== 0) return result
  }
  return 0
}

function compareToBound(order: readonly string[], row: Row, bound: readonly unknown[]): number {
  for (let index = 0; index < order.length; index += 1) {
    const result = compareValues(row[order[index] as string], bound[index])
    if (result !== 0) return result
  }
  return 0
}

export class MemoryEngine {
  readonly dialect: string
  readonly name: string
  readonly recorded: Recorded
  tables: Record<string, Row[]> = {}
  private readonly watermarks: number[] = []
  private readonly markers = new Map<string, number[]>()
  private readonly failInserts = new Map<string, number>()

  constructor(options: MemoryOptions = {}) {
    this.dialect = options.dialect ?? 'postgres'
    this.name = options.name ?? 'engine'
    this.recorded = options.journal ?? new Recorded()
    for (const [name, rows] of Object.entries(options.tables ?? {})) {
      this.tables[name] = rows.map((row) => ({ ...row }))
    }
    if (options.watermark !== undefined && options.watermark !== null) {
      this.watermarks.push(options.watermark)
    }
    for (const [key, value] of Object.entries(options.markers ?? {})) {
      this.markers.set(key, [value])
    }
    for (const [table, count] of Object.entries(options.failInserts ?? {})) {
      this.failInserts.set(table, count)
    }
    const target = this as unknown as Record<string, unknown>
    if (options.canKeepBookkeeping === false) {
      for (const name of ['mapWatermark', 'recordMapVersion']) target[name] = undefined
    }
    if (options.canMigrate === false) {
      for (const name of [
        'keyRange',
        'nthKey',
        'copyIn',
        'count',
        'backfillMarker',
        'recordBackfillMarker',
      ]) {
        target[name] = undefined
      }
    }
  }

  private note(call: string, fields: Record<string, unknown> = {}): void {
    this.recorded.note(this.name, call, fields)
  }

  private rows(table: string): Row[] {
    const existing = this.tables[table]
    if (existing !== undefined) return existing
    const created: Row[] = []
    this.tables[table] = created
    return created
  }

  // --- Engine ------------------------------------------------------------------------------

  async ensureSchema(
    layout: PhysicalLayout,
    _options: { readonly keys: Readonly<Record<string, readonly string[]>> },
  ): Promise<void> {
    this.note('ensure_schema', { tables: Object.values(layout.tables).sort(compareCodePoints) })
    for (const table of Object.values(layout.tables)) this.rows(table)
  }

  async insert(table: string, values: Readonly<Row>): Promise<void> {
    this.note('insert', { table })
    const remaining = this.failInserts.get(table) ?? 0
    if (remaining > 0) {
      this.failInserts.set(table, remaining - 1)
      throw new EngineError(`insert into ${table} failed: this engine was told to refuse it`)
    }
    this.rows(table).push({ ...values })
  }

  async get(table: string, key: Readonly<Row>): Promise<Row | null> {
    this.note('get', { table })
    for (const row of this.rows(table)) {
      if (Object.entries(key).every(([column, value]) => row[column] === value)) return { ...row }
    }
    return null
  }

  /**
   * Snapshot, run, and put the snapshot back on failure.
   *
   * Enough to make a rollback observable, which is what the dual-write cases need: rows written
   * inside a transaction that throws must not reach the copy, and the only way to check that is for
   * the source to forget them too.
   */
  async transaction<T>(body: () => Promise<T>): Promise<T> {
    this.note('transaction')
    const snapshot: Record<string, Row[]> = {}
    for (const [name, rows] of Object.entries(this.tables)) {
      snapshot[name] = rows.map((row) => ({ ...row }))
    }
    try {
      return await body()
    } catch (error) {
      this.tables = snapshot
      throw error
    }
  }

  // --- WatermarkStore ----------------------------------------------------------------------

  async mapWatermark(): Promise<number | null> {
    this.note('map_watermark')
    return this.watermarks.length === 0 ? null : Math.max(...this.watermarks)
  }

  async recordMapVersion(version: number, _options: { readonly modelVersion: string }): Promise<void> {
    this.note('record_map_version', { version })
    this.watermarks.push(version)
  }

  // --- Migratable --------------------------------------------------------------------------

  async keyRange(
    table: string,
    order: readonly string[],
    options: {
      readonly after?: readonly unknown[]
      readonly upto?: readonly unknown[]
      readonly limit?: number
    } = {},
  ): Promise<Row[]> {
    const cols = keyColumns(order, table)
    if (options.after !== undefined) sameWidth(options.after, cols, 'after')
    if (options.upto !== undefined) sameWidth(options.upto, cols, 'upto')
    this.note('key_range', {
      table,
      after: options.after === undefined ? null : [...options.after],
      upto: options.upto === undefined ? null : [...options.upto],
      limit: options.limit ?? null,
    })
    let rows = [...this.rows(table)].sort((a, b) => compareKeys(cols, a, b))
    if (options.after !== undefined) {
      const after = options.after
      rows = rows.filter((row) => compareToBound(cols, row, after) > 0)
    }
    if (options.upto !== undefined) {
      const upto = options.upto
      rows = rows.filter((row) => compareToBound(cols, row, upto) <= 0)
    }
    if (options.limit !== undefined) rows = rows.slice(0, options.limit)
    return rows.map((row) => ({ ...row }))
  }

  async nthKey(
    table: string,
    order: readonly string[],
    options: { readonly position: number },
  ): Promise<unknown[] | null> {
    const cols = keyColumns(order, table)
    this.note('nth_key', { table, position: options.position })
    if (options.position < 1) {
      throw new EngineError(`position is one-based; ${options.position} is not a row`)
    }
    const rows = [...this.rows(table)].sort((a, b) => compareKeys(cols, a, b))
    if (options.position > rows.length) return null
    const row = rows[options.position - 1] as Row
    return cols.map((column) => row[column])
  }

  async copyIn(table: string, rows: readonly Row[]): Promise<void> {
    this.note('copy_in', { table, rows: rows.length })
    if (rows.length === 0) return
    const columns = Object.keys(rows[0] as Row).sort(compareCodePoints)
    for (const row of rows) {
      const here = Object.keys(row).sort(compareCodePoints)
      if (here.join(' ') !== columns.join(' ')) {
        throw new EngineError(
          `copyIn into ${table} was given rows with different columns ([${columns.join(', ')}] ` +
            `and [${here.join(', ')}]). A chunk comes from one table, so this is a caller ` +
            `assembling it from two.`,
        )
      }
    }
    const existing = this.rows(table)
    // Idempotent on the whole row's identity, which is what both real targets do by a different
    // mechanism: ON CONFLICT DO NOTHING in PostgreSQL and a ReplacingMergeTree collapse in
    // ClickHouse. Not the same mechanism, which is why the adapters have live tests in every
    // direction and this only has to absorb a recopy.
    for (const row of rows) {
      const present = existing.some((candidate) =>
        Object.keys(row).every((column) => candidate[column] === row[column]),
      )
      if (!present) existing.push({ ...row })
    }
  }

  async count(table: string): Promise<number> {
    this.note('count', { table })
    return this.rows(table).length
  }

  async backfillMarker(options: {
    readonly materialization: string
    readonly entity: string
  }): Promise<number> {
    this.note('backfill_marker', {
      materialization: options.materialization,
      entity: options.entity,
    })
    const seen = this.markers.get(`${options.materialization}|${options.entity}`)
    return seen === undefined || seen.length === 0 ? 0 : Math.max(...seen)
  }

  async recordBackfillMarker(options: {
    readonly materialization: string
    readonly entity: string
    readonly rows: number
  }): Promise<void> {
    this.note('record_backfill_marker', {
      materialization: options.materialization,
      entity: options.entity,
      rows: options.rows,
    })
    const key = `${options.materialization}|${options.entity}`
    this.markers.set(key, [...(this.markers.get(key) ?? []), options.rows])
  }
}

export interface EngineSpec {
  readonly dialect?: string
  readonly tables?: Readonly<Record<string, readonly Row[]>>
  readonly bookkeeping?: boolean
  readonly migratable?: boolean
  readonly watermark?: number | null
  readonly markers?: Readonly<Record<string, number>>
  readonly fail_inserts?: Readonly<Record<string, number>>
}

/**
 * Build the engine set a `migration/` case describes.
 *
 * Here rather than in the runner for the same reason the engine itself is here: two runners that
 * each read this document their own way can disagree about the *fixture*, and then a red vector
 * says "one of two tables differed" instead of "one of two libraries differed".
 */
export function enginesFrom(
  spec: Readonly<Record<string, EngineSpec>>,
  journal: Recorded = new Recorded(),
): Record<string, MemoryEngine> {
  const built: Record<string, MemoryEngine> = {}
  for (const [name, body] of Object.entries(spec)) {
    built[name] = new MemoryEngine({
      dialect: body.dialect ?? 'postgres',
      name,
      journal,
      tables: body.tables ?? {},
      canKeepBookkeeping: body.bookkeeping ?? true,
      canMigrate: body.migratable ?? true,
      watermark: body.watermark ?? null,
      markers: body.markers ?? {},
      failInserts: body.fail_inserts ?? {},
    })
  }
  return built
}

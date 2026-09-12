/**
 * Copying a group's rows into the copy a map fans writes out to, and comparing the two.
 *
 * Both halves read the client's data, which is why they are here in the public library and not in
 * the control plane. What crosses the boundary to us is **numbers, never rows**: a verify report
 * carries seven counts and the detail that makes a mismatch fixable stays on the client's machine.
 *
 * **The progress marker is a row count, never a key, and that is the whole design.** A key resumes
 * exactly and needs a **codec**: every key value would have to round-trip through JSON and back, in
 * every language that ever writes an adapter - and a lossy codec puts the resume point *after* rows
 * nobody copied. That is silent data loss, and it differs per language. A count cannot fail that
 * way; resuming costs one indexed scan, once per resume, rather than once per chunk.
 *
 * Three properties hold each other up:
 *
 * **The chunk goes before the marker.** A crash in between costs a recopy, which the target's key
 * semantics absorb. The other order costs the chunk, permanently.
 *
 * **Idempotence comes from the target's semantics** - `ON CONFLICT DO NOTHING` in PostgreSQL, a
 * `ReplacingMergeTree` collapse in ClickHouse. Not the same mechanism, which is why the live tests
 * run in every direction rather than in one.
 *
 * **There is no ceiling and no second pass**, because dual write precedes backfill: reaching the end
 * of the table **once** is enough. Rows arriving above that point are the fan-out's. The narrow
 * claim that holds is worth stating precisely, because a wider one was written first and was false:
 * no row that existed when the backfill started is skipped - inserting rows can only move a row
 * further along the key order, never before the resume point. Rows that arrived later belong to the
 * fan-out, which is the same premise the missing ceiling stands on.
 */

import { compareCodePoints } from './canonical.js'
import { Timestamp } from './timestamp.js'
import type { VerificationRequest } from './verification.js'
import { MIGRATABLE_MEMBERS, satisfies } from './capabilities.js'
import { EngineError, MigrationRefused } from './errors.js'
import { colocationGroups } from './groups.js'
import { groupColumns } from './layout.js'
import type { Materialization } from './placement.js'
import { placementOf } from './placement.js'
import type { Row, Session } from './session.js'
import { tableFor } from './session.js'

/**
 * Rows per chunk, by default.
 *
 * A thousand rather than a round ten thousand: a chunk is held in memory twice during `verify` -
 * the source's rows and the target's - and the number that matters is not throughput but how much
 * work a crash discards, which is one chunk.
 */
export const CHUNK_ROWS = 1000

/**
 * Sub-second digits each dialect keeps, for the neutral types where dialects differ.
 *
 * Both current SQL timestamp types retain six digits. This table guards future dialects; the
 * runtime value must preserve those digits too, which is why the adapters return Timestamp.
 */
export const DIALECT_PRECISION: Readonly<Record<string, number>> = {
  'timestamp|postgres': 6,
  'timestamptz|postgres': 6,
  'timestamp|clickhouse': 6,
  'timestamptz|clickhouse': 6,
}

/**
 * Neutral types a copy between dialects does not silently change.
 *
 * Not the same claim as "every value survives". PostgreSQL's `date` has a wider range than
 * ClickHouse's `Date32`, so a date in the year 1800 does not survive that move - but it fails
 * *loudly*, on the insert or as a mismatch in `verify`. The line drawn here is silent-and-universal
 * loss, which is a much smaller set than lossy, and it is the set worth a refusal that arrives
 * before the work.
 */
export const PRECISION_INDEPENDENT: ReadonlySet<string> = new Set([
  'bool',
  'int32',
  'int64',
  'float32',
  'float64',
  'string',
  'bytes',
  'uuid',
  'date',
  'json',
])

/**
 * What an engine adapter has to offer for a group to be migrated into or out of it.
 *
 * Optional, exactly like the watermark store and for the same reason: requiring these of the engine
 * interface would break every adapter anybody has written, including fakes in someone else's test
 * suite, for a capability an engine with a fixed schema cannot provide. Non-participation is a
 * **named refusal** rather than a silent skip, because a migration that quietly copies nothing is
 * the worst outcome available here.
 */
export interface Migratable {
  readonly dialect: string
  keyRange(
    table: string,
    order: readonly string[],
    options?: {
      readonly after?: readonly unknown[]
      readonly upto?: readonly unknown[]
      readonly limit?: number
    },
  ): Promise<Row[]>
  nthKey(table: string, order: readonly string[], options: { readonly position: number }): Promise<unknown[] | null>
  copyIn(table: string, rows: readonly Row[]): Promise<void>
  count(table: string): Promise<number>
  get(table: string, key: Readonly<Row>): Promise<Row | null>
  backfillMarker(options: { readonly materialization: string; readonly entity: string }): Promise<number>
  recordBackfillMarker(options: {
    readonly materialization: string
    readonly entity: string
    readonly rows: number
  }): Promise<void>
}

/** A compile-time ratchet between the interface and the list the runtime check uses. */
export const MIGRATABLE_IS_TOTAL: (typeof MIGRATABLE_MEMBERS)[number] extends keyof Migratable
  ? true
  : never = true

/**
 * The ordering columns for a keyset scan, refusing an empty one.
 *
 * Here rather than in each adapter so that the two cannot disagree about it, and exported because
 * an adapter written outside this repository has the same argument to check. An empty order is not
 * a scan of everything in an unspecified order - it is a paginated scan with no pagination, which
 * returns the same first page forever.
 */
export function keyColumns(order: readonly string[], table: string): readonly string[] {
  if (order.length === 0) {
    throw new EngineError(
      `a keyset scan of ${table} needs at least one ordering column. With none, every page is the ` +
        `first page and a backfill would copy the same chunk until it was stopped.`,
    )
  }
  return [...order]
}

/** A bound has one value per ordering column, or the comparison is not the one intended. */
export function sameWidth(bound: readonly unknown[], cols: readonly string[], name: string): void {
  if (bound.length !== cols.length) {
    throw new EngineError(
      `${name} has ${bound.length} values and the order has ${cols.length} columns ` +
        `[${cols.join(', ')}]. A row-value comparison of different widths is not a narrower ` +
        `comparison, it is a different one.`,
    )
  }
}

interface Copy {
  readonly entity: string
  readonly key: readonly string[]
  readonly source: Migratable
  readonly sourceEngine: string
  readonly sourceTable: string
  readonly target: Migratable
  readonly targetEngine: string
  readonly targetId: string
  readonly targetTable: string
}

/** How far one entity's copy into one target has got. */
export interface EntityProgress {
  readonly entity: string
  readonly engine: string
  readonly table: string
  /** The marker: rows of this entity copied into this target, across every run. */
  readonly rowsCopied: number
  readonly rowsThisRun: number
  readonly chunks: number
  /** Whether the last chunk came back short, which is what "the tail is the fan-out's now" means. */
  readonly complete: boolean
}

export function entityProgressRecord(progress: EntityProgress): Record<string, unknown> {
  return {
    entity: progress.entity,
    engine: progress.engine,
    table: progress.table,
    rows_copied: progress.rowsCopied,
    rows_this_run: progress.rowsThisRun,
    chunks: progress.chunks,
    complete: progress.complete,
  }
}

/** What one call to {@link backfill} did, per entity and per target. */
export interface BackfillProgress {
  readonly group: string
  readonly entities: readonly EntityProgress[]
  /** Every entity of every target has reached the end of its table at least once. */
  readonly complete: boolean
  readonly rowsThisRun: number
}

export function backfillRecord(progress: BackfillProgress): Record<string, unknown> {
  return {
    group: progress.group,
    complete: progress.complete,
    rows_this_run: progress.rowsThisRun,
    entities: progress.entities.map(entityProgressRecord),
  }
}

export function backfillForAHuman(progress: BackfillProgress): string {
  const lines = [
    `backfill of ${progress.group}: ${progress.complete ? 'complete' : 'more to do'}, ` +
      `${progress.rowsThisRun} rows this run`,
  ]
  for (const entity of progress.entities) {
    lines.push(
      `  ${entity.entity} -> ${entity.engine}.${entity.table}: ${entity.rowsCopied} rows copied ` +
        `(${entity.rowsThisRun} this run, ${entity.chunks} chunks)` +
        `${entity.complete ? '' : ', more to do'}`,
    )
  }
  return lines.join('\n')
}

/**
 * One source row the target does not have, or has differently.
 *
 * **This holds the client's own data and it is the reason the verify record does not.** The key is
 * here because "which row" is the first thing an operator needs and a count cannot say it; it stays
 * on their machine because a row is the one thing that must never travel to us. The two facts are
 * the same decision seen from two sides.
 */
export interface Difference {
  readonly entity: string
  readonly table: string
  readonly key: Readonly<Row>
  /** Columns whose values differ, or empty when the row is absent from the target altogether. */
  readonly columns: readonly string[]
}

const DIFFERENCES_KEPT = 20

/**
 * What the comparison found, in the shape the gate needs and nothing wider.
 *
 * {@link verifyRecord} is the boundary. It carries seven counts, and the control plane reads exactly
 * those - so there is no field in which a value of the client's could travel, and adding one would
 * be a visible change to that function rather than an accident somewhere in a call chain.
 * `differences` is the other half of that: the detail that makes a mismatch fixable, kept here and
 * deliberately absent from the record.
 */
export interface VerifyReport {
  readonly request?: VerificationRequest
  readonly at: string
  readonly group: string
  readonly chunksCompared: number
  readonly chunksMismatched: number
  readonly tailRowsRead: number
  readonly tailRowsMissingInTarget: number
  readonly rowsSource: number
  readonly rowsTarget: number
  readonly differences: readonly Difference[]
  readonly differencesSuppressed: number
  /**
   * Whether the target holds everything the source holds. Zero tolerance, both terms.
   *
   * Zero rather than a threshold, and the reason is arithmetic rather than principled: any non-zero
   * threshold is an answer to "how many of your rows may we lose", and there is no number to say
   * out loud there.
   */
  readonly matched: boolean
}

/** The seven counts the gate reads. **Numbers, never rows** - see {@link VerifyReport}. */
export function verifyRecord(report: VerifyReport): Record<string, unknown> {
  return {
    at: report.at,
    ...(report.request === undefined ? {} : { request: report.request.asRecord() }),
    chunks_compared: report.chunksCompared,
    chunks_mismatched: report.chunksMismatched,
    tail_rows_read: report.tailRowsRead,
    tail_rows_missing_in_target: report.tailRowsMissingInTarget,
    rows_source: report.rowsSource,
    rows_target: report.rowsTarget,
  }
}

export function verifyForAHuman(report: VerifyReport): string {
  const lines = [
    `verify of ${report.group} at ${report.at}: ${report.matched ? 'matched' : 'DID NOT MATCH'}`,
    `  below the marker: ${report.chunksCompared} chunks compared, ` +
      `${report.chunksMismatched} mismatched` +
      (report.chunksMismatched === 0 ? '' : '  <- the backfill did not copy these'),
    `  above the marker: ${report.tailRowsRead} rows read, ` +
      `${report.tailRowsMissingInTarget} missing in the copy` +
      (report.tailRowsMissingInTarget === 0
        ? ''
        : '  <- the dual-write fan-out did not reach these'),
    `  rows: ${report.rowsSource} in the source, ${report.rowsTarget} in the copy (reported, not ` +
      `gated on: two counts of live tables are taken at different instants)`,
  ]
  if (report.differences.length > 0) {
    lines.push(
      '  the rows below are your own data. They are not part of what is reported to Smart Data ' +
        'Engines:',
    )
    for (const difference of report.differences) {
      const where =
        difference.columns.length === 0
          ? 'absent'
          : `differs in [${difference.columns.join(', ')}]`
      lines.push(`    ${difference.entity} ${JSON.stringify(difference.key)} -> ${where}`)
    }
    if (report.differencesSuppressed > 0) {
      lines.push(`    ... and ${report.differencesSuppressed} more`)
    }
  }
  return lines.join('\n')
}

function migratable(session: Session, name: string, role: string, group: string): Migratable {
  const engine = session.engineNamed(name)
  if (!satisfies(engine, MIGRATABLE_MEMBERS)) {
    throw new MigrationRefused(
      `'${name}' cannot act as the ${role} of a migration of '${group}': its adapter does not ` +
        `offer the row-level operations a copy needs. An engine whose schema is fixed in its own ` +
        `source has nowhere to keep a progress marker and no table to scan in key order, so this ` +
        `is a property of the engine rather than a missing feature. Refused here rather than ` +
        `skipped, because a migration that copies nothing and says nothing is the worst thing ` +
        `this module could do.`,
    )
  }
  return engine as unknown as Migratable
}

/**
 * Why a copy of these columns between these two dialects would change values, or `null`.
 *
 * A string rather than a throw, and dialect names rather than engines, because two doors ask this
 * question and only one of them used to. `backfill` refuses a copy that would truncate; the write
 * fan-out in `Session` was doing the same truncation one row at a time, for as long as an
 * `also_write` map was in force. Measured on live servers before it was fixed: a `timestamptz`
 * written as `09:30:15.123456` came back from PostgreSQL unchanged and from ClickHouse as
 * `09:30:15.123`, with no error on either side.
 */
export function precisionRefusal(
  group: string,
  entity: string,
  columns: Readonly<Record<string, string>>,
  sourceDialect: string,
  targetDialect: string,
): string | null {
  for (const column of Object.keys(columns).sort(compareCodePoints)) {
    const neutral = columns[column] as string
    if (neutral.startsWith('decimal(') || PRECISION_INDEPENDENT.has(neutral)) continue
    const here = DIALECT_PRECISION[`${neutral}|${sourceDialect}`]
    const there = DIALECT_PRECISION[`${neutral}|${targetDialect}`]
    if (here === undefined || there === undefined) {
      return (
        `${group}.${entity}.${column} has neutral type '${neutral}', and this library does not ` +
        `know whether ${sourceDialect} and ${targetDialect} store it to the same precision. ` +
        `Refused rather than attempted: a type nobody classified is a type nobody checked, and ` +
        `the failure mode of guessing here is a value that comes back changed with no error ` +
        `anywhere.`
      )
    }
    if (there < here) {
      return (
        `${group}.${entity}.${column} is '${neutral}', which ${sourceDialect} stores to ${here} ` +
        `sub-second digits and ${targetDialect} to ${there}. Copying it would truncate every ` +
        `value with more precision than that - silently, because the insert succeeds and the ` +
        `value comes back changed - and verify would then find every such row mismatched at ` +
        `the end of the copy rather than before it. Your rows may all happen to be aligned to ` +
        `${there} digits, in which case this refusal costs you a migration that would have ` +
        `worked; we cannot tell without reading your data, and a copy that is faithful only ` +
        `for ` +
        `the values that happen to be present is not something to build a gate on.`
      )
    }
  }
  return null
}

/**
 * The target's table has the same columns as the source's, or this is not a move.
 *
 * A fan-out target is allowed to be any derived materialisation, and a derived materialisation is
 * allowed to be a denormalised wide table - which is a useful thing and not a migration target.
 * Filling one means reading the group's relations and assembling rows that exist in no single
 * table, and this module copies rows. Refused by name, because the alternative is a copy that
 * leaves the extra columns null and looks like it worked.
 */
function shapesAgree(
  group: string,
  entity: string,
  source: Materialization,
  target: Materialization,
): void {
  const here = Object.keys(source.layout.columns[entity] ?? {})
  const there = Object.keys(target.layout.columns[entity] ?? {})
  for (const [label, columns, materialization] of [
    ['source', here, source],
    ['target', there, target],
  ] as const) {
    if (columns.length === 0) {
      throw new MigrationRefused(
        `the ${label} materialisation '${materialization.id}' of '${group}' does not describe the ` +
          `columns of ${entity}, so a copy cannot be checked for shape before it starts. ` +
          `ensureSchema needs them too; a layout with tables and no columns is not one this ` +
          `library can apply.`,
      )
    }
  }
  const onlySource = here.filter((column) => !there.includes(column)).sort(compareCodePoints)
  const onlyTarget = there.filter((column) => !here.includes(column)).sort(compareCodePoints)
  if (onlySource.length > 0 || onlyTarget.length > 0) {
    throw new MigrationRefused(
      `${group}.${entity} has different columns in '${source.id}' and '${target.id}' (only in the ` +
        `source: [${onlySource.join(', ')}]; only in the target: [${onlyTarget.join(', ')}]). That ` +
        `is a reshape rather than a move: filling a wide table means reading the group's relations ` +
        `and assembling rows that exist in no single table, and this module copies rows. A copy ` +
        `would leave the extra columns null and look like it had worked.`,
    )
  }
}

/**
 * Every refusal, before a single row moves.
 *
 * A migration is the operation with the least tolerance for a late discovery in this whole library:
 * the cost of finding a problem at chunk four thousand is four thousand chunks of the client's I/O
 * and an operator who now has to decide whether what has been copied is safe to leave.
 */
function plan(session: Session, group: string): readonly Copy[] {
  const members = colocationGroups(session.model).find((candidate) => candidate.name === group)
  if (members === undefined) {
    const names = colocationGroups(session.model)
      .map((candidate) => candidate.name)
      .sort(compareCodePoints)
    throw new MigrationRefused(
      `'${group}' is not a colocation group of this model. It has [${names.join(', ')}].`,
    )
  }
  const body = placementOf(session.placement, group)
  if (body.alsoWrite.length === 0) {
    throw new MigrationRefused(
      `'${group}' has no fan-out target in this map, so there is nothing to backfill. A migration ` +
        `reaches this library as a placement map with 'also_write' - there is no phase name in the ` +
        `document and no second channel - so a map without that key is one that says this group is ` +
        `not being migrated.`,
    )
  }
  const source = migratable(session, body.source.engine, 'source', group)
  const columns = groupColumns(session.model, members)

  const copies: Copy[] = []
  for (const copy of body.alsoWrite) {
    const target = migratable(session, copy.engine, 'target', group)
    for (const entity of members.members) {
      const spec = session.model.entities.find((candidate) => candidate.name === entity)
      const key = spec === undefined ? [] : [...spec.key]
      if (key.length === 0) {
        throw new MigrationRefused(
          `${group}.${entity} has no key, so its rows cannot be scanned in a stable order and a ` +
            `chunk boundary would not mean anything.`,
        )
      }
      shapesAgree(group, entity, body.source, copy)
      // No precision check here. `Session.open` refuses a map whose fan-out would truncate,
      // over every group, and this needs a session - so a copy reaching this line has already
      // been through that door. Asking twice leaves a branch no mutation can reach alone.
      copies.push({
        entity,
        key,
        source,
        sourceEngine: body.source.engine,
        sourceTable: tableFor(body.source.layout, entity),
        target,
        targetEngine: copy.engine,
        targetId: copy.id,
        targetTable: tableFor(copy.layout, entity),
      })
    }
  }
  return copies
}

export interface BackfillOptions {
  readonly chunkRows?: number
  /**
   * Bound the work to that many chunks per entity, for an operator who wants to copy for a while
   * and stop, and for a test that needs to interrupt at a known point. Absent runs each entity to
   * the end of its table.
   */
  readonly stopAfter?: number
}

/**
 * Copy a group's existing rows into every fan-out target the map names. Resumable.
 *
 * Called again after an interruption it picks up from the marker, and called again after completion
 * it does nothing - both because the marker is durable and lives in the target engine, next to the
 * rows it describes. That is the correct coupling: a target dropped and recreated loses its marker
 * with its data, and a marker kept anywhere else would claim work that no longer exists.
 */
export async function backfill(
  session: Session,
  group: string,
  options: BackfillOptions = {},
): Promise<BackfillProgress> {
  const chunkRows = options.chunkRows ?? CHUNK_ROWS
  if (chunkRows < 1) throw new MigrationRefused(`a chunk of ${chunkRows} rows is not a chunk`)
  const entities: EntityProgress[] = []
  for (const copy of plan(session, group)) {
    entities.push(await backfillOne(copy, chunkRows, options.stopAfter))
  }
  return {
    group,
    entities,
    complete: entities.every((entity) => entity.complete),
    rowsThisRun: entities.reduce((sum, entity) => sum + entity.rowsThisRun, 0),
  }
}

async function backfillOne(
  copy: Copy,
  chunkRows: number,
  stopAfter: number | undefined,
): Promise<EntityProgress> {
  let marker = await copy.target.backfillMarker({
    materialization: copy.targetId,
    entity: copy.entity,
  })
  let after = await resumePoint(copy, marker)
  let rowsThisRun = 0
  let chunks = 0
  let complete = false
  while (stopAfter === undefined || chunks < stopAfter) {
    const rows = await copy.source.keyRange(
      copy.sourceTable,
      copy.key,
      after === null ? { limit: chunkRows } : { after, limit: chunkRows },
    )
    if (rows.length === 0) {
      complete = true
      break
    }
    // The chunk first, then the marker. A crash between them costs a recopy, which the target's key
    // semantics absorb; the other order costs the chunk, permanently.
    await copy.target.copyIn(copy.targetTable, rows)
    marker += rows.length
    await copy.target.recordBackfillMarker({
      materialization: copy.targetId,
      entity: copy.entity,
      rows: marker,
    })
    const last = rows[rows.length - 1] as Row
    after = copy.key.map((column) => last[column])
    rowsThisRun += rows.length
    chunks += 1
    if (rows.length < chunkRows) {
      // The end of the table, once. Rows arriving above this point from here on are the fan-out's,
      // which is why there is no ceiling and no second pass.
      complete = true
      break
    }
  }
  return {
    entity: copy.entity,
    engine: copy.targetEngine,
    table: copy.targetTable,
    rowsCopied: marker,
    rowsThisRun,
    chunks,
    complete,
  }
}

/**
 * Turn a row count back into a key, or refuse if the source has lost rows.
 *
 * One indexed scan, paid once per resume rather than once per chunk, which is the trade that makes a
 * row count an acceptable marker. If rows have been inserted below this point since the marker was
 * written, row N is now an earlier row, so this key moves down and the backfill recopies.
 *
 * A source with fewer rows than the marker claims were copied is the one case that refuses. It means
 * rows left the source outside this library, and a backfill cannot resume against a table that has
 * shrunk: the marker would be describing a table that no longer exists.
 */
async function resumePoint(copy: Copy, marker: number): Promise<readonly unknown[] | null> {
  if (marker <= 0) return null
  const position = await copy.source.nthKey(copy.sourceTable, copy.key, { position: marker })
  if (position === null) {
    throw new MigrationRefused(
      `the marker for ${copy.entity} in ${copy.targetEngine} says ${marker} rows have been copied, ` +
        `and ${copy.sourceEngine}.${copy.sourceTable} does not have that many. Rows have left the ` +
        `source outside this library, so the marker describes a table that no longer exists and ` +
        `resuming from it would be guessing. Nothing has been copied by this call.`,
    )
  }
  return position
}

export interface VerifyOptions {
  readonly request?: VerificationRequest
  readonly chunkRows?: number
  /**
   * The instant the report is stamped with. Absent reads the calendar clock.
   *
   * An argument at all because this library reads a wall clock in exactly two places and both are
   * pinned by a test - a map carries no date, so the only thing a clock here could do is stamp a
   * report. Passing one makes the report reproducible for a caller who wants that.
   */
  readonly at?: string
}

/**
 * Compare both copies of a group and report counts. The gate reads the counts, not the rows.
 *
 * Reads the **source first and the target second**, always, and the order is load-bearing. A write
 * is in the source before it is in the copy, so a row read from the source may not have reached the
 * copy yet - but the copy is read after the whole source window, so the window has already elapsed.
 * Anything still missing is then looked up once more by point read, on what is normally an empty
 * set. Reversing the two reads would make this flaky in the direction that stops a healthy
 * migration.
 */
export async function verify(
  session: Session,
  group: string,
  options: VerifyOptions = {},
): Promise<VerifyReport> {
  options.request?.checkSession(session.placement, session.projectId, group)
  if (options.at !== undefined) options.request?.checkTime(options.at)
  if (options.request !== undefined && session.model.version !== options.request.modelVersion) {
    throw new MigrationRefused('verification request names another session model')
  }
  const chunkRows = options.chunkRows ?? CHUNK_ROWS
  if (chunkRows < 1) throw new MigrationRefused(`a chunk of ${chunkRows} rows is not a chunk`)
  let chunksCompared = 0
  let chunksMismatched = 0
  let tailRowsRead = 0
  let tailMissing = 0
  let rowsSource = 0
  let rowsTarget = 0
  const differences: Difference[] = []
  let suppressed = 0

  for (const copy of plan(session, group)) {
    const marker = await copy.target.backfillMarker({
      materialization: copy.targetId,
      entity: copy.entity,
    })
    rowsSource += await copy.source.count(copy.sourceTable)
    rowsTarget += await copy.target.count(copy.targetTable)
    let after: readonly unknown[] | null = null
    let seen = 0
    for (;;) {
      const below = seen < marker
      const want = below ? Math.min(chunkRows, marker - seen) : chunkRows
      const rows = await copy.source.keyRange(
        copy.sourceTable,
        copy.key,
        after === null ? { limit: want } : { after, limit: want },
      )
      if (rows.length === 0) break
      const last = rows[rows.length - 1] as Row
      const high = copy.key.map((column) => last[column])
      const missing = await missingInTarget(copy, rows, after, high)
      if (below) {
        chunksCompared += 1
        if (missing.length > 0) chunksMismatched += 1
      } else {
        tailRowsRead += rows.length
        tailMissing += missing.length
      }
      for (const difference of missing) {
        if (differences.length < DIFFERENCES_KEPT) differences.push(difference)
        else suppressed += 1
      }
      after = high
      seen += rows.length
    }
  }

  const at = options.at ?? new Date().toISOString()
  options.request?.checkTime(at)
  return {
    at,
    ...(options.request === undefined ? {} : { request: options.request }),
    group,
    chunksCompared,
    chunksMismatched,
    tailRowsRead,
    tailRowsMissingInTarget: tailMissing,
    rowsSource,
    rowsTarget,
    differences,
    differencesSuppressed: suppressed,
    matched: chunksMismatched === 0 && tailMissing === 0,
  }
}

/**
 * Which of these source rows the target does not have, or has differently.
 *
 * One windowed read of the target instead of one point read per row, and then point reads only for
 * what the window says is missing - which is normally nothing. The second look is what absorbs a
 * fan-out that was in flight during the first: it happens after the whole window has been read, so
 * the write has had that long to land.
 */
async function missingInTarget(
  copy: Copy,
  rows: readonly Row[],
  low: readonly unknown[] | null,
  high: readonly unknown[],
): Promise<readonly Difference[]> {
  const mirror = await copy.target.keyRange(
    copy.targetTable,
    copy.key,
    low === null ? { upto: high } : { after: low, upto: high },
  )
  const index = new Map<string, Row>()
  for (const row of mirror) index.set(keyOf(copy.key, row), row)
  const out: Difference[] = []
  for (const row of rows) {
    const there = index.get(keyOf(copy.key, row))
    if (there !== undefined && differingColumns(row, there).length === 0) continue
    // Not there, or there and different. Look once more, directly, before calling it a loss.
    const named: Row = {}
    for (const column of copy.key) named[column] = row[column]
    const again = await copy.target.get(copy.targetTable, named)
    if (again !== null) {
      const differs = differingColumns(row, again)
      if (differs.length === 0) continue
      out.push({ entity: copy.entity, table: copy.targetTable, key: named, columns: differs })
      continue
    }
    out.push({ entity: copy.entity, table: copy.targetTable, key: named, columns: [] })
  }
  return out
}

/**
 * A comparable string for one row's key.
 *
 * `JSON.stringify` of the values in key order, which is a **lookup key inside this function** and
 * never a stored one - the distinction the whole module rests on. A codec good enough to index a
 * chunk in memory is not good enough to resume a backfill from, because a lossy round trip there
 * puts the resume point after rows nobody copied.
 */
function keyOf(order: readonly string[], row: Row): string {
  // Timestamp.toJSON preserves six digits; native int64 keys must also remain exact.
  return JSON.stringify(order.map((column) => row[column] ?? null), (_key, value: unknown) =>
    typeof value === 'bigint' ? ['bigint', value.toString()] : value,
  )
}

/**
 * Columns of the **source** row whose values the target does not match, by name.
 *
 * Compared value by value rather than by digest, and the reason is diagnostics. Both copies are read
 * by one process on one machine, so a checksum would compress a comparison that costs nothing to do
 * exactly - and an exact comparison can say *which column* differs, which is the difference between
 * an operator who can fix a migration and one who can only stop it.
 *
 * Over the source's columns and not over the union, which is a decision rather than an oversight.
 * `ensureSchema` allows a table to have columns the map does not name - it permits the write, because
 * a client may have added one outside SDE - so a copy table with an extra column is a supported
 * state. Comparing the union would then report *every* row as differing, on a column that has
 * nothing to do with the copy, and stop a healthy migration.
 */
function differingColumns(source: Row, target: Row): readonly string[] {
  const out: string[] = []
  for (const column of Object.keys(source)) {
    if (!(column in target) || !sameValue(source[column], target[column])) out.push(column)
  }
  return out.sort(compareCodePoints)
}

/**
 * Whether two column values are the same.
 *
 * Exact epoch microseconds for Timestamp, with Date and Buffer also reduced to comparable values.
 * This runtime's `===` on two equal dates is false and would report every timestamp column
 * as differing - the failure mode being a healthy migration that can never pass its gate. The
 * reference implementation gets this for free from Python's `==`.
 */
function sameValue(left: unknown, right: unknown): boolean {
  if (left instanceof Timestamp && right instanceof Timestamp) {
    return left.epochMicroseconds === right.epochMicroseconds
  }
  if (left instanceof Date && right instanceof Date) return left.getTime() === right.getTime()
  if (left instanceof Uint8Array && right instanceof Uint8Array) {
    return left.length === right.length && left.every((byte, index) => byte === right[index])
  }
  if (typeof left === 'bigint' || typeof right === 'bigint') {
    // A driver may hand back a bigint where the other hands back a number for the same int64
    // column. Comparing those with `Object.is` reports every row as differing.
    return String(left) === String(right)
  }
  return Object.is(left, right)
}

/**
 * Routing operations for one model against one placement, and the dual write that makes a
 * migration possible.
 *
 * Holds no connection state of its own: engines do that. It exists to answer "where does this
 * operation go" and to refuse the operations that cannot be answered - and both of those are pure
 * functions of the model and the map, which is why this class has no configuration.
 *
 * **Everything here is asynchronous, and that is a decision rather than a fashion.** Node's I/O is
 * asynchronous; a synchronous wrapper around it means blocking the event loop, which is a worse
 * thing to do to a client's process than a `Promise` in a signature. So the split follows the
 * capability tiers exactly: Tier 0 and Tier 1 - the model, the map, routing, telemetry - do no I/O
 * and stay synchronous, and Tier 2 is async because it talks to a database. The reference
 * implementation is synchronous throughout because psycopg is.
 *
 * One consequence is worth naming, because requirement 17.4 asks for the overhead to be measured
 * per language and the note there said the "naive asynchronous wrapper" hazard had no subject yet:
 * it has one now. The measured budget still applies to the work this library adds - routing,
 * shaping, recording - which is synchronous and happens between two awaits.
 *
 * A session is opened rather than constructed, because the forward-only check reads a table and a
 * constructor cannot await. That is not a workaround: it means you cannot hold a session that has
 * not been checked, which is the same guarantee the reference gets from doing it in `__init__`.
 */

import { compareCodePoints } from './canonical.js'
import { EngineError, MigrationRefused, ModelPlanningError } from './errors.js'
import type { Group } from './groups.js'
import { colocationGroups, groupOf } from './groups.js'
import type { NameMap } from './hashing.js'
import { groupColumns } from './layout.js'
import { precisionRefusal } from './migration.js'
import type { LogicalModel } from './model.js'
import type { Materialization, PhysicalLayout, PlacementMap } from './placement.js'
import { placementOf } from './placement.js'
import { resolve } from './routing.js'
import { schemaIsFixed } from './schema.js'
import type { OperationShape, ShapeKind } from './shapes.js'
import { enumerateShapes, shapeId } from './shapes.js'
import type { Recorder } from './telemetry.js'
import type { WatermarkCheck } from './watermark.js'
import { enforceForwardOnly } from './watermark.js'

export type Row = Record<string, unknown>

/** What an adapter has to offer for a session to route to it. */
export interface Engine {
  readonly dialect: string
  ensureSchema(
    layout: PhysicalLayout,
    options: { readonly keys: Readonly<Record<string, readonly string[]>> },
  ): Promise<void>
  insert(table: string, values: Readonly<Row>): Promise<void>
  get(table: string, key: Readonly<Row>): Promise<Row | null>
  /**
   * One engine, one transaction, that engine's semantics.
   *
   * A callback rather than a pair of begin/commit calls, so a caller cannot leave one open. There
   * is no distributed transaction here and there will not be one: a client needing two entities to
   * commit together declares that, the planner puts them in the same group and therefore the same
   * engine, and the requirement turns into a placement constraint instead of a two-phase commit.
   */
  transaction<T>(body: () => Promise<T>): Promise<T>
}

export interface SessionOptions {
  /**
   * Telemetry is optional and off by default. A library that starts measuring the moment it is
   * imported is a library people are right to be suspicious of; measurement begins when a recorder
   * is handed in, which is a visible line in the client's code.
   */
  readonly recorder?: Recorder
  /** The name map from {@link hashIdentifiers}, when the client hashes their identifiers. */
  readonly names?: NameMap
}

interface Deferred {
  readonly engine: string
  readonly table: string
  readonly group: string
  readonly materialization: string
  readonly queuedNs: number
  readonly values: Row
}

function now(): number {
  return Number(process.hrtime.bigint())
}

/**
 * The separator for a composite lookup key, written as an escape rather than as the character.
 *
 * U+0000, for the reason section 2a of the contract picked it for hashed names: it cannot occur in
 * an identifier. Written `'\u0000'` and never as a literal byte, because a source file carrying a
 * raw control character is one that survives review, breaks a diff and can reach a published
 * package - which happened to this file twice while it was being written.
 */
const SEP = '\u0000'

/**
 * The index key for one shape: entity, kind and field list.
 *
 * Joined on {@link SEP} so that `['ab']` and `['a', 'b']` cannot collide. The empty string would
 * have made them the same shape - a range read over one field answering as a range read over two,
 * routed wherever the other one was routed.
 */
function shapeKey(entity: string, kind: string, fields: readonly string[]): string {
  return [entity, kind, ...fields].join(SEP)
}

export class Session {
  private readonly shapes = new Map<string, OperationShape>()
  private readonly groups: readonly Group[]
  private readonly reverseFields = new Map<string, Map<string, string>>()
  private readonly declared: readonly string[]
  private inWriteTransaction = false
  private deferred: Deferred[] = []

  private constructor(
    readonly model: LogicalModel,
    readonly placement: PlacementMap,
    private readonly engines: Readonly<Record<string, Engine>>,
    private readonly recorder: Recorder | undefined,
    private readonly names: NameMap | undefined,
    /**
     * Whether an older map could be loaded over this one, and why.
     *
     * Public because a protection whose state cannot be read is a protection taken on trust. It has
     * three values and the middle one matters: `enforced`, `unavailable` - no engine in this map can
     * keep the bookkeeping - and `not_applicable` for an unsigned map, which is the client's own
     * document.
     */
    readonly rollbackProtection: WatermarkCheck,
  ) {
    this.groups = colocationGroups(model)
    for (const shape of enumerateShapes(model)) {
      this.shapes.set(shapeKey(shape.entity, shape.kind, shape.fields), shape)
    }
    if (names !== undefined) {
      // The one place where the client's names and the hashed ones meet. When a model has been
      // hashed, everything downstream of here - the map, the shapes, the tables, the telemetry -
      // speaks digests only, and the application keeps saying save('User', { email: ... }).
      // Without this the mode is unusable: a client would have to write the digests in their own
      // source, which nobody will do and which would put them there anyway.
      for (const [entity, mapping] of Object.entries(names.fields)) {
        const hashedEntity = names.entities[entity]
        if (hashedEntity === undefined) continue
        const reverse = new Map<string, string>()
        for (const [original, hashed] of Object.entries(mapping)) reverse.set(hashed, original)
        this.reverseFields.set(hashedEntity, reverse)
      }
      this.declared = Object.keys(names.entities).sort(compareCodePoints)
    } else {
      this.declared = []
    }
  }

  /**
   * Open a session, refusing a map this set of engines cannot serve and one that goes backwards.
   *
   * Both checks are here rather than in a method somebody has to remember to call, and here rather
   * than in `ensureSchema`, which a deployment past its first release skips. A rolled-back map file
   * is read at process start, so the check has to be on the path every start takes.
   */
  static async open(
    model: LogicalModel,
    placement: PlacementMap,
    engines: Readonly<Record<string, Engine>>,
    options: SessionOptions = {},
  ): Promise<Session> {
    const required = new Set<string>()
    for (const group of Object.keys(placement.groups)) {
      const body = placementOf(placement, group)
      for (const materialization of [body.source, ...body.derived]) {
        required.add(materialization.engine)
      }
    }
    const missing = [...required].filter((name) => !(name in engines)).sort(compareCodePoints)
    if (missing.length > 0) {
      throw new EngineError(
        `the placement map refers to engines that were not supplied: [${missing.join(', ')}]. A ` +
          `session cannot route an operation to an engine it has no adapter for, and guessing at ` +
          `a connection is not something a library should do.`,
      )
    }
    // A copy that silently holds different values from its source is refused before the first
    // write rather than discovered by verify at the end of one. The rule is backfill's and it
    // lives in one function, because this is the second door asking it and for a long time only
    // the first one did: measured against live servers, a `timestamptz` written through a fan-out
    // map came back `09:30:15.123456` from PostgreSQL and `09:30:15.123` from ClickHouse, with no
    // error anywhere and backfill refusing the very same copy a phase later. Here rather than at
    // loadMap, and that is forced: a map names engines by name and carries no dialect, so the
    // earliest moment this is answerable is the one where the adapters are in hand.
    for (const group of colocationGroups(model)) {
      const body = placement.groups[group.name] === undefined ? null : placementOf(placement, group.name)
      if (body === null || body.alsoWrite.length === 0) continue
      const columns = groupColumns(model, group)
      const sourceDialect = (engines[body.source.engine] as Engine).dialect
      for (const copy of body.alsoWrite) {
        for (const entity of Object.keys(columns).sort(compareCodePoints)) {
          const refusal = precisionRefusal(
            group.name,
            entity,
            columns[entity] ?? {},
            sourceDialect,
            (engines[copy.engine] as Engine).dialect,
          )
          if (refusal !== null) {
            throw new MigrationRefused(
              `${refusal} This map fans writes out to ${copy.engine}, so it would happen on every ` +
                `write rather than once during a copy, and nothing would report it.`,
            )
          }
        }
      }
    }

    // It costs one statement per participating engine, once per process, and nothing at all for an
    // unsigned map - which is checked inside rather than here, because gathering the watermarks
    // first and then noticing the map was unsigned is the right answer with the promise broken.
    const protection = await enforceForwardOnly(placement, engines)
    return new Session(
      model,
      placement,
      engines,
      options.recorder,
      options.names,
      protection,
    )
  }

  // --- the hashing boundary ----------------------------------------------------------------
  //
  // Four helpers, each guarded by a single check, because with hashing off the cost of this whole
  // mechanism has to be one comparison on the hot path.

  /** The client's entity name, as the model knows it. */
  private entityName(entity: string): string {
    if (this.names === undefined) return entity
    return this.names.entities[entity] ?? entity
  }

  private fieldsIn(entity: string, values: Readonly<Row>): Row {
    if (this.names === undefined) return { ...values }
    const mapping = this.names.fields[entity] ?? {}
    const out: Row = {}
    for (const [name, value] of Object.entries(values)) out[mapping[name] ?? name] = value
    return out
  }

  private fieldsOut(entity: string, row: Row | null): Row | null {
    if (this.names === undefined || row === null) return row
    const reverse = this.reverseFields.get(this.entityName(entity))
    if (reverse === undefined) return row
    const out: Row = {}
    for (const [name, value] of Object.entries(row)) out[reverse.get(name) ?? name] = value
    return out
  }

  private clientNames(entity: string, fields: readonly string[]): string[] {
    if (this.names === undefined) return [...fields]
    const reverse = this.reverseFields.get(this.entityName(entity))
    if (reverse === undefined) return [...fields]
    return fields.map((name) => reverse.get(name) ?? name)
  }

  /**
   * The adapter registered under this name, refusing an unknown one.
   *
   * Exposed for the migration module, which needs all three of what a session holds and is
   * deliberately a set of free functions rather than methods here: a call that copies a table for
   * an hour has no business sitting in autocomplete next to `save`. Reaching into a private field
   * from a sibling module would have worked and would have made this class a friend of that one,
   * which is a worse arrangement than admitting what a session holds.
   */
  engineNamed(name: string): Engine {
    const engine = this.engines[name]
    if (engine === undefined) {
      throw new EngineError(
        `this session has no adapter named '${name}'. The map named it and the constructor ` +
          `refuses a map whose engines were not supplied, so this is a name from somewhere else.`,
      )
    }
    return engine
  }

  /** The adapters, by the names the map uses. A copy; the session keeps its own. */
  engineNames(): readonly string[] {
    return Object.keys(this.engines).sort(compareCodePoints)
  }

  groupOf(entity: string): Group {
    return groupOf(this.groups, this.entityName(entity))
  }

  private shapeFor(entity: string, kind: ShapeKind, fields: readonly string[] = []): OperationShape {
    const shape = this.shapes.get(shapeKey(entity, kind, fields))
    if (shape === undefined) {
      throw new ModelPlanningError(
        `this model has no ${kind} of ${entity}${
          fields.length > 0 ? ` over [${fields.join(', ')}]` : ''
        }. Operation shapes are enumerated from the model, so an operation that is not one of them ` +
          `is one the planner never routed and never scored.`,
      )
    }
    return shape
  }

  private target(shape: OperationShape, fresh: boolean): [Engine, Materialization] {
    const materialization = resolve(this.placement, shape, {
      inWriteTransaction: this.inWriteTransaction,
      fresh,
    })
    const engine = this.engines[materialization.engine]
    if (engine === undefined) {
      throw new EngineError(`no adapter for engine '${materialization.engine}'`)
    }
    return [engine, materialization]
  }

  // --- schema ------------------------------------------------------------------------------

  /** Create what each engine is missing for the groups placed in it. */
  async ensureSchema(): Promise<void> {
    for (const group of this.groups) {
      const body = placementOf(this.placement, group.name)
      const keys: Record<string, readonly string[]> = {}
      for (const member of group.members) {
        keys[member] = this.model.entities.find((entity) => entity.name === member)?.key ?? []
      }
      for (const materialization of [body.source, ...body.derived]) {
        const engine = this.engines[materialization.engine]
        if (engine === undefined) continue
        await engine.ensureSchema(materialization.layout, { keys })
      }
    }
  }

  /** Whether an engine in this map imposes its own schema, so `ensureSchema` sends it nothing. */
  fixedSchemaEngines(): readonly string[] {
    return [...new Set(Object.values(this.engines).filter((e) => schemaIsFixed(e.dialect)).map((e) => e.dialect))].sort(
      compareCodePoints,
    )
  }

  // --- data --------------------------------------------------------------------------------

  async save(entity: string, values: Readonly<Row>): Promise<void> {
    const target = this.entityName(entity)
    const body = this.fieldsIn(entity, values)
    const shape = this.shapeFor(target, 'write')
    const [engine, materialization] = this.target(shape, false)
    const table = tableFor(materialization.layout, target)
    const started = this.recorder === undefined ? 0 : now()
    let failed = false
    try {
      await engine.insert(table, body)
      await this.fanOut(target, shape.group, body)
    } catch (error) {
      failed = true
      throw error
    } finally {
      // Recorded on both paths on purpose: a failed write is exactly the operation whose latency
      // and error count matter most to a placement decision, and it is the one an early return
      // would silently omit. The fan-out is inside the timed region, which is a decision - it makes
      // the client's write slower and the telemetry has to say so, or a placement would be scored
      // against a latency the application is not experiencing.
      this.observe(shape, started, 1, failed)
    }
  }

  async get(entity: string, key: Readonly<Row>, options: { fresh?: boolean } = {}): Promise<Row | null> {
    const target = this.entityName(entity)
    const given = this.fieldsIn(entity, key)
    const spec = this.model.entities.find((e) => e.name === target)
    if (spec === undefined) throw new ModelPlanningError(`this model has no entity '${entity}'`)
    const expected = [...spec.key].sort(compareCodePoints)
    const supplied = Object.keys(given).sort(compareCodePoints)
    if (supplied.join(SEP) !== expected.join(SEP)) {
      // Both lists are put back into the client's vocabulary: with hashing on, an error naming
      // digests tells them nothing about their own code.
      throw new ModelPlanningError(
        `a point read of ${entity} needs exactly its key ` +
          `[${this.clientNames(entity, expected).join(', ')}], and was given ` +
          `[${this.clientNames(entity, supplied).join(', ')}]. A partial key is a range read, ` +
          `which is a different shape and may well be routed somewhere else.`,
      )
    }
    const shape = this.shapeFor(target, 'point_read', expected)
    const [engine, materialization] = this.target(shape, options.fresh === true)
    const table = tableFor(materialization.layout, target)
    const started = this.recorder === undefined ? 0 : now()
    let failed = false
    let row: Row | null = null
    try {
      row = await engine.get(table, given)
      return this.fieldsOut(entity, row)
    } catch (error) {
      failed = true
      throw error
    } finally {
      this.observe(shape, started, row === null ? 0 : 1, failed)
    }
  }

  // --- dual write --------------------------------------------------------------------------

  /**
   * Write the row to every `also_write` copy of the group. Additionally, never authoritatively.
   *
   * **A failure here does not interrupt the client's operation.** The row is in the source, which is
   * the copy that counts, and turning a migration into an application outage would make the safest
   * thing this product does the most dangerous. So the divergence is recorded and `verify` is the
   * gate that refuses to switch reads while any of them remain.
   *
   * Inside a write transaction the fan-out is **deferred to commit** rather than skipped or done
   * inline, and each of those three was considered. Inline is wrong: the target is a different
   * engine, so it is outside the source's transaction, and a rolled-back row would exist in the
   * copy - which after the switch is a row the client explicitly undid, readable. Skipping is wrong
   * for a quieter reason: those rows are above the backfill marker, so nothing else copies them,
   * and `verify`'s tail check would refuse the migration of every group that uses a transaction.
   */
  private async fanOut(entity: string, group: string, values: Row): Promise<void> {
    const body = placementOf(this.placement, group)
    if (body.alsoWrite.length === 0) return
    for (const copy of body.alsoWrite) {
      const table = tableFor(copy.layout, entity)
      const entry: Deferred = {
        engine: copy.engine,
        table,
        group,
        materialization: copy.id,
        queuedNs: now(),
        values: { ...values },
      }
      if (this.inWriteTransaction) {
        this.deferred.push(entry)
        continue
      }
      await this.replayOne(entry)
    }
  }

  /**
   * Write one row to one copy, measure how long the copy was behind, and never throw.
   *
   * `queuedNs` is when the row was handed to the fan-out, so the interval measured is the whole
   * time the copy did not have a row the source did. Outside a transaction that is the duration of
   * this write; inside one it also includes the rest of the transaction, which **overstates** the
   * staleness - the safe direction for a bound somebody checks a budget against.
   */
  private async replayOne(entry: Deferred): Promise<void> {
    let failed = false
    const engine = this.engines[entry.engine]
    try {
      if (engine === undefined) throw new EngineError(`no adapter for engine '${entry.engine}'`)
      await engine.insert(entry.table, entry.values)
    } catch {
      // Deliberately swallowed, and the only place in this library that swallows a write failure.
      // There is no logging channel here to record it in, which the reference has - so the
      // divergence reaches the caller only as a fan-out failure count, and `verify` is the gate
      // that acts on it.
      failed = true
    }
    this.recorder?.recordFanOut({
      group: entry.group,
      materialization: entry.materialization,
      nanoseconds: now() - entry.queuedNs,
      failed,
    })
  }

  private observe(shape: OperationShape, started: number, rows: number, failed: boolean): void {
    const recorder = this.recorder
    if (recorder === undefined) return
    recorder.record({
      shapeId: shapeId(shape),
      group: shape.group,
      entity: shape.entity,
      kind: shape.kind,
      nanoseconds: now() - started,
      rows,
      failed,
    })
  }

  // --- transactions ------------------------------------------------------------------------

  /**
   * Run `body` inside a transaction covering the given entities.
   *
   * They must share a colocation group, because a transaction is one engine's transaction. If they
   * do not, this throws before anything is opened and names the fix: declare the atomicity, and the
   * planner will colocate them.
   *
   * Called with no entities it covers the whole model, which is only legal when the model has one
   * group. That is not a convenience for small models so much as a refusal to let a two-group model
   * quietly get a transaction that only covers half of what the caller meant.
   */
  async transaction<T>(
    entities: readonly string[],
    body: (session: Session) => Promise<T>,
  ): Promise<T> {
    // Entity names in the *client's* vocabulary, so that an error message and the entities they
    // passed are in the same language. With hashing on, the model's entities are digests, and
    // feeding those back through `groupOf` would try to hash a hash.
    const declared =
      this.declared.length > 0 ? this.declared : this.model.entities.map((entity) => entity.name)
    const names = entities.length > 0 ? [...entities] : [...declared]
    const byGroup = new Map<string, string[]>()
    for (const name of [...names].sort(compareCodePoints)) {
      const group = this.groupOf(name).name
      byGroup.set(group, [...(byGroup.get(group) ?? []), name])
    }
    if (byGroup.size > 1) {
      const layout = [...byGroup.entries()]
        .sort(([a], [b]) => compareCodePoints(a, b))
        .map(([group, members]) => `${group}: ${members.join(', ')}`)
        .join('; ')
      throw new ModelPlanningError(
        `a transaction cannot span colocation groups (${layout}). One group is one engine and one ` +
          `engine's transaction; there is no distributed transaction here and there will not be ` +
          `one. If these entities have to commit together, declare it with atomicWith on either ` +
          `side, and the planner will place them in the same engine - which turns the requirement ` +
          `into a placement constraint instead of a two-phase commit.`,
      )
    }

    const first = names[0]
    if (first === undefined) throw new ModelPlanningError('a transaction needs an entity')
    const group = this.groupOf(first)
    const engineName = placementOf(this.placement, group.name).source.engine
    const engine = this.engines[engineName]
    if (engine === undefined) throw new EngineError(`no adapter for engine '${engineName}'`)

    const previous = this.inWriteTransaction
    const outer = this.deferred.length
    this.inWriteTransaction = true
    let committed = false
    try {
      const result = await engine.transaction(async () => body(this))
      committed = true
      return result
    } finally {
      this.inWriteTransaction = previous
      const pending = this.deferred.slice(outer)
      this.deferred.length = outer
      if (committed && !previous) {
        // Replayed after the source transaction has committed, and only by the outermost one: a
        // nested block that returns to a still-open transaction has not committed anything yet, so
        // its rows go back on the queue rather than to the copy.
        for (const entry of pending) await this.replayOne(entry)
      } else if (committed) {
        this.deferred.push(...pending)
      }
      // Rolled back: the rows never existed in the source, so they must never exist in the copy -
      // dropping them is the whole reason the fan-out was deferred.
    }
  }
}

/** The table this layout gives an entity, refusing a map that places a group it cannot name. */
export function tableFor(layout: PhysicalLayout, entity: string): string {
  const table = layout.tables[entity]
  if (table === undefined) {
    throw new EngineError(
      `the layout has no table for '${entity}'. The map claims to place a group that contains ` +
        `this entity, so this is a defect in the map rather than something the library can work ` +
        `around.`,
    )
  }
  return table
}

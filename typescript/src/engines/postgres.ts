/**
 * PostgreSQL adapter.
 *
 * Three rules run through everything here, and all three come from the requirements rather than
 * from taste.
 *
 * **A failed write is reported, never worked around.** No retry into another engine, no swallowing,
 * no "eventually consistent" story invented on the spot. If the source engine for a group will not
 * take the write, the client's code finds out. The library swallows its *own* internal problems -
 * routing, telemetry - because a bug of ours must not take down someone's application, but a write
 * that did not happen is not our internal problem and reporting success for it would be the worst
 * thing this library could do.
 *
 * **Identifiers are quoted, always.** Not for injection - identifiers come from the placement map,
 * not from user input - but because entity names may contain non-ASCII characters, and an unquoted
 * identifier in PostgreSQL is folded to lower case in a way that is lossy for some of them. The
 * quoting function is bound from `schema.ts` rather than written again, so DDL and DML cannot
 * disagree about how an identifier is escaped.
 *
 * **Values are converted per row, from the field types the server sent.** `pg` offers a global type
 * parser registry, and using it would be the shortest path and the wrong one: this library goes
 * into somebody else's application, so registering a parser would change how *their* other queries
 * come back. The reference implementation has no equivalent temptation, which is why the rule is
 * written down here.
 *
 * The driver is `pg`, and it is an **optional peer dependency**. A client who never places a group
 * in PostgreSQL should not have it in their tree, and a client who has it already should keep the
 * version they chose - the core of this library has no runtime dependencies at all, because every
 * one of them would be a dependency they inherit and a version conflict they may have to resolve.
 */

import { EngineError } from '../errors.js'
import type { PhysicalLayout } from '../placement.js'
import { BACKFILL_TABLE, WATERMARK_TABLE } from '../placement.js'
import { QUOTE, schemaStatements } from '../schema.js'
import type { Row } from '../session.js'
import { keyColumns, sameWidth } from '../migration.js'

// Bound from the one definition in schema.ts, so that DDL and DML cannot disagree about how an
// identifier is escaped.
const quote = QUOTE['postgres'] as (identifier: string) => string

/**
 * Milliseconds. Not a guess about networks: this bounds *opening* a connection, which either
 * completes in milliseconds on a healthy link or is not going to complete.
 *
 * Ten seconds leaves room for a saturated cross-region hop and still fails long before a request
 * timeout a caller sets. It is a **default** rather than a rule - a `connect_timeout` in the DSN
 * wins - and it exists because the alternative, measured against a socket that accepts TCP and says
 * nothing, is a call that never returns. Queries are deliberately not bounded: an analytical query
 * legitimately takes minutes and a library that timed it out would be deciding something about the
 * caller's workload.
 */
export const CONNECT_TIMEOUT_MS = 10_000

/** Type OIDs this adapter converts, and what it converts them to. See the module docstring. */
const OID = {
  bool: 16,
  bytea: 17,
  int8: 20,
  int2: 21,
  int4: 23,
  json: 114,
  float4: 700,
  float8: 701,
  date: 1082,
  timestamp: 1114,
  timestamptz: 1184,
  numeric: 1700,
  uuid: 2950,
  jsonb: 3802,
} as const

/**
 * One value, converted from what `pg` hands back into what this library promises.
 *
 * Four of these are decisions rather than plumbing, and each is a place where two engines would
 * otherwise disagree about the same column.
 *
 * **`int8` becomes a `bigint`.** `pg` returns it as a string, and a `number` would silently lose
 * precision above 2^53 - which is not exotic for an identifier column. A `bigint` is the only
 * JavaScript type that holds an int64, so it is the one used, and the ClickHouse adapter agrees.
 *
 * **`numeric` stays a string.** There is no exact decimal in this runtime, and turning `12.34` into
 * a float is the one conversion this library must never do quietly: the value comes back changed and
 * nothing raises. The reference returns `Decimal`; a string is this language's nearest honest
 * equivalent, and it round-trips.
 *
 * **`date` stays a string.** `pg` parses it into a `Date`, which is midnight in *this process's*
 * timezone - so the same stored date reads as a different day depending on where the reader runs.
 * A calendar date has no time and no zone; `YYYY-MM-DD` says exactly that.
 *
 * **`timestamptz` becomes a `Date`.** That one is unambiguous: it is an instant, `pg` already
 * parses it, and the ClickHouse adapter parses its own string form into the same thing.
 */
function convert(value: unknown, oid: number): unknown {
  if (value === null || value === undefined) return null
  switch (oid) {
    case OID.int8:
      return typeof value === 'bigint' ? value : BigInt(String(value))
    case OID.numeric:
      return String(value)
    case OID.date:
      // `pg` has already parsed it into a local-midnight Date; render the calendar date back out of
      // the parts that do not depend on a zone.
      return value instanceof Date ? isoDate(value) : String(value)
    case OID.timestamp:
    case OID.timestamptz:
      return value instanceof Date ? value : new Date(String(value))
    default:
      return value
  }
}

function isoDate(value: Date): string {
  const year = String(value.getFullYear()).padStart(4, '0')
  const month = String(value.getMonth() + 1).padStart(2, '0')
  const day = String(value.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

/**
 * One value on the way *in*.
 *
 * A `bigint` is handed to `pg` as a string, because the driver has no encoder for it and would
 * otherwise throw. A `Date` and everything else go through untouched: `pg` sends a timestamp as UTC,
 * which is what the ClickHouse adapter does explicitly - and that divergence, measured in the
 * reference implementation, was two hours wide with no error anywhere.
 */
function outbound(value: unknown): unknown {
  if (typeof value === 'bigint') return value.toString()
  return value
}

interface QueryResultLike {
  readonly rows: readonly Record<string, unknown>[]
  readonly fields: readonly { readonly name: string; readonly dataTypeID: number }[]
}

interface ClientLike {
  connect(): Promise<void>
  end(): Promise<void>
  query(text: string, values?: readonly unknown[]): Promise<QueryResultLike>
  on(event: 'error', listener: (error: Error) => void): unknown
}

export interface PostgresOptions {
  /**
   * The `pg` module, for a caller who has it under a different name or wants to hand in a double.
   *
   * Loaded with a dynamic import when absent, which is what keeps it an optional dependency: a
   * client who never places a group in PostgreSQL never resolves it.
   */
  readonly driver?: unknown
}

export class PostgresEngine {
  readonly dialect = 'postgres'
  private client: ClientLike | null = null
  /**
   * The asynchronous failure the driver reported, if any, kept for the next call to explain.
   *
   * **This exists because an unhandled `'error'` event kills the client's process.** `pg.Client` is
   * an `EventEmitter`, and it emits `'error'` when the server terminates the connection between
   * queries - a restart, a failover, an administrator. Node's rule for an `'error'` event with no
   * listener is to throw it, so a library that did not listen would turn a database restart into
   * `Error: Connection terminated unexpectedly` from inside somebody's event loop, with no call of
   * theirs on the stack and nothing to catch it.
   *
   * Found by the test that measures the *message* after a cut connection: the assertion failed and
   * took eight unrelated tests with it, which is a mild version of what a client would have seen.
   * The reference implementation has no equivalent hazard - psycopg raises at the next call and
   * emits nothing - so this is the one place where "same contract, different runtime" means extra
   * code rather than a translation.
   */
  private lost: Error | null = null

  constructor(
    private readonly dsn: string,
    private readonly options: PostgresOptions = {},
  ) {}

  /**
   * Open the connection, with a bound on how long that may take.
   *
   * Measured in the reference implementation before this bound existed: a host that accepts the TCP
   * connection and never answers hung the call **for as long as the test was willing to wait**.
   * That is not an exotic case - it is a firewall that accepts, a load balancer with no healthy
   * backend, a server mid-restart - and without a bound it happens inside the caller's request path
   * with nothing to time out.
   *
   * The default is only applied when the caller has not chosen one. A `connect_timeout` in the DSN
   * is their decision about their own network and this must not override it - see
   * {@link connectBound}, where honouring it costs more work here than it does in the reference.
   */
  async connect(): Promise<void> {
    if (this.client !== null) return
    const driver = (this.options.driver ?? (await loadDriver())) as {
      Client: new (config: Record<string, unknown>) => ClientLike
    }
    const config: Record<string, unknown> = {
      connectionString: this.dsn,
      connectionTimeoutMillis: connectBound(this.dsn),
    }
    const client = new driver.Client(config)
    // Attached before `connect`, because the window between them is one a server can fail in.
    client.on('error', (error: Error) => {
      this.lost = error
    })
    try {
      await client.connect()
    } catch (error) {
      throw new EngineError(`could not connect to PostgreSQL: ${message(error)}`)
    }
    this.lost = null
    this.client = client
  }

  async close(): Promise<void> {
    if (this.client === null) return
    const client = this.client
    this.client = null
    await client.end()
  }

  private get cx(): ClientLike {
    if (this.client === null) throw new EngineError('not connected; call connect() first')
    return this.client
  }

  /**
   * The driver's message, plus the one sentence it cannot know to add.
   *
   * Measured in the reference: cut the connection under a live session and the first failing call
   * reports what the server said, which is right. **Every call after it reports only that the
   * connection is closed** - true, unhelpful, and the point at which a reader needs to be told that
   * this library holds the connection it was handed and does not reopen it. Reconnecting is one line
   * and it is the caller's, because a library that silently reconnected would also silently retry.
   */
  private explain(error: unknown): string {
    const text = message(error)
    const gone =
      this.lost !== null || /connection.*(closed|terminated|ended)|not queryable/i.test(text)
    if (gone) {
      return (
        `${text}. The connection is gone and this library does not reopen one it was handed: ` +
        `call close() then connect() on the engine, or hand the session a new one. Nothing was ` +
        `retried, so no write reached the engine twice.`
      )
    }
    return text
  }

  private async run(sql: string, values: readonly unknown[] = []): Promise<QueryResultLike> {
    return this.cx.query(sql, values.map(outbound))
  }

  private rowsOf(result: QueryResultLike): Row[] {
    const oids = new Map(result.fields.map((field) => [field.name, field.dataTypeID]))
    return result.rows.map((row) => {
      const out: Row = {}
      for (const [name, value] of Object.entries(row)) {
        out[name] = convert(value, oids.get(name) ?? -1)
      }
      return out
    })
  }

  // --- schema ------------------------------------------------------------------------------

  /**
   * Create what is missing, change nothing that exists.
   *
   * Idempotent on purpose: an application restarting must not reapply DDL, and two instances
   * starting at once must not race. Anything beyond creation - altering a column, dropping an
   * index - is a migration, which is the orchestrator's job and carries a safety classification.
   */
  async ensureSchema(
    layout: PhysicalLayout,
    options: { readonly keys: Readonly<Record<string, readonly string[]>> },
  ): Promise<void> {
    const statements = schemaStatements(layout, { keys: options.keys, dialect: this.dialect })
    for (const statement of statements) {
      try {
        await this.run(statement)
      } catch (error) {
        throw new EngineError(`schema statement failed: ${statement}: ${message(error)}`)
      }
    }
    await this.verifySchema(layout)
  }

  /**
   * Check that what exists is what the map describes, because `IF NOT EXISTS` does not.
   *
   * `CREATE TABLE IF NOT EXISTS` accepts a table of that name whatever shape it is in, so a table
   * left over from something else - an older map, another application, a migration run by hand - is
   * silently kept and the first insert fails with `column "at" does not exist`. That error names a
   * column and not the cause, and it arrives in the client's request path rather than at startup.
   *
   * A **missing** column is refused: writes through this map cannot work. An **extra** column is
   * allowed - a client may have added one outside SDE, the map does not name it, writes are
   * unaffected, and refusing would make this library an obstacle to work it has no opinion about.
   *
   * Names only. Comparing declared types to `information_schema.data_type` means matching
   * `numeric(8,2)` against `numeric`, and a check that has to normalise dialect spellings would
   * report differences that are not differences.
   */
  private async verifySchema(layout: PhysicalLayout): Promise<void> {
    const expected = new Map<string, Set<string>>()
    for (const [entity, table] of Object.entries(layout.tables)) {
      expected.set(table, new Set(Object.keys(layout.columns[entity] ?? {})))
    }
    if (expected.size === 0) return

    const result = await this.run(
      'SELECT table_name, column_name FROM information_schema.columns ' +
        'WHERE table_schema = current_schema() AND table_name = ANY($1)',
      [[...expected.keys()].sort()],
    )
    const found = new Map<string, Set<string>>()
    for (const row of result.rows) {
      const table = String(row['table_name'])
      const columns = found.get(table) ?? new Set<string>()
      columns.add(String(row['column_name']))
      found.set(table, columns)
    }

    for (const [table, columns] of [...expected.entries()].sort()) {
      const actual = found.get(table)
      if (actual === undefined) {
        throw new EngineError(
          `'${table}' does not exist after applying the schema. The statement reported success, ` +
            `so this is a permissions or search_path problem rather than a bad map.`,
        )
      }
      const missing = [...columns].filter((column) => !actual.has(column)).sort()
      if (missing.length > 0) {
        throw new EngineError(
          `'${table}' already existed with a different shape: the map needs ` +
            `[${missing.join(', ')}] and the table has [${[...actual].sort().join(', ')}]. ` +
            `CREATE TABLE IF NOT EXISTS keeps whatever is there, so this table came from ` +
            `somewhere else - an older map, another application, a migration run by hand. ` +
            `Refusing here rather than at the first insert, which would fail in your request path ` +
            `with an error naming a column and not the cause.`,
        )
      }
    }
  }

  // --- data --------------------------------------------------------------------------------

  async insert(table: string, values: Readonly<Row>): Promise<void> {
    const columns = Object.keys(values).sort()
    if (columns.length === 0) throw new EngineError('nothing to insert')
    const placeholders = columns.map((_, index) => `$${index + 1}`).join(', ')
    const sql =
      `INSERT INTO ${quote(table)} (${columns.map(quote).join(', ')}) VALUES (${placeholders})`
    try {
      await this.run(
        sql,
        columns.map((column) => values[column]),
      )
    } catch (error) {
      // Surfaced, not swallowed and not rerouted. See the module docstring.
      throw new EngineError(`insert into ${table} failed: ${this.explain(error)}`)
    }
  }

  async get(table: string, key: Readonly<Row>): Promise<Row | null> {
    const columns = Object.keys(key).sort()
    const where = columns.map((column, index) => `${quote(column)} = $${index + 1}`).join(' AND ')
    try {
      const result = await this.run(
        `SELECT * FROM ${quote(table)} WHERE ${where}`,
        columns.map((column) => key[column]),
      )
      const rows = this.rowsOf(result)
      return rows[0] ?? null
    } catch (error) {
      throw new EngineError(`select from ${table} failed: ${this.explain(error)}`)
    }
  }

  async count(table: string): Promise<number> {
    try {
      const result = await this.run(`SELECT count(*) AS n FROM ${quote(table)}`)
      return Number(result.rows[0]?.['n'] ?? 0)
    } catch (error) {
      throw new EngineError(`count on ${table} failed: ${message(error)}`)
    }
  }

  // --- rollback protection ------------------------------------------------------------------

  /**
   * The highest map version applied against this engine, creating the table if missing.
   *
   * Creating on read rather than on write, and that closes a real gap: with the table appearing
   * only on the first write, the first load of a signed map has nothing to compare against and a
   * file swapped immediately after a deployment goes unnoticed.
   */
  async mapWatermark(): Promise<number | null> {
    try {
      await this.run(
        `CREATE TABLE IF NOT EXISTS ${quote(WATERMARK_TABLE)} (` +
          `${quote('map_version')} bigint NOT NULL, ` +
          `${quote('model_version')} text NOT NULL, ` +
          `${quote('seen_at')} timestamptz NOT NULL DEFAULT now())`,
      )
      const result = await this.run(
        `SELECT max(${quote('map_version')}) AS high FROM ${quote(WATERMARK_TABLE)}`,
      )
      const high = result.rows[0]?.['high']
      return high === null || high === undefined ? null : Number(high)
    } catch (error) {
      throw new EngineError(`reading ${WATERMARK_TABLE} failed: ${this.explain(error)}`)
    }
  }

  /**
   * Append. Never update, so there is nothing to contend over and nothing to lose.
   *
   * The timestamp comes from the engine's own `now()` rather than from this process: an audit column
   * wants the clock of the thing being audited, and this library reading a clock is a thing its
   * tests would then have to work around.
   */
  async recordMapVersion(version: number, options: { readonly modelVersion: string }): Promise<void> {
    try {
      await this.run(
        `INSERT INTO ${quote(WATERMARK_TABLE)} (${quote('map_version')}, ` +
          `${quote('model_version')}) VALUES ($1, $2)`,
        [version, options.modelVersion],
      )
    } catch (error) {
      throw new EngineError(
        `recording a map version in ${WATERMARK_TABLE} failed: ${message(error)}`,
      )
    }
  }

  // --- migration ---------------------------------------------------------------------------

  /**
   * Rows in key order, strictly after one key and up to another inclusive.
   *
   * Row-value comparison - `(a, b) > ($1, $2)` - rather than a hand-rolled disjunction over the
   * key's columns. The disjunction is where composite-key pagination goes wrong, and it goes wrong
   * by skipping rows.
   *
   * The bounds are asymmetric on purpose. `after` is exclusive because it is a resume point: the row
   * it names has been dealt with. `upto` is inclusive because it names the last row of a chunk read
   * from somewhere else, and that row is one this range has to include.
   */
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
    const clauses: string[] = []
    const values: unknown[] = []
    const tuple = `(${cols.map(quote).join(', ')})`
    if (options.after !== undefined) {
      sameWidth(options.after, cols, 'after')
      clauses.push(`${tuple} > (${cols.map((_, i) => `$${values.length + i + 1}`).join(', ')})`)
      values.push(...options.after)
    }
    if (options.upto !== undefined) {
      sameWidth(options.upto, cols, 'upto')
      clauses.push(`${tuple} <= (${cols.map((_, i) => `$${values.length + i + 1}`).join(', ')})`)
      values.push(...options.upto)
    }
    const where = clauses.length > 0 ? ` WHERE ${clauses.join(' AND ')}` : ''
    let cap = ''
    if (options.limit !== undefined) {
      values.push(options.limit)
      cap = ` LIMIT $${values.length}`
    }
    try {
      const result = await this.run(
        `SELECT * FROM ${quote(table)}${where} ORDER BY ${tuple}${cap}`,
        values,
      )
      return this.rowsOf(result)
    } catch (error) {
      throw new EngineError(`key range select from ${table} failed: ${message(error)}`)
    }
  }

  /**
   * The key of the `position`-th row in key order, one-based, or null if there is no such row.
   *
   * An `OFFSET` scan, which is the expensive kind of query, and it is here because it is paid **once
   * per resume** rather than once per chunk. See `migration.ts` for why the marker is a row count
   * and not a key.
   */
  async nthKey(
    table: string,
    order: readonly string[],
    options: { readonly position: number },
  ): Promise<unknown[] | null> {
    const cols = keyColumns(order, table)
    if (options.position < 1) {
      throw new EngineError(`position is one-based; ${options.position} is not a row`)
    }
    const projection = cols.map(quote).join(', ')
    try {
      const result = await this.run(
        `SELECT ${projection} FROM ${quote(table)} ORDER BY (${projection}) OFFSET $1 LIMIT 1`,
        [options.position - 1],
      )
      const rows = this.rowsOf(result)
      const row = rows[0]
      if (row === undefined) return null
      return cols.map((column) => row[column])
    } catch (error) {
      throw new EngineError(`reading row ${options.position} of ${table} failed: ${message(error)}`)
    }
  }

  /**
   * Insert rows, skipping any whose key is already there.
   *
   * `ON CONFLICT DO NOTHING` is what makes a backfill chunk **idempotent**, and idempotence is what
   * makes it resumable: the marker is written after the chunk, so a crash in between costs a recopy
   * and never a lost row. Without it the recopy would be a primary-key violation and the safe
   * failure mode would become the loud one.
   *
   * The bare form, with no conflict target, so it covers the primary key and any unique index the
   * layout asked for. Naming the key here would mean deriving it a second time.
   */
  async copyIn(table: string, rows: readonly Row[]): Promise<void> {
    if (rows.length === 0) return
    const columns = Object.keys(rows[0] as Row).sort()
    for (const row of rows) {
      const here = Object.keys(row).sort()
      if (here.join(' ') !== columns.join(' ')) {
        throw new EngineError(
          `copyIn into ${table} was given rows with different columns ([${columns.join(', ')}] ` +
            `and [${here.join(', ')}]). A chunk comes from one table, so this is a caller ` +
            `assembling it from two.`,
        )
      }
    }
    const values: unknown[] = []
    const tuples = rows.map((row) => {
      const placeholders = columns.map((column) => {
        values.push(row[column])
        return `$${values.length}`
      })
      return `(${placeholders.join(', ')})`
    })
    try {
      await this.run(
        `INSERT INTO ${quote(table)} (${columns.map(quote).join(', ')}) ` +
          `VALUES ${tuples.join(', ')} ON CONFLICT DO NOTHING`,
        values,
      )
    } catch (error) {
      throw new EngineError(`copying ${rows.length} rows into ${table} failed: ${message(error)}`)
    }
  }

  /**
   * How many rows of this entity have been copied into this engine. Zero if none.
   *
   * `max()` over an append-only table, exactly like the map watermark, and for the same reason: no
   * row to update, nothing to contend over, and identical semantics in an engine with no unique
   * constraint. A stale row can never lower the marker.
   */
  async backfillMarker(options: {
    readonly materialization: string
    readonly entity: string
  }): Promise<number> {
    try {
      await this.run(
        `CREATE TABLE IF NOT EXISTS ${quote(BACKFILL_TABLE)} (` +
          `${quote('materialization')} text NOT NULL, ` +
          `${quote('entity')} text NOT NULL, ` +
          `${quote('rows_copied')} bigint NOT NULL, ` +
          `${quote('at')} timestamptz NOT NULL DEFAULT now())`,
      )
      const result = await this.run(
        `SELECT max(${quote('rows_copied')}) AS high FROM ${quote(BACKFILL_TABLE)} ` +
          `WHERE ${quote('materialization')} = $1 AND ${quote('entity')} = $2`,
        [options.materialization, options.entity],
      )
      const high = result.rows[0]?.['high']
      return high === null || high === undefined ? 0 : Number(high)
    } catch (error) {
      throw new EngineError(`reading ${BACKFILL_TABLE} failed: ${message(error)}`)
    }
  }

  /** Append the new marker. Never update, so an interrupted run leaves a readable trail. */
  async recordBackfillMarker(options: {
    readonly materialization: string
    readonly entity: string
    readonly rows: number
  }): Promise<void> {
    try {
      await this.run(
        `INSERT INTO ${quote(BACKFILL_TABLE)} (${quote('materialization')}, ${quote('entity')}, ` +
          `${quote('rows_copied')}) VALUES ($1, $2, $3)`,
        [options.materialization, options.entity, options.rows],
      )
    } catch (error) {
      throw new EngineError(
        `recording backfill progress in ${BACKFILL_TABLE} failed: ${message(error)}`,
      )
    }
  }

  // --- transactions ------------------------------------------------------------------------

  /**
   * One engine, one transaction, that engine's semantics.
   *
   * There is no distributed transaction here and there will not be one. A client needing two
   * entities to commit together declares that, and the planner puts them in the same group and
   * therefore the same engine - so the requirement turns into a placement constraint instead of a
   * two-phase commit. That is the trade this product makes, and it is why this method is a dozen
   * lines rather than a subsystem.
   */
  async transaction<T>(body: () => Promise<T>): Promise<T> {
    await this.run('BEGIN')
    try {
      const result = await body()
      await this.run('COMMIT')
      return result
    } catch (error) {
      try {
        await this.run('ROLLBACK')
      } catch {
        // The rollback failing means the connection is gone, which the original error already says.
        // Reporting this one instead would replace the reason with a symptom.
      }
      throw error
    }
  }
}

/**
 * The bound to open a connection with: the caller's, in milliseconds, or this adapter's default.
 *
 * **`pg` does not read `connect_timeout` from a connection string, and finding that out is the
 * whole reason this function exists.** `pg-connection-string` parses the parameter into a
 * `connect_timeout` property, and `pg` then *overwrites* that property from
 * `connectionTimeoutMillis` - so the value a caller wrote into their DSN reaches the JavaScript
 * client and is discarded. Measured, because it is not the kind of thing a signature admits.
 *
 * The first version of this adapter did what the reference does: apply a default unless the DSN
 * already says `connect_timeout`. In psycopg that is right, because the parameter goes through to
 * libpq and libpq honours it. Here it produced the worst of both - the caller's value ignored by
 * the driver and this adapter's default suppressed by the caller's value - so a DSN that asked for
 * a two-second bound got **no bound at all**, which is the case the bound exists for. Found by the
 * test that asserts the caller's value wins, which failed by hanging until its own deadline fired.
 *
 * So the parameter is translated rather than deferred to. The caller's decision is honoured and a
 * bound always exists, which are the two properties that were meant to hold in the first place.
 */
export function connectBound(dsn: string): number {
  const declared = /[?&]connect_timeout=(\d+)/.exec(dsn)
  if (declared === null) return CONNECT_TIMEOUT_MS
  const seconds = Number(declared[1])
  // Zero means "wait forever" in libpq, and a caller who wrote that has said something explicit
  // about their own network. Passed through as `pg`'s own spelling of no timeout.
  return seconds === 0 ? 0 : seconds * 1000
}


async function loadDriver(): Promise<unknown> {
  try {
    return await import('pg')
  } catch {
    throw new EngineError(
      "the PostgreSQL adapter needs the 'pg' package: npm install pg. The core library has no " +
        'dependencies, because it goes into your application and every dependency here would be ' +
        'one you inherit.',
    )
  }
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

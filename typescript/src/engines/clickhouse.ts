/**
 * ClickHouse adapter, over the HTTP interface, with no driver dependency at all.
 *
 * **Why this one is written and the PostgreSQL one is not.** There is an official Node client and
 * the reference implementation uses the official Python one, so the symmetry would have been to
 * take it. Measured instead of assumed: `@clickhouse/client` 1.23 requires **Node 20 or newer**,
 * and this package declares Node 18 to 22 and its CI runs all three. The two ways out were to drop
 * Node 18 - narrowing a published claim to gain a dependency - or to pin a superseded version of
 * the client, which is the version conflict the zero-dependency rule exists to avoid. So neither.
 * ClickHouse's HTTP interface needs no client: this runtime has one.
 *
 * Two consequences are worth stating rather than discovering. This adapter owns **both** timeouts
 * instead of inheriting a driver's defaults, which is how the reference implementation found that
 * its ClickHouse `connect_timeout` never fired - the TCP connect succeeds, so what was left was a
 * read timeout whose driver default is 300 seconds. And every type conversion here is ours, which
 * is not extra work: it was already ours in the reference, because a driver's idea of what a
 * `DateTime64` differs from the next driver's.
 *
 * Three things here are genuinely different from the PostgreSQL adapter, and none of them is a
 * detail. Each is a place where "the same operation" means something else because the engine is
 * different, and the product's position is that such a difference must be *stated* rather than
 * smoothed over.
 *
 * **A naive datetime is UTC. Always, explicitly, here.** A `Date` in this runtime is an instant, so
 * there is no naive case to get wrong on the way in - but there is on the way *out*: ClickHouse
 * returns `DateTime64(6, 'UTC')` as `2026-08-27 12:00:00.123456`, with no zone in the text. Read as
 * local time that is a different instant on every machine, and after a migration between engines
 * every timestamp in the client's analytics would shift by the offset of whichever host happened to
 * write the row. The reference measured that divergence at two hours with no error anywhere.
 *
 * **There are no transactions, and this adapter says so instead of pretending.** `transaction()`
 * throws. Not a no-op that runs the body: a caller who believes they are in a transaction and is
 * not has been lied to at the worst possible moment.
 *
 * **Keys are not enforced, so the table is a `ReplacingMergeTree` and point reads use `FINAL`.**
 * A MergeTree does not enforce uniqueness on its `ORDER BY`, so a second write of the same key does
 * not raise the way a primary key does - and that divergence cannot be removed, only chosen. Plain
 * `MergeTree` would leave two rows and make every aggregate over that group quietly wrong;
 * `ReplacingMergeTree` keeps the newest and collapses the rest at merge time. `FINAL` on point reads
 * and counts is what makes the collapse visible immediately rather than eventually, and it is not
 * free - which is the trade, stated.
 */

import { request as httpRequest } from 'node:http'
import { request as httpsRequest } from 'node:https'

import { compareCodePoints } from '../canonical.js'
import { EngineError } from '../errors.js'
import { Timestamp } from '../timestamp.js'
import { keyColumns, sameWidth } from '../migration.js'
import type { PhysicalLayout } from '../placement.js'
import { BACKFILL_TABLE, WATERMARK_TABLE } from '../placement.js'
import { QUOTE, schemaStatements } from '../schema.js'
import type { Row } from '../session.js'

const quote = QUOTE['clickhouse'] as (identifier: string) => string

/** Milliseconds, and the same argument as the PostgreSQL adapter's constant of the same name. */
export const CONNECT_TIMEOUT_MS = 10_000

/**
 * Milliseconds, for the version exchange `connect()` performs.
 *
 * Set to something different from the connect bound on purpose. **Opening is bounded tightly;
 * reading is not, past this exchange** - an analytical query legitimately takes minutes and a
 * library that timed it out would be deciding something about the caller's workload. The handshake
 * is the one exchange this library knows the shape of.
 *
 * The reference implementation reaches the same place through its driver's `send_receive_timeout`,
 * and that is measured rather than assumed: with that bound at 15 seconds, a 23-second and a
 * 24-second query both returned normally, so the bound does not cut a slow query. Here the
 * distinction is structural instead - only the handshake request carries a timeout - which is one
 * fewer thing to be right about.
 */
export const HANDSHAKE_TIMEOUT_MS = 15_000

interface Parsed {
  readonly protocol: 'http:' | 'https:'
  readonly host: string
  readonly port: number
  readonly user: string
  readonly password: string
  readonly database: string
}

/**
 * A `clickhouse://user:password@host:port/database` DSN, as the reference implementation's driver
 * takes it, so one client can configure two languages from one string.
 */
function parse(dsn: string): Parsed {
  let url: URL
  try {
    url = new URL(dsn)
  } catch {
    throw new EngineError(
      `'${dsn}' is not a ClickHouse DSN. The form is ` +
        `clickhouse://user:password@host:port/database, which is what the reference ` +
        `implementation's driver takes - so one string configures both languages.`,
    )
  }
  const secure = url.protocol === 'clickhouses:' || url.protocol === 'https:'
  const database = url.pathname.replace(/^\//, '')
  return {
    protocol: secure ? 'https:' : 'http:',
    host: url.hostname,
    port: url.port === '' ? (secure ? 8443 : 8123) : Number(url.port),
    user: decodeURIComponent(url.username) || 'default',
    password: decodeURIComponent(url.password),
    database: database === '' ? 'default' : database,
  }
}

interface Meta {
  readonly name: string
  readonly type: string
}

interface JsonResult {
  readonly meta?: readonly Meta[]
  readonly data?: readonly Record<string, unknown>[]
}

/**
 * One value, converted from ClickHouse's JSON text into what this library promises, using the
 * column type the server reported.
 *
 * The type comes from the response's own `meta` block rather than from the layout, for the reason
 * the PostgreSQL adapter reads field OIDs: the server is the authority on what it stored, and a map
 * can be stale in a way a response cannot.
 *
 * Four conversions, and each one is the same decision the PostgreSQL adapter makes, arrived at from
 * the other side - which is what makes the two agree.
 */
function convert(value: unknown, type: string): unknown {
  if (value === null || value === undefined) return null
  const bare = type.replace(/^Nullable\((.*)\)$/, '$1').replace(/^LowCardinality\((.*)\)$/, '$1')
  if (/^U?Int(64|128|256)/.test(bare)) {
    // A string in ClickHouse's JSON, because a JSON number cannot hold an int64 - and a `number`
    // here would lose precision above 2^53, which is not exotic for an identifier column.
    return BigInt(String(value))
  }
  if (bare.startsWith('Decimal')) {
    // No exact decimal in this runtime, and turning `12.34` into a float is the one conversion this
    // library must never do quietly: the value comes back changed and nothing raises.
    return String(value)
  }
  if (bare.startsWith('DateTime')) {
    // `2026-08-27 12:00:00.000`, with no zone in the text. Read as local time it is a different
    // instant on every machine, so the zone is supplied here - and it is UTC because that is what
    // the column declares and what the PostgreSQL side stores.
    return Timestamp.from(String(value))
  }
  if (bare.startsWith('Date')) {
    // A calendar date has no time and no zone. `YYYY-MM-DD` says exactly that, and the PostgreSQL
    // adapter renders its own `date` back to the same form for the same reason.
    return String(value)
  }
  if (bare.startsWith('Bool')) return Boolean(value)
  return value
}

/** One value on the way *in*, as a JSON-safe form ClickHouse will accept. */
function outbound(value: unknown): unknown {
  if (typeof value === 'bigint') return value.toString()
  if (value instanceof Timestamp || value instanceof Date) {
    // Date remains a valid millisecond-resolution input; Timestamp retains six digits.
    // Both serialize UTC parts, independently of the process timezone.
    return value.toISOString().replace('T', ' ').replace('Z', '')
  }
  return value
}

export class ClickHouseEngine {
  readonly dialect = 'clickhouse'
  private readonly target: Parsed
  private open = false
  private serverVersion: string | null = null

  constructor(dsn: string) {
    this.target = parse(dsn)
  }

  /**
   * Ask the server what it is, with a bound on how long that may take.
   *
   * The equivalent of the reference implementation's driver handshake, and it exists for the same
   * measurement: a host that accepts the TCP connection and never answers hung a call for as long
   * as the test would wait. Here the reason is one layer up - the TCP connect succeeds, so a connect
   * bound never fires, and what is left is the wait for a response.
   */
  async connect(): Promise<void> {
    if (this.open) return
    try {
      const result = await this.send('SELECT version()', {
        timeoutMs: HANDSHAKE_TIMEOUT_MS,
        format: 'JSON',
      })
      const parsed = JSON.parse(result) as JsonResult
      this.serverVersion = String(Object.values(parsed.data?.[0] ?? {})[0] ?? '')
    } catch (error) {
      throw new EngineError(`could not connect to ClickHouse: ${message(error)}`)
    }
    this.open = true
  }

  async close(): Promise<void> {
    // Nothing to close: every request is its own HTTP exchange on an agent this adapter does not
    // keep alive. Present so that the shape matches the other adapter - a caller writing
    // `close()` in a `finally` should not have to know which engine they have.
    this.open = false
  }

  /** What the server said it is, or null before `connect()`. Read by nothing; useful in a report. */
  get version(): string | null {
    return this.serverVersion
  }

  private ensureOpen(): void {
    if (!this.open) throw new EngineError('not connected; call connect() first')
  }

  /**
   * One HTTP exchange.
   *
   * `timeoutMs` is **absent for a query**, which is the whole timeout design of this adapter: only
   * the handshake carries a bound, so nothing here can cut a slow analytical query. A caller who
   * wants one sets it on the server, where it belongs, with `max_execution_time`.
   */
  private send(
    sql: string,
    options: { readonly timeoutMs?: number; readonly format?: string; readonly body?: string } = {},
  ): Promise<string> {
    const query = options.format === undefined ? sql : `${sql} FORMAT ${options.format}`
    const search = new URLSearchParams({ database: this.target.database })
    if (options.body === undefined) search.set('query', query)
    else search.set('query', query)
    const path = `/?${search.toString()}`
    const payload = options.body ?? ''
    const requestFn = this.target.protocol === 'https:' ? httpsRequest : httpRequest

    return new Promise<string>((resolve, reject) => {
      const req = requestFn(
        {
          protocol: this.target.protocol,
          host: this.target.host,
          port: this.target.port,
          method: 'POST',
          path,
          headers: {
            'X-ClickHouse-User': this.target.user,
            'X-ClickHouse-Key': this.target.password,
            'Content-Type': 'text/plain; charset=utf-8',
            'Content-Length': Buffer.byteLength(payload),
          },
          ...(options.timeoutMs === undefined ? {} : { timeout: options.timeoutMs }),
        },
        (response) => {
          const chunks: Buffer[] = []
          response.on('data', (chunk: Buffer) => chunks.push(chunk))
          response.on('end', () => {
            const text = Buffer.concat(chunks).toString('utf8')
            const status = response.statusCode ?? 0
            if (status >= 200 && status < 300) resolve(text)
            // ClickHouse puts its own message in the body, and it names the column or the setting.
            // A summary of ours would lose exactly that.
            else reject(new Error(`ClickHouse answered ${status}: ${text.trim()}`))
          })
        },
      )
      // `timeout` on the options bounds socket inactivity, which covers a host that accepts the
      // connection and then says nothing - the case this exists for. The socket has to be destroyed
      // explicitly: Node emits the event and leaves the request open otherwise.
      req.on('timeout', () => {
        req.destroy(
          new Error(
            `no answer within ${options.timeoutMs} ms. The socket was accepted, so this is a host ` +
              `that took the connection and did not answer - a firewall that accepts, a load ` +
              `balancer with no healthy backend, a server mid-restart.`,
          ),
        )
      })
      req.on('error', reject)
      req.end(payload)
    })
  }

  private async query(sql: string): Promise<Row[]> {
    this.ensureOpen()
    const text = await this.send(sql, { format: 'JSON' })
    const parsed = JSON.parse(text) as JsonResult
    const types = new Map((parsed.meta ?? []).map((column) => [column.name, column.type]))
    return (parsed.data ?? []).map((row) => {
      const out: Row = {}
      for (const [name, value] of Object.entries(row)) {
        out[name] = convert(value, types.get(name) ?? 'String')
      }
      return out
    })
  }

  private async command(sql: string): Promise<void> {
    this.ensureOpen()
    await this.send(sql)
  }

  // --- schema ------------------------------------------------------------------------------

  async ensureSchema(
    layout: PhysicalLayout,
    options: { readonly keys: Readonly<Record<string, readonly string[]>> },
  ): Promise<void> {
    const statements = schemaStatements(layout, { keys: options.keys, dialect: this.dialect })
    for (const statement of statements) {
      try {
        await this.command(statement)
      } catch (error) {
        throw new EngineError(`schema statement failed: ${statement}: ${message(error)}`)
      }
    }
    await this.verifySchema(layout)
  }

  /**
   * Check that what exists is what the map describes, for the reason the PostgreSQL adapter does:
   * `CREATE TABLE IF NOT EXISTS` accepts a table of that name whatever shape it is in.
   *
   * A missing column is refused; an extra one is allowed, because a client may have added one
   * outside SDE and the map has no opinion about it.
   *
   * **Types too, and here they need no normalising at all.** Measured against every type this
   * library renders: `system.columns.type` returns the exact string we wrote, down to the space in
   * `Decimal(12, 2)` and the quotes in `DateTime64(6, 'UTC')`. So the comparison is literal, and
   * it is literal on purpose - a renderer that started emitting a different spelling of the same
   * type would fail this, which is the right way round for a document we sign.
   */
  private async verifySchema(layout: PhysicalLayout): Promise<void> {
    const expected = new Map<string, Record<string, string>>()
    for (const [entity, table] of Object.entries(layout.tables)) {
      expected.set(table, layout.columns[entity] ?? {})
    }
    if (expected.size === 0) return
    const names = [...expected.keys()].sort(compareCodePoints)
    // Through `literal`, not through a second escaper. The first version of this line inlined one
    // that escaped a quote and **not** a backslash, so a table name ending in one would have
    // closed the string early - and a table name is not always ours: a hand-written map is a
    // supported mode, and an unhashed entity name can contain anything. Found by CodeQL
    // (`js/incomplete-sanitization`), which is the same finding as the module docstring's rule
    // about quoting identifiers in one place, arriving one level down.
    const list = names.map(literal).join(', ')
    const rows = await this.query(
      `SELECT table, name, type FROM system.columns WHERE database = currentDatabase() ` +
        `AND table IN (${list})`,
    )
    const found = new Map<string, Map<string, string>>()
    for (const row of rows) {
      const table = String(row['table'])
      const columns = found.get(table) ?? new Map<string, string>()
      columns.set(String(row['name']), String(row['type']))
      found.set(table, columns)
    }
    for (const table of names) {
      const actual = found.get(table)
      if (actual === undefined) {
        throw new EngineError(
          `'${table}' does not exist after applying the schema. The statement reported success, ` +
            `so this is a permissions or database problem rather than a bad map.`,
        )
      }
      const columns = expected.get(table) as Record<string, string>
      const missing = Object.keys(columns)
        .filter((column) => !actual.has(column))
        .sort()
      if (missing.length > 0) {
        throw new EngineError(
          `'${table}' already existed with a different shape: the map needs ` +
            `[${missing.join(', ')}] and the table has ` +
            `[${[...actual.keys()].sort().join(', ')}]. ` +
            `CREATE TABLE IF NOT EXISTS keeps whatever is there, so this table came from ` +
            `somewhere else. Refusing here rather than at the first insert.`,
        )
      }
      for (const column of Object.keys(columns).sort()) {
        const declared = columns[column] as string
        const reported = actual.get(column) as string
        if (reported === declared) continue
        throw new EngineError(
          `${table}.${column} is '${reported}' and this map declares it '${declared}'. ` +
            `CREATE TABLE IF NOT EXISTS keeps a table of that name whatever shape it is in, and ` +
            `this library never alters a column's type - so the table came from somewhere else, ` +
            `or from a map that rendered this column differently. Refusing rather than writing ` +
            `into it: with a timestamp the difference is usually precision, and a write that ` +
            `succeeds and comes back rounded is worse than one that fails.`,
        )
      }
    }
  }


  // --- data --------------------------------------------------------------------------------

  private async insertRows(table: string, rows: readonly Row[]): Promise<void> {
    if (rows.length === 0) return
    const columns = Object.keys(rows[0] as Row).sort()
    const body = rows
      .map((row) => {
        const out: Record<string, unknown> = {}
        for (const column of columns) out[column] = outbound(row[column])
        return JSON.stringify(out)
      })
      .join('\n')
    this.ensureOpen()
    await this.send(
      `INSERT INTO ${quote(table)} (${columns.map(quote).join(', ')}) FORMAT JSONEachRow`,
      { body },
    )
  }

  async insert(table: string, values: Readonly<Row>): Promise<void> {
    if (Object.keys(values).length === 0) throw new EngineError('nothing to insert')
    try {
      await this.insertRows(table, [values])
    } catch (error) {
      throw new EngineError(`insert into ${table} failed: ${message(error)}`)
    }
  }

  /**
   * One row by key, with `FINAL` so a superseded row is never returned.
   *
   * Without `FINAL` a key that has been saved twice returns whichever duplicate the scan reaches
   * first until a merge happens - which is to say, nondeterministically the old value. Paying for
   * `FINAL` on a point read is the cheaper half of that trade.
   */
  async get(table: string, key: Readonly<Row>): Promise<Row | null> {
    const columns = Object.keys(key).sort()
    const where = columns.map((column) => `${quote(column)} = ${literal(key[column])}`).join(' AND ')
    try {
      const rows = await this.query(
        `SELECT * FROM ${quote(table)} FINAL WHERE ${where} LIMIT 1`,
      )
      return rows[0] ?? null
    } catch (error) {
      throw new EngineError(`select from ${table} failed: ${message(error)}`)
    }
  }

  async count(table: string): Promise<number> {
    try {
      const rows = await this.query(`SELECT count() AS n FROM ${quote(table)} FINAL`)
      return Number(rows[0]?.['n'] ?? 0)
    } catch (error) {
      throw new EngineError(`count on ${table} failed: ${message(error)}`)
    }
  }

  // --- rollback protection ------------------------------------------------------------------
  //
  // Append-only and `max()`, which is what makes this identical in both engines: no key to enforce,
  // no row to update, nothing to contend over.

  async mapWatermark(): Promise<number | null> {
    try {
      await this.command(
        `CREATE TABLE IF NOT EXISTS ${quote(WATERMARK_TABLE)} (` +
          `${quote('map_version')} Int64, ${quote('model_version')} String, ` +
          `${quote('seen_at')} DateTime64(3, 'UTC') DEFAULT now64(3)) ` +
          `ENGINE = MergeTree ORDER BY (${quote('map_version')})`,
      )
      const rows = await this.query(
        `SELECT max(${quote('map_version')}) AS high, count() AS n FROM ${quote(WATERMARK_TABLE)}`,
      )
      const row = rows[0]
      if (row === undefined || Number(row['n']) === 0) return null
      return Number(row['high'])
    } catch (error) {
      throw new EngineError(`reading ${WATERMARK_TABLE} failed: ${message(error)}`)
    }
  }

  async recordMapVersion(
    version: number,
    options: { readonly modelVersion: string },
  ): Promise<void> {
    try {
      await this.insertRows(WATERMARK_TABLE, [
        { map_version: version, model_version: options.modelVersion },
      ])
    } catch (error) {
      throw new EngineError(
        `recording a map version in ${WATERMARK_TABLE} failed: ${message(error)}`,
      )
    }
  }

  // --- migration ---------------------------------------------------------------------------

  /**
   * Rows in key order, strictly after one key and up to another inclusive, with `FINAL`.
   *
   * `FINAL` here is not tidiness: without it a key saved twice returns two rows until a merge
   * happens, and the next page starts strictly after that key - so one of the duplicates is read
   * and the other is not, which for a backfill means copying a row this engine considers
   * superseded.
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
    const tuple = `(${cols.map(quote).join(', ')})`
    const clauses: string[] = []
    if (options.after !== undefined) {
      sameWidth(options.after, cols, 'after')
      clauses.push(`${tuple} > (${options.after.map(literal).join(', ')})`)
    }
    if (options.upto !== undefined) {
      sameWidth(options.upto, cols, 'upto')
      clauses.push(`${tuple} <= (${options.upto.map(literal).join(', ')})`)
    }
    const where = clauses.length > 0 ? ` WHERE ${clauses.join(' AND ')}` : ''
    const cap = options.limit === undefined ? '' : ` LIMIT ${Number(options.limit)}`
    try {
      return await this.query(
        `SELECT * FROM ${quote(table)} FINAL${where} ORDER BY ${tuple}${cap}`,
      )
    } catch (error) {
      throw new EngineError(`key range select from ${table} failed: ${message(error)}`)
    }
  }

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
      const rows = await this.query(
        `SELECT ${projection} FROM ${quote(table)} FINAL ORDER BY (${projection}) ` +
          `LIMIT 1 OFFSET ${Number(options.position) - 1}`,
      )
      const row = rows[0]
      if (row === undefined) return null
      return cols.map((column) => row[column])
    } catch (error) {
      throw new EngineError(`reading row ${options.position} of ${table} failed: ${message(error)}`)
    }
  }

  /**
   * Insert rows. Idempotence here is the table's, not the statement's.
   *
   * There is no `ON CONFLICT` to ask for: a `ReplacingMergeTree` keeps the newest row for a key and
   * collapses the rest at merge time, so recopying a chunk after a crash converges instead of
   * raising. Not the same mechanism as the PostgreSQL adapter's, which is exactly why the live
   * tests run a backfill in every direction rather than in one.
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
    try {
      await this.insertRows(table, rows)
    } catch (error) {
      throw new EngineError(`copying ${rows.length} rows into ${table} failed: ${message(error)}`)
    }
  }

  async backfillMarker(options: {
    readonly materialization: string
    readonly entity: string
  }): Promise<number> {
    try {
      await this.command(
        `CREATE TABLE IF NOT EXISTS ${quote(BACKFILL_TABLE)} (` +
          `${quote('materialization')} String, ${quote('entity')} String, ` +
          `${quote('rows_copied')} Int64, ${quote('at')} DateTime64(3, 'UTC') DEFAULT now64(3)) ` +
          `ENGINE = MergeTree ORDER BY (${quote('materialization')}, ${quote('entity')})`,
      )
      const rows = await this.query(
        `SELECT max(${quote('rows_copied')}) AS high, count() AS n FROM ` +
          `${quote(BACKFILL_TABLE)} WHERE ${quote('materialization')} = ` +
          `${literal(options.materialization)} AND ${quote('entity')} = ${literal(options.entity)}`,
      )
      const row = rows[0]
      if (row === undefined || Number(row['n']) === 0) return 0
      return Number(row['high'])
    } catch (error) {
      throw new EngineError(`reading ${BACKFILL_TABLE} failed: ${message(error)}`)
    }
  }

  async recordBackfillMarker(options: {
    readonly materialization: string
    readonly entity: string
    readonly rows: number
  }): Promise<void> {
    try {
      await this.insertRows(BACKFILL_TABLE, [
        {
          materialization: options.materialization,
          entity: options.entity,
          rows_copied: options.rows,
        },
      ])
    } catch (error) {
      throw new EngineError(
        `recording backfill progress in ${BACKFILL_TABLE} failed: ${message(error)}`,
      )
    }
  }

  // --- transactions ------------------------------------------------------------------------

  /**
   * Refuses. There is no transaction here to give you.
   *
   * A no-op that ran the body would be the friendlier signature and the worse library: the caller
   * would believe a group of writes was atomic, and would find out otherwise from the state of the
   * data rather than from an exception.
   *
   * The way out is not a flag. Declare the atomicity - `atomicWith` on the entity - and the planner
   * is then obliged to place those entities in one group, and one group is one engine, so it will
   * not be this one.
   */
  transaction<T>(_body: () => Promise<T>): Promise<T> {
    throw new EngineError(
      'ClickHouse has no multi-statement transactions, so this adapter will not pretend to start ' +
        'one. If these writes have to commit together, declare it: atomicWith on the entities ' +
        'makes them one colocation group, one group is one engine, and the planner is then not ' +
        'permitted to put them here. A no-op that ran your callback would let the writes proceed ' +
        'and let you believe they were atomic.',
    )
  }
}

/**
 * A SQL literal for one value, and **the only place in this file that escapes a string**.
 *
 * Exported so it can be checked directly, because the alternative is checking it through a server
 * and a server reinterprets escapes inside identifiers: a table name carrying a backslash comes
 * back as a *different* name rather than as a syntax error, so a live test on one cannot isolate
 * this rule. `adapters.test.ts` pins the two characters and also refuses a second escaper anywhere
 * else in this file - which is the class of defect that produced it, found by CodeQL as
 * `js/incomplete-sanitization`.
 *
 * ClickHouse's HTTP interface has no bound parameters in the sense the PostgreSQL protocol does -
 * it has `param_name` substitution, which needs the declared type of every parameter in the query
 * text. So these are rendered, and the rendering is **total**: every kind this library can hold has
 * a branch, and an unknown kind throws rather than falling through to a string. A value that
 * reached a query as an unquoted `[object Object]` would be a syntax error at best.
 */
export function literal(value: unknown): string {
  if (value === null || value === undefined) return 'NULL'
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) {
      throw new EngineError(`${value} is not a value any engine here can store`)
    }
    return String(value)
  }
  if (typeof value === 'bigint') return value.toString()
  if (typeof value === 'boolean') return value ? '1' : '0'
  if (value instanceof Timestamp || value instanceof Date) return `'${String(outbound(value))}'`
  if (typeof value === 'string') return `'${value.replace(/\\/g, '\\\\').replace(/'/g, "\\'")}'`
  throw new EngineError(
    `a key value of type ${typeof value} cannot be rendered for ClickHouse. Rendering it as a ` +
      `string would produce a query that is wrong rather than one that fails.`,
  )
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

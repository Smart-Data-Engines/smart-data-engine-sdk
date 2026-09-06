/**
 * DDL as a value, not as a side effect.
 *
 * A layout and a set of keys in, statements out, and no connection anywhere. This module lives
 * outside `src/engines/` deliberately: that directory is the part of the library that opens
 * sockets, and rendering DDL needs a driver about as much as printing a receipt needs a bank. The
 * reference implementation draws the same line, and the control plane's import allowlist refuses
 * its engine package by name.
 *
 * Being pure is also what makes this the home of the `schema/` conformance vectors. Two libraries
 * that agree on a map and disagree on the DDL place one entity in tables with different columns -
 * and nothing raises, because each of them created a table successfully.
 *
 * **Identifiers are sorted with `compareCodePoints`, never with the default sort.** That is not
 * style. `Array.prototype.sort` orders by UTF-16 code unit, Python's `sorted` orders by code
 * point, and the two disagree for any identifier outside the Basic Multilingual Plane - so a
 * column named with an astral character would come out in a different order here than in the
 * reference, giving two `CREATE TABLE` statements for one layout. This is the second call site of
 * the comparator that produced the `canonical/` vector family; the first was object keys in the
 * IR. `schema/003` pins it.
 */

import { compareCodePoints } from './canonical.js'
import { EngineError } from './errors.js'
import type { PhysicalLayout } from './placement.js'

/** Every dialect this library renders DDL for, sorted. */
export const DIALECTS = ['clickhouse', 'orderbook', 'postgres'] as const

export type Dialect = (typeof DIALECTS)[number]

/**
 * Dialects whose physical schema the engine imposes rather than accepting from us.
 *
 * A named set rather than a test on the dialect string, so the places that have to behave
 * differently agree by construction instead of each checking for one name.
 */
export const FIXED_SCHEMA: ReadonlySet<string> = new Set(['orderbook'])

function quoteAnsi(identifier: string): string {
  return '"' + identifier.replaceAll('"', '""') + '"'
}

/**
 * Backticks, doubled to escape.
 *
 * ClickHouse accepts double quotes too. Backticks are the idiomatic form and, more usefully, they
 * make a generated statement obviously ClickHouse when it turns up in a log next to a PostgreSQL
 * one.
 */
function quoteBacktick(identifier: string): string {
  return '`' + identifier.replaceAll('`', '``') + '`'
}

/**
 * Quoting per dialect. The adapters bind their own from here instead of keeping a copy: two
 * implementations of an escaping rule is how one of them ends up missing the doubling.
 *
 * A fixed-schema engine has no entry, and the absence is deliberate rather than an omission: it
 * emits no DDL, and its query language takes the symbol and the exchange as string *literals*
 * rather than identifiers. A no-op function entered here for symmetry would look usable and
 * silently escape nothing the day somebody reached for it.
 */
export const QUOTE: Readonly<Record<string, (identifier: string) => string>> = {
  clickhouse: quoteBacktick,
  postgres: quoteAnsi,
}

export interface SchemaOptions {
  readonly keys: Readonly<Record<string, readonly string[]>>
  readonly dialect: string
}

function sorted(values: Iterable<string>): string[] {
  return [...values].sort(compareCodePoints)
}

/** `[entity, table]` in code point order of the entity name. See the module docstring. */
function tablesInOrder(layout: PhysicalLayout): (readonly [string, string])[] {
  return Object.entries(layout.tables).sort(([a], [b]) => compareCodePoints(a, b))
}

function columnsAndKey(
  layout: PhysicalLayout,
  entity: string,
  keys: Readonly<Record<string, readonly string[]>>,
): { columns: string[]; key: readonly string[] } {
  const cols = layout.columns[entity] ?? {}
  const columns = sorted(Object.keys(cols))
  if (columns.length === 0) throw new EngineError(`the layout gives no columns for '${entity}'`)
  const key = keys[entity] ?? []
  if (key.length === 0) {
    throw new EngineError(`no key for '${entity}'; a table without one cannot be addressed`)
  }
  return { columns, key }
}

function typeOf(layout: PhysicalLayout, entity: string, column: string): string {
  const declared = (layout.columns[entity] ?? {})[column]
  // Unreachable through `columnsAndKey`, which reads the same object. Present because the
  // alternative under `noUncheckedIndexedAccess` is a non-null assertion, and an assertion is a
  // claim with no message on the day it is wrong.
  if (declared === undefined) {
    throw new EngineError(`the layout gives no type for ${entity}.${column}`)
  }
  return declared
}

function postgresStatements(layout: PhysicalLayout, options: SchemaOptions): string[] {
  const statements: string[] = []
  for (const [entity, table] of tablesInOrder(layout)) {
    const { columns, key } = columnsAndKey(layout, entity, options.keys)
    const defs = columns.map((c) => `${quoteAnsi(c)} ${typeOf(layout, entity, c)}`).join(', ')
    const pk = key.map(quoteAnsi).join(', ')
    statements.push(
      `CREATE TABLE IF NOT EXISTS ${quoteAnsi(table)} (${defs}, PRIMARY KEY (${pk}))`,
    )
  }

  for (const index of layout.indexes) {
    const entity = String(index['entity'])
    const table = layout.tables[entity]
    if (table === undefined) continue
    const name = String(index['name'])
    const declared = index['columns']
    if (!Array.isArray(declared) || declared.length === 0) {
      throw new EngineError(
        `the index '${name}' on '${entity}' names no columns. An index over nothing is not a ` +
          `narrower index, it is a statement that will not parse.`,
      )
    }
    const cols = declared.map((c) => quoteAnsi(String(c))).join(', ')
    statements.push(
      `CREATE INDEX IF NOT EXISTS ${quoteAnsi(name)} ON ${quoteAnsi(table)} (${cols})`,
    )
  }
  return statements
}

function clickhouseStatements(layout: PhysicalLayout, options: SchemaOptions): string[] {
  const statements: string[] = []
  for (const [entity, table] of tablesInOrder(layout)) {
    const { columns, key } = columnsAndKey(layout, entity, options.keys)
    const missing = key.filter((column) => !columns.includes(column))
    if (missing.length > 0) {
      throw new EngineError(
        `the key of '${entity}' names columns the layout does not have: ` +
          `[${missing.map((m) => `'${m}'`).join(', ')}]. In ClickHouse the key becomes ORDER BY, ` +
          `so this would produce a table that cannot be created rather than one with a missing ` +
          `constraint.`,
      )
    }
    const defs = columns.map((c) => `${quoteBacktick(c)} ${typeOf(layout, entity, c)}`).join(', ')
    // ORDER BY is the declared key, in declared order. That order is positional and carries
    // meaning: it decides which prefixes of the key can prune granules, so sorting it would change
    // the physical performance of the table while leaving the map looking identical.
    const order = key.map(quoteBacktick).join(', ')
    statements.push(
      `CREATE TABLE IF NOT EXISTS ${quoteBacktick(table)} (${defs}) ` +
        `ENGINE = ReplacingMergeTree ORDER BY (${order})`,
    )
  }

  if (layout.indexes.length > 0) {
    throw new EngineError(
      `the layout carries ${layout.indexes.length} index definitions and this engine has no ` +
        `B-tree to put them in. A ClickHouse index is a data-skipping index with a type and a ` +
        `granularity, so this map was built for another dialect.`,
    )
  }
  return statements
}

/**
 * No DDL, because this engine's schema is not ours to create.
 *
 * An empty list rather than a throw, and the difference matters. "Run nothing" is the *correct*
 * action for a caller preparing a fixed-schema engine: the storage exists the moment the engine
 * opens its data directory. Throwing would push the branch out to every caller. What the empty
 * list loses is the *explanation*, and {@link schemaIsFixed} supplies that to anybody printing
 * one - "here is the schema we chose for you: (nothing)" needs a sentence after it.
 */
function noStatements(): string[] {
  return []
}

const BY_DIALECT: Readonly<Record<string, (l: PhysicalLayout, o: SchemaOptions) => string[]>> = {
  clickhouse: clickhouseStatements,
  orderbook: noStatements,
  postgres: postgresStatements,
}

/**
 * Whether this engine imposes its own schema, so an empty statement list means "nothing to do".
 *
 * The one public way to tell that apart from "no tables in this layout". A caller that never asks
 * still behaves correctly - creating nothing is right - but a caller *reporting* what the client
 * must do has a different sentence to write.
 */
export function schemaIsFixed(dialect: string): boolean {
  if (!(dialect in BY_DIALECT)) {
    throw new EngineError(
      `unknown dialect '${dialect}'; this library renders [${DIALECTS.join(', ')}]. Answering ` +
        `false would say 'that engine takes DDL from us' about an engine it has never heard of.`,
    )
  }
  return FIXED_SCHEMA.has(dialect)
}

/**
 * The statements that would create this layout, in the order they must run.
 *
 * Idempotent by construction - every statement is `IF NOT EXISTS` - because an application
 * restarting must not reapply DDL and two instances starting at once must not race. Anything
 * beyond creation is a migration, which carries a rollback path and a safety classification.
 *
 * An unknown dialect throws. Falling back to ANSI would emit a statement that looks right, runs on
 * the wrong engine, and creates a table with the wrong storage semantics.
 */
export function schemaStatements(
  layout: PhysicalLayout,
  options: SchemaOptions,
): readonly string[] {
  const build = BY_DIALECT[options.dialect]
  if (build === undefined) {
    throw new EngineError(
      `no DDL for dialect '${options.dialect}'; this library renders [${DIALECTS.join(', ')}]. ` +
        `Refusing rather than falling back to ANSI: a statement that looks right on the wrong ` +
        `engine creates a table with the wrong storage semantics.`,
    )
  }
  return build(layout, options)
}

/**
 * What can stand under a table's old name after a group has moved, and what cannot.
 *
 * `notPossible` is the field worth reading, because in this library's engine set it is usually the
 * populated one, and the reasons are structural rather than missing work. A caller who gets an
 * empty list back cannot tell "nothing needed" from "nothing thought about".
 */
export interface CompatibilityViews {
  /** Statements to run on the **target** when reads switch, in order. */
  readonly create: readonly string[]
  /** Statements to run when the source is dropped, so the view goes with what it replaced. */
  readonly drop: readonly string[]
  /** `[entity, why]` for each table that cannot have one, sorted by entity. */
  readonly notPossible: readonly (readonly [string, string])[]
  /** Whether every table of the group got one. False is ordinary - see above. */
  readonly complete: boolean
}

export interface ViewOptions {
  /** Entity name to the table name it had in the engine it left. */
  readonly was: Readonly<Record<string, string>>
  readonly dialect: string
}

/**
 * Views on the target under the table names the group had in the engine it left.
 *
 * **A view cannot cross engines, and that is why this renders on the target.** The old table is in
 * the old engine, and no dialect here has a way to select from another server. So what this offers
 * is for the case where somebody re-points their tool at the new engine and their SQL still says
 * the old table name.
 *
 * Columns are listed rather than `SELECT *`, so the view names exactly what the map names, and
 * they are listed **sorted by name, exactly as `CREATE TABLE` sorts them**, because a view whose
 * columns came out differently would break anything reading them by position, which is most of
 * what a hand-written query does with `SELECT *`. The reference implementation read the order off
 * the layout document instead, on the stated grounds that the document was already sorted - it is
 * not, since a foreign-key column is appended per relation, so any entity with a relation had a
 * view and a table that disagreed. `schema/009` feeds a document whose columns are out of order on
 * purpose, which makes both sorts separately mutable.
 *
 * For ClickHouse the view reads `FINAL`, and that is the substance rather than a detail: the table
 * is a `ReplacingMergeTree`, so a plain read returns rows the declared key should have collapsed
 * until a background merge happens. A query moved verbatim to the target returns numbers that are
 * too big, silently. A view that quietly reproduced that would be worse than no view.
 */
export function compatibilityViews(
  layout: PhysicalLayout,
  options: ViewOptions,
): CompatibilityViews {
  const { was, dialect } = options
  if (!(dialect in BY_DIALECT)) {
    throw new EngineError(
      `no compatibility view for dialect '${dialect}'; this library renders ` +
        `[${DIALECTS.join(', ')}].`,
    )
  }
  const entities = tablesInOrder(layout)
  if (FIXED_SCHEMA.has(dialect)) {
    const notPossible = entities.map(
      ([entity, table]) =>
        [
          entity,
          `${dialect} imposes its own schema and accepts no DDL from this library, so there is ` +
            `nowhere to put a view. Its table is '${table}' and the old name was ` +
            `'${was[entity]}': a query naming the old one has to be edited.`,
        ] as const,
    )
    return { create: [], drop: [], notPossible, complete: notPossible.length === 0 }
  }

  const quote = QUOTE[dialect]
  // Unreachable: `FIXED_SCHEMA` is the only dialect without an entry and it returned above. A
  // narrowing check rather than a non-null assertion, because the assertion is the version that
  // has no message on the day the two tables stop agreeing.
  if (quote === undefined) throw new EngineError(`no quoting rule for dialect '${dialect}'`)
  // Idempotent like every statement `schemaStatements` renders, and the two dialects spell that
  // differently - measured against both servers rather than assumed. PostgreSQL has no
  // `CREATE VIEW IF NOT EXISTS`: it is a syntax error.
  const opening = dialect === 'postgres' ? 'CREATE OR REPLACE VIEW' : 'CREATE VIEW IF NOT EXISTS'
  const final = dialect === 'clickhouse' ? ' FINAL' : ''
  const create: string[] = []
  const drop: string[] = []
  const notPossible: (readonly [string, string])[] = []
  for (const [entity, table] of entities) {
    const old = was[entity]
    if (old === undefined) {
      notPossible.push([
        entity,
        `the source layout gives no table for '${entity}', so there is no old name to stand in for.`,
      ])
      continue
    }
    if (old === table) {
      notPossible.push([
        entity,
        `both engines call this table '${table}', so the name is not what moved - the dialect is.` +
          (dialect === 'clickhouse'
            ? ` A query moved here verbatim must read \`FROM ${quote(table)} FINAL\`: the table ` +
              `is a ReplacingMergeTree, so without it a row written twice under one key is ` +
              `counted twice until a background merge collapses it - measured, two rows against ` +
              `one.`
            : ''),
      ])
      continue
    }
    const cols = sorted(Object.keys(layout.columns[entity] ?? {}))
    if (cols.length === 0) throw new EngineError(`the layout gives no columns for '${entity}'`)
    // Sorted by name, which is what `CREATE TABLE` does. See the docstring: reading this order off
    // the document instead was wrong, and wrong in a way one fixture could not show.
    const selected = cols.map(quote).join(', ')
    create.push(`${opening} ${quote(old)} AS SELECT ${selected} FROM ${quote(table)}${final}`)
    drop.push(`DROP VIEW IF EXISTS ${quote(old)}`)
  }
  return { create, drop, notPossible, complete: notPossible.length === 0 }
}

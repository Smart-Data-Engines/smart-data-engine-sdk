/**
 * The physical design vocabulary a placement map can carry, and the rules each element obeys.
 *
 * Placement map contract 5 lets a layout say three things about storage beyond tables and column
 * types: the physical order of an entity's key, a time partition, and indexes of a named method.
 * They are chosen by the control plane and a library only renders them. Every element is a closed
 * vocabulary rendered by this library; nothing in a map is pasted into DDL.
 *
 * This is the port of the reference's `sde/physical.py`, and it is one module for the reason that
 * one is: the map parser, the DDL renderer and the adapters' verification all read it, because a
 * second copy of "which index methods ClickHouse has" is how a map gets signed that no library can
 * apply. The two data rules - a partition follows the key, and `key_order` only reorders the key -
 * were measured on PostgreSQL 15.19 and ClickHouse 24.8.14.39 before they were written; the
 * reference module carries the measurements.
 */

import { MapError } from './errors.js'
import { compareCodePoints } from './canonical.js'

export const PHYSICAL_DESIGN_SINCE = 5
/** The placement map contract that introduced `key_order`, `partition_by` and index methods. */

export const GRANULARITIES = ['day', 'month', 'year'] as const
/**
 * Time partition sizes. No week: ClickHouse's `toStartOfWeek` depends on a mode, and a closed
 * vocabulary has no modes.
 */

export const TEMPORAL_TYPES = ['date', 'timestamptz'] as const
/**
 * Neutral types a partition may be derived from. `timestamp` is absent on purpose: its ClickHouse
 * column carries no zone, so the partition a value falls into follows the server's configured
 * timezone, and a reconfigured server would put one key into two partitions for ever.
 */
export const POSTGRES_METHODS = ['brin', 'btree'] as const
export const CLICKHOUSE_METHODS = ['bloom_filter', 'minmax', 'set'] as const
/** ClickHouse data-skipping index types. It has no B-tree; its primary index is `ORDER BY`. */

export const INDEX_METHODS: readonly string[] = [...POSTGRES_METHODS, ...CLICKHOUSE_METHODS].sort(
  compareCodePoints,
)

export const GRANULARITY_RANGE = [1, 1024] as const
export const SET_ROWS_RANGE = [1, 65536] as const
/** `set(0)` means unlimited in ClickHouse, an unbounded memory commitment, so zero is outside. */

const LEGACY_INDEX_KEYS: ReadonlySet<string> = new Set(['columns', 'entity', 'name'])
const LATER_INDEX_KEYS: ReadonlySet<string> = new Set(['granularity', 'max_rows', 'method'])
const INDEX_KEYS: ReadonlySet<string> = new Set([...LEGACY_INDEX_KEYS, ...LATER_INDEX_KEYS])

export const PARTITION_FUNCTIONS: Readonly<Record<string, string>> = Object.freeze({
  day: 'toDate',
  month: 'toYYYYMM',
  year: 'toYear',
})
/** ClickHouse partition expressions, as its catalogue reports them back. */

export const PARTITIONING_DIALECTS: ReadonlySet<string> = new Set(['clickhouse'])
/**
 * Dialects that render `partition_by`. PostgreSQL is absent on purpose: declarative partitioning
 * there needs every partition created before a row can arrive, a lifecycle this product does not
 * manage, and an unpartitioned table under a map that says otherwise would be a silent drop.
 */

export const METHODS_BY_DIALECT: Readonly<Record<string, readonly string[]>> = Object.freeze({
  clickhouse: CLICKHOUSE_METHODS,
  postgres: POSTGRES_METHODS,
})

export type PartitionSpec = Readonly<{ field: string; granularity: string }>

type ErrorClass = new (message: string) => Error

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value)
}

function show(value: unknown): string {
  return value === undefined ? 'undefined' : JSON.stringify(value)
}

function sortedKeys(value: Record<string, unknown>): string[] {
  return Object.keys(value).sort(compareCodePoints)
}

/** An index's method. Absent means `btree`, what every map before contract 5 meant. */
export function indexMethod(index: Readonly<Record<string, unknown>>): string {
  return index['method'] === undefined ? 'btree' : String(index['method'])
}

function nonEmptyNames(value: unknown): value is string[] {
  return (
    Array.isArray(value) &&
    value.length > 0 &&
    value.every((column) => typeof column === 'string' && column.length > 0)
  )
}

/** `key_order`: entity -> the physical order of that entity's key columns. */
export function parseKeyOrder(
  raw: unknown,
  where: string,
  tables: Readonly<Record<string, string>>,
): Record<string, readonly string[]> {
  if (!isRecord(raw)) throw new MapError(`${where}: key_order maps an entity to a list of its key columns`)
  const out: Record<string, readonly string[]> = Object.create(null)
  for (const entity of sortedKeys(raw)) {
    const columns = raw[entity]
    if (!Object.hasOwn(tables, entity)) {
      throw new MapError(
        `${where}: key_order names '${entity}', which has no table in this layout. It has ` +
          `${JSON.stringify(sortedKeys(tables))}.`,
      )
    }
    if (!nonEmptyNames(columns)) {
      throw new MapError(`${where}: key_order['${entity}'] must be a non-empty list of column names`)
    }
    if (new Set(columns).size !== columns.length) {
      throw new MapError(
        `${where}: key_order['${entity}'] names a column twice: ${JSON.stringify(columns)}. A key ` +
          'order is a permutation of the key, so each key column appears exactly once.',
      )
    }
    out[entity] = [...columns]
  }
  return out
}

/** `partition_by`: entity -> `{"field": ..., "granularity": ...}`, and nothing else. */
export function parsePartitionBy(
  raw: unknown,
  where: string,
  tables: Readonly<Record<string, string>>,
): Record<string, PartitionSpec> {
  if (!isRecord(raw)) {
    throw new MapError(`${where}: partition_by maps an entity to a {field, granularity} object`)
  }
  const out: Record<string, PartitionSpec> = Object.create(null)
  for (const entity of sortedKeys(raw)) {
    const spec = raw[entity]
    if (!Object.hasOwn(tables, entity)) {
      throw new MapError(
        `${where}: partition_by names '${entity}', which has no table in this layout. It has ` +
          `${JSON.stringify(sortedKeys(tables))}.`,
      )
    }
    const keys = isRecord(spec) ? sortedKeys(spec) : null
    if (keys === null || keys.length !== 2 || keys[0] !== 'field' || keys[1] !== 'granularity') {
      throw new MapError(
        `${where}: partition_by['${entity}'] must be exactly {"field": ..., "granularity": ...}; ` +
          `found keys ${keys === null ? (Array.isArray(spec) ? 'list' : typeof spec) : JSON.stringify(keys)}. ` +
          'The vocabulary is closed so that nothing in a map is pasted into DDL.',
      )
    }
    const record = spec as Record<string, unknown>
    const field = record['field']
    const granularity = record['granularity']
    if (typeof field !== 'string' || field.length === 0) {
      throw new MapError(`${where}: partition_by['${entity}'].field must name a column`)
    }
    if (typeof granularity !== 'string' || !(GRANULARITIES as readonly string[]).includes(granularity)) {
      throw new MapError(
        `${where}: partition_by['${entity}'].granularity is ${show(granularity)}; it is one of ` +
          `${JSON.stringify(GRANULARITIES)}.`,
      )
    }
    out[entity] = Object.freeze({ field, granularity })
  }
  return out
}

/**
 * Index definitions, validated where the map arrives rather than when DDL is rendered.
 *
 * The structural half applies to every contract and is a tightening; the method, granularity and
 * `max_rows` keys are contract 5, because an earlier library ignores them and would build a B-tree
 * where a later one builds BRIN from the same document.
 */
export function parseIndexes(
  raw: unknown,
  where: string,
  tables: Readonly<Record<string, string>>,
  columns: Readonly<Record<string, Readonly<Record<string, string>>>>,
  contract: number,
): Readonly<Record<string, unknown>>[] {
  if (raw === undefined || raw === null) return []
  if (!Array.isArray(raw)) throw new MapError(`${where}: indexes is a list of index definitions`)
  const allowed = contract >= PHYSICAL_DESIGN_SINCE ? INDEX_KEYS : LEGACY_INDEX_KEYS
  const out: Readonly<Record<string, unknown>>[] = []
  const names = new Set<string>()
  raw.forEach((index: unknown, position: number) => {
    const at = `${where}: indexes[${position}]`
    if (!isRecord(index)) throw new MapError(`${at} must be an object`)
    const unknown = sortedKeys(index).filter((key) => !allowed.has(key))
    if (unknown.length > 0) {
      const later = unknown.filter((key) => LATER_INDEX_KEYS.has(key))
      if (later.length > 0) {
        throw new MapError(
          `${at} uses ${JSON.stringify(later)}, which placement map contract ${PHYSICAL_DESIGN_SINCE} ` +
            `introduced, in a document declaring contract ${contract}. A library of that contract ` +
            'would ignore the key and create a different index from the same document, which is ' +
            'the difference the version exists to prevent.',
        )
      }
      throw new MapError(`${at} has keys this format does not define: ${JSON.stringify(unknown)}`)
    }
    const entity = index['entity']
    const name = index['name']
    const cols = index['columns']
    if (typeof entity !== 'string' || !Object.hasOwn(tables, entity)) {
      throw new MapError(
        `${at} names entity ${show(entity)}, which has no table in this layout. It has ` +
          `${JSON.stringify(sortedKeys(tables))}.`,
      )
    }
    if (typeof name !== 'string' || name.length === 0) throw new MapError(`${at} needs a non-empty name`)
    if (names.has(name)) throw new MapError(`${at} reuses the index name '${name}'`)
    names.add(name)
    if (!nonEmptyNames(cols) || new Set(cols).size !== cols.length) {
      throw new MapError(`${at} needs a non-empty list of distinct column names`)
    }
    const declared = Object.hasOwn(columns, entity) ? columns[entity] : undefined
    if (declared !== undefined && Object.keys(declared).length > 0) {
      const missing = cols.filter((column) => !Object.hasOwn(declared, column))
      if (missing.length > 0) {
        throw new MapError(
          `${at} indexes ${JSON.stringify(missing)}, which '${entity}' does not have in this ` +
            'layout. An index on a missing column is refused by the engine at CREATE INDEX, in ' +
            "the client's process, rather than here.",
        )
      }
    }
    const method = index['method'] === undefined ? 'btree' : index['method']
    if (typeof method !== 'string' || !INDEX_METHODS.includes(method)) {
      throw new MapError(`${at} has method ${show(method)}; the methods are ${JSON.stringify(INDEX_METHODS)}`)
    }
    const granularity = index['granularity']
    const maxRows = index['max_rows']
    if ((CLICKHOUSE_METHODS as readonly string[]).includes(method)) {
      const [low, high] = GRANULARITY_RANGE
      if (!isInteger(granularity) || granularity < low || granularity > high) {
        throw new MapError(
          `${at}: a ${method} index needs an integer granularity from ${low} to ${high}; found ` +
            show(granularity),
        )
      }
      if (cols.length !== 1) {
        throw new MapError(`${at}: a ${method} index summarises exactly one column, found ${JSON.stringify(cols)}`)
      }
    } else if (granularity !== undefined) {
      throw new MapError(`${at}: granularity belongs to data-skipping indexes, not ${method}`)
    }
    if (method === 'set') {
      const [low, high] = SET_ROWS_RANGE
      if (!isInteger(maxRows) || maxRows < low || maxRows > high) {
        throw new MapError(
          `${at}: a set index needs an integer max_rows from ${low} to ${high}; found ` +
            `${show(maxRows)}. Zero means unlimited in ClickHouse, which is not a choice this ` +
            'format offers.',
        )
      }
    } else if (maxRows !== undefined) {
      throw new MapError(`${at}: max_rows belongs to set indexes, not ${method}`)
    }
    out.push({ ...index })
  })
  return out
}

/**
 * The two rules that need the model: a permutation of the key, and a partition on a key column.
 *
 * A renderer without the model repeats the key half against the keys it is given (`effectiveKey`,
 * `partitionExpression`), which keeps the data rule true for a caller that skipped the model.
 */
export function checkAgainstModel(
  where: string,
  entity: string,
  key: readonly string[],
  fieldTypes: Readonly<Record<string, string>>,
  keyOrder: readonly string[] | undefined,
  partition: PartitionSpec | undefined,
): void {
  if (keyOrder !== undefined && !samePermutation(keyOrder, key)) {
    throw new MapError(
      `${where}: key_order['${entity}'] is ${JSON.stringify(keyOrder)} and the declared key is ` +
        `${JSON.stringify(key)}. A key order reorders the key; it cannot add, drop or replace a ` +
        'column, because the key is what makes a row the same row in every engine.',
    )
  }
  if (partition !== undefined) {
    const field = partition.field
    if (!key.includes(field)) {
      throw new MapError(
        `${where}: partition_by['${entity}'] partitions on '${field}', which is not in the key ` +
          `${JSON.stringify(key)}. ClickHouse collapses rows of one key only inside one ` +
          'partition, so two writes of one key could land in two partitions and stay two rows ' +
          'physically for ever - measured: after OPTIMIZE FINAL both remained.',
      )
    }
    const kind = Object.hasOwn(fieldTypes, field) ? fieldTypes[field] : undefined
    if (kind === undefined || !(TEMPORAL_TYPES as readonly string[]).includes(kind)) {
      throw new MapError(
        `${where}: partition_by['${entity}'] partitions on '${field}', which is ${show(kind)}; a ` +
          `time partition needs one of ${JSON.stringify(TEMPORAL_TYPES)}.`,
      )
    }
  }
}

function samePermutation(left: readonly string[], right: readonly string[]): boolean {
  if (left.length !== right.length) return false
  const a = [...left].sort(compareCodePoints)
  const b = [...right].sort(compareCodePoints)
  return a.every((value, position) => value === b[position])
}

/** The key in physical order: `key_order` when present, the declared order otherwise. */
export function effectiveKey(
  where: string,
  entity: string,
  key: readonly string[],
  keyOrder: Readonly<Record<string, readonly string[]>>,
  error: ErrorClass = MapError,
): string[] {
  const ordered = Object.hasOwn(keyOrder, entity) ? keyOrder[entity] : undefined
  if (ordered === undefined) return [...key]
  if (!samePermutation(ordered, key)) {
    throw new error(
      `${where}: key_order['${entity}'] is ${JSON.stringify(ordered)} and the key is ` +
        `${JSON.stringify(key)}. A key order must be a permutation of the key.`,
    )
  }
  return [...ordered]
}

/** `[function, field]` for a ClickHouse partition, after the key rule is checked again. */
export function partitionExpression(
  where: string,
  entity: string,
  key: readonly string[],
  partition: PartitionSpec | undefined,
  error: ErrorClass = MapError,
): readonly [string, string] | null {
  if (partition === undefined) return null
  if (!key.includes(partition.field)) {
    throw new error(
      `${where}: partition_by['${entity}'] partitions on '${partition.field}', outside the key ` +
        `${JSON.stringify(key)}; duplicates of one key would survive merges in two partitions.`,
    )
  }
  const fn = PARTITION_FUNCTIONS[partition.granularity]
  if (fn === undefined) throw new error(`${where}: unknown partition granularity ${partition.granularity}`)
  return [fn, partition.field]
}

/**
 * What a dialect renders, for whoever has to propose a physical design for it.
 *
 * The control plane hands the reference's version of this to a model; this port exists so that a
 * TypeScript caller asking the same question gets the same answer, key for key.
 */
export function capabilities(dialect: string): Record<string, unknown> {
  const methodsOf = Object.hasOwn(METHODS_BY_DIALECT, dialect) ? METHODS_BY_DIALECT[dialect] : undefined
  if (methodsOf === undefined) return { physical_design: false }
  const methods: Record<string, unknown> = {}
  for (const method of methodsOf) {
    const entry: Record<string, unknown> = {}
    if ((CLICKHOUSE_METHODS as readonly string[]).includes(method)) {
      entry['granularity'] = [...GRANULARITY_RANGE]
      entry['columns'] = 1
    }
    if (method === 'set') entry['max_rows'] = [...SET_ROWS_RANGE]
    methods[method] = entry
  }
  return {
    physical_design: true,
    key_order: true,
    partition_granularities: PARTITIONING_DIALECTS.has(dialect) ? [...GRANULARITIES] : [],
    partition_on: 'a key column of type date or timestamptz',
    index_methods: methods,
  }
}

// ── Reading the physical design back from an engine ─────────────────────────────────────────────
//
// `CREATE TABLE IF NOT EXISTS` keeps an existing table whatever its sort key or partition, and
// PostgreSQL's `CREATE INDEX IF NOT EXISTS ... USING brin` keeps an existing B-tree of that name -
// both measured. What an engine holds is read from its catalogue after the statements run and
// compared with the declaration. Where the catalogue speaks a formatted expression (ClickHouse),
// it is parsed into names rather than compared with a string we predict.

/** One way an existing table differs from the physical design its layout declares. */
export interface PhysicalFinding {
  readonly table: string
  readonly aspect: string
  readonly declared: string
  readonly found: string
}

export function describeFinding(finding: PhysicalFinding): string {
  return `${finding.table}: ${finding.aspect} is ${finding.found} and the map declares ${finding.declared}`
}

export interface DeclaredIndex {
  readonly name: string
  readonly method: string
  readonly columns: readonly string[]
  readonly granularity: number | null
  readonly typeFull: string
}

export interface DeclaredTable {
  readonly table: string
  readonly key: readonly string[]
  readonly partition: readonly [string, string] | null
  readonly indexes: readonly DeclaredIndex[]
}

export interface PhysicalDesign {
  readonly tables: Readonly<Record<string, string>>
  readonly indexes: readonly Readonly<Record<string, unknown>>[]
  readonly partitionBy: Readonly<Record<string, PartitionSpec>>
  readonly keyOrder?: Readonly<Record<string, readonly string[]>>
}

/** The physical expectations of every table a layout names, in table order. */
export function declaredTables(
  layout: PhysicalDesign,
  keys: Readonly<Record<string, readonly string[]>>,
): DeclaredTable[] {
  const out: DeclaredTable[] = []
  const entries = Object.entries(layout.tables).sort(([, a], [, b]) => compareCodePoints(a, b))
  const indexes = [...layout.indexes].sort((a, b) => compareCodePoints(String(a['name']), String(b['name'])))
  for (const [entity, table] of entries) {
    const key = Object.hasOwn(keys, entity) ? [...(keys[entity] ?? [])] : []
    const where = `table '${table}'`
    const ordered = effectiveKey(where, entity, key, layout.keyOrder ?? {})
    const spec = Object.hasOwn(layout.partitionBy, entity) ? layout.partitionBy[entity] : undefined
    const partition = partitionExpression(where, entity, key, spec)
    const declared: DeclaredIndex[] = []
    for (const index of indexes) {
      if (String(index['entity']) !== entity) continue
      const method = indexMethod(index)
      const granularity = index['granularity']
      declared.push({
        name: String(index['name']),
        method,
        columns: (index['columns'] as unknown[]).map(String),
        granularity: typeof granularity === 'number' ? granularity : null,
        typeFull: method === 'set' ? `set(${String(index['max_rows'])})` : method,
      })
    }
    out.push({ table, key: ordered, partition, indexes: declared })
  }
  return out
}

const BARE = /[A-Za-z_][A-Za-z0-9_]*/y

/**
 * ``a, `b c`, d`` -> `["a", "b c", "d"]`: names as ClickHouse's catalogue writes them.
 *
 * A name is bare or backtick-quoted with a backslash escaping the next character - the rule this
 * library writes. Anything else throws: an expression the parser does not understand is not
 * evidence that the table matches.
 */
export function parseIdentifierList(text: string): string[] {
  const names: string[] = []
  let position = 0
  const length = text.length
  while (position < length) {
    if (text[position] === '`') {
      position += 1
      let name = ''
      for (;;) {
        if (position >= length) throw new Error(`unterminated quoted name in ${JSON.stringify(text)}`)
        const char = text[position]!
        if (char === '\\') {
          if (position + 1 >= length) throw new Error(`dangling escape in ${JSON.stringify(text)}`)
          name += text[position + 1]!
          position += 2
          continue
        }
        if (char === '`') {
          position += 1
          break
        }
        name += char
        position += 1
      }
      names.push(name)
    } else {
      BARE.lastIndex = position
      const match = BARE.exec(text)
      if (match === null) throw new Error(`not a list of column names: ${JSON.stringify(text)}`)
      names.push(match[0])
      position = BARE.lastIndex
    }
    if (position < length) {
      // A separator is only ever between two names: ", " at the end is not a list.
      if (!text.startsWith(', ', position) || position + 2 >= length) {
        throw new Error(`not a list of column names: ${JSON.stringify(text)}`)
      }
      position += 2
    }
  }
  return names
}

/** ``toYYYYMM(`at`)`` -> `["toYYYYMM", "at"]`; an empty key -> `null`. */
export function parsePartitionKey(text: string): readonly [string, string] | null {
  if (text === '') return null
  const match = /^([A-Za-z][A-Za-z0-9]*)\((.*)\)$/s.exec(text)
  if (match === null) throw new Error(`not a single-function partition key: ${JSON.stringify(text)}`)
  const names = parseIdentifierList(match[2]!)
  if (names.length !== 1) throw new Error(`not a single-function partition key: ${JSON.stringify(text)}`)
  return [match[1]!, names[0]!]
}

/**
 * Throw when a table differs physically. Provisioning calls this; sessions do not.
 *
 * A session only reports (`session.physical`): the difference is performance, and turning it into
 * the application's outage is what requirement 3.6 forbids. Provisioning is where a person applying
 * a map can act on it.
 */
export function refuseFindings(findings: readonly PhysicalFinding[], error: ErrorClass): void {
  if (findings.length === 0) return
  const details = findings.map(describeFinding).join('; ')
  throw new error(
    `existing tables differ from the physical design this map declares: ${details}. ` +
      '`CREATE ... IF NOT EXISTS` keeps whatever table or index already has the name, so this ' +
      'came from an earlier map or from outside SDE. A new layout needs fresh tables (staging), ' +
      'not the old ones under the new declaration.',
  )
}

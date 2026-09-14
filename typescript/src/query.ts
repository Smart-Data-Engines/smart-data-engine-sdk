/** Bounded logical read plans. Predicate values never become shape identities or telemetry. */
import { compareCodePoints } from './canonical.js'
import { ModelPlanningError } from './errors.js'
import { QUOTE } from './schema.js'
import type { Row } from './session.js'
import { Timestamp } from './timestamp.js'

export const MAX_PAGE_ROWS = 1000
export const MAX_ORDER_FIELDS = 32

export class QueryRefused extends ModelPlanningError {
  override readonly name = 'QueryRefused'
}

export interface Range {
  readonly field: string
  readonly low?: unknown
  readonly high?: unknown
}
export interface ReadColumn { readonly name: string; readonly type: string }
export interface ReadFilter { readonly column: ReadColumn; readonly operation: 'eq' | 'ge' | 'lt'; readonly value: unknown }
export interface ReadPlan {
  readonly columns: readonly ReadColumn[]
  readonly filters: readonly ReadFilter[]
  readonly order: readonly ReadColumn[]
  readonly descending: boolean
  readonly after: readonly unknown[] | null
  readonly limit: number
}
export interface ScanPage {
  readonly rows: readonly Row[]
  readonly nextAfter: Readonly<Row> | null
}
export interface Queryable {
  selectRows(table: string, plan: ReadPlan): Promise<Row[]>
  countRows(table: string, plan: ReadPlan): Promise<bigint>
}
export interface ReadOptions {
  readonly where?: Readonly<Row> | null
  readonly bounds?: Range | null
  readonly orderBy?: string | null
  readonly descending?: boolean
  readonly after?: Readonly<Row> | null
  readonly limit?: number
  readonly paginate?: boolean
}

export function queryEngine(engine: object): Queryable {
  if (!('selectRows' in engine) || typeof engine.selectRows !== 'function' ||
      !('countRows' in engine) || typeof engine.countRows !== 'function') {
    throw new QueryRefused('this adapter does not support logical reads (selectRows/countRows)')
  }
  return engine as Queryable
}

function text(value: string): string {
  for (const character of value) {
    const point = character.codePointAt(0)!
    if (point >= 0xd800 && point <= 0xdfff) throw new QueryRefused('query strings must contain Unicode scalar values')
  }
  return value
}

interface DecimalValue { readonly text: string; readonly scale: number; readonly integerDigits: number }
function decimal(value: unknown): DecimalValue {
  if (typeof value === 'bigint' || (typeof value === 'number' && Number.isSafeInteger(value))) value = String(value)
  if (typeof value !== 'string') throw new QueryRefused('decimal query values require integer or decimal text')
  const source = value.trim().replace(/^([+-]?)\./, (_match, sign: string) => sign + '0.')
  const parts = /^([+-]?)(\d+)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(source)
  if (parts === null) throw new QueryRefused('invalid decimal query value')
  const fraction = parts[3] ?? '', exponent = Number(parts[4] ?? 0)
  if (!Number.isSafeInteger(exponent)) throw new QueryRefused('decimal query values may use at most 76 digits')
  let digits = (parts[2]! + fraction).replace(/^0+(?=\d)/, '')
  const effectiveScale = fraction.length - exponent
  const scale = Math.max(0, effectiveScale)
  const integerDigits = Math.max(0, digits.length - effectiveScale)
  if (integerDigits + scale > 76 || scale > 76) throw new QueryRefused('decimal query values may use at most 76 digits')
  if (effectiveScale < 0) digits += '0'.repeat(-effectiveScale)
  digits = digits.padStart(scale + 1, '0')
  const point = digits.length - scale
  const result = (parts[1] === '-' ? '-' : '') + (scale === 0 ? digits : digits.slice(0, point) + '.' + digits.slice(point))
  return { text: result, scale, integerDigits }
}

export function queryValue(column: ReadColumn, value: unknown): unknown {
  if (value === null) return null
  const kind = column.type
  if (kind.startsWith('decimal(')) return decimal(value).text
  if (kind === 'bool') {
    if (typeof value !== 'boolean') throw new QueryRefused('a boolean query value must be a bool')
    return value
  }
  if (kind === 'int32' || kind === 'int64') {
    if (typeof value !== 'bigint' && !(typeof value === 'number' && Number.isSafeInteger(value))) {
      throw new QueryRefused('an integer query value must be an exact integer')
    }
    const integer = BigInt(value as bigint | number), bits = kind === 'int32' ? 32n : 64n
    if (integer < -(2n ** (bits - 1n)) || integer >= 2n ** (bits - 1n)) throw new QueryRefused('integer query value is outside ' + kind)
    return kind === 'int32' ? Number(integer) : integer
  }
  if (kind === 'float32' || kind === 'float64') {
    if (typeof value !== 'number' || !Number.isFinite(value)) throw new QueryRefused('float query bounds must be finite')
    return value
  }
  if (kind === 'string') {
    if (typeof value !== 'string') throw new QueryRefused('a string query value must be text')
    return text(value)
  }
  if (kind === 'uuid') {
    if (typeof value !== 'string' || !/^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$/.test(value)) {
      throw new QueryRefused('a UUID query value must be canonical UUID text')
    }
    return value.toLowerCase()
  }
  if (kind === 'date') {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith('0000-')) {
      throw new QueryRefused('a date query value must be YYYY-MM-DD text')
    }
    const parsed = new Date(value + 'T00:00:00Z')
    if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value) throw new QueryRefused('invalid date query value')
    return value
  }
  if (kind === 'timestamp' || kind === 'timestamptz') {
    try {
      if (!(typeof value === 'string' || value instanceof Date || value instanceof Timestamp)) throw new Error()
      if (typeof value === 'string' && !/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2}(?::\d{2})?)?$/.test(value)) throw new Error()

      return Timestamp.from(value)
    } catch { throw new QueryRefused('a timestamp query value must be valid ISO text with at most six fractional digits, Date or Timestamp') }
  }
  if (kind === 'bytes' && value instanceof Uint8Array) return Buffer.from(value)
  throw new QueryRefused('query predicates are not supported for ' + kind)
}

export function isQueryMapping(value: unknown): value is Readonly<Row> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) &&
    (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null) &&
    Reflect.ownKeys(value).length === Object.keys(value).length
}

export function planRead(columns: readonly ReadColumn[], key: readonly string[], options: ReadOptions = {}): ReadPlan {
  const byName = new Map(columns.map(column => [column.name, column]))
  function field(name: string): ReadColumn {
    const column = byName.get(name)
    if (column === undefined) throw new QueryRefused('query refers to a field this entity does not declare')
    return column
  }
  const limit = options.limit === undefined ? 100 : options.limit
  const descending = options.descending === undefined ? false : options.descending
  if (!Number.isInteger(limit) || limit < 1 || limit > MAX_PAGE_ROWS) throw new QueryRefused('page limit must be an integer between 1 and ' + MAX_PAGE_ROWS)
  if (typeof descending !== 'boolean') throw new QueryRefused('descending must be a bool')
  const paginate = options.paginate !== false
  if (paginate && key.length === 0) throw new QueryRefused('a paginated read requires an entity key')
  const orderNames = [...(options.orderBy == null ? [] : [options.orderBy]), ...key.filter(name => name !== options.orderBy)]
  if (paginate && orderNames.length > MAX_ORDER_FIELDS) throw new QueryRefused('a read may order by at most ' + MAX_ORDER_FIELDS + ' fields')
  const order = paginate ? orderNames.map(field) : []
  if (order.some(column => ['json', 'float32', 'float64'].includes(column.type))) throw new QueryRefused('JSON and floating-point ordering are not supported by this read API')
  if (order.some(column => column.type.startsWith('decimal(') &&
      Number(column.type.slice('decimal('.length, -1).split(',')[0]) > 76)) {
    throw new QueryRefused('decimal ordering supports at most 76 digits')
  }
  const filters: ReadFilter[] = []
  if (options.where != null) {
    if (!isQueryMapping(options.where)) throw new QueryRefused('where must map logical field names to scalar values')
    for (const name of Object.keys(options.where).sort(compareCodePoints)) {
      const column = field(name)
      if (column.type === 'json') throw new QueryRefused('JSON predicates are not supported by this read API')
      filters.push({ column, operation: 'eq', value: queryValue(column, options.where[name]) })
    }
  }
  if (options.bounds != null) {
    const bounds = options.bounds
    if (!isQueryMapping(bounds) || typeof bounds.field !== 'string') throw new QueryRefused('bounds must be a Range')
    const column = field(bounds.field)
    if (!['int32', 'int64', 'float32', 'float64', 'decimal(', 'date', 'timestamp'].some(prefix => column.type.startsWith(prefix))) {
      throw new QueryRefused('this field has no range-read shape')
    }
    if (bounds.low == null && bounds.high == null) throw new QueryRefused('a range needs at least one bound')
    if (bounds.low != null) filters.push({ column, operation: 'ge', value: queryValue(column, bounds.low) })
    if (bounds.high != null) filters.push({ column, operation: 'lt', value: queryValue(column, bounds.high) })
  }
  let after: unknown[] | null = null
  if (options.after != null) {
    if (!paginate) throw new QueryRefused('an aggregate has no pagination position')
    if (!isQueryMapping(options.after) || Object.keys(options.after).length !== orderNames.length ||
        orderNames.some(name => !Object.hasOwn(options.after!, name))) {
      throw new QueryRefused('after must contain exactly the complete ordering key')
    }
    after = order.map(column => queryValue(column, options.after![column.name]))
  }
  return { columns: [...columns], filters, order, descending, after, limit }
}

function expression(column: ReadColumn, dialect: string): string {
  const quoted = QUOTE[dialect]!(column.name)
  if (column.type === 'string' && dialect === 'postgres') return '(' + quoted + ' COLLATE "C")'
  if (column.type === 'uuid' && dialect === 'clickhouse') return 'toString(' + quoted + ')'
  if (column.type === 'float32' || column.type === 'float64') {
    return dialect === 'postgres' ? 'CAST(' + quoted + ' AS double precision)' : 'toFloat64(' + quoted + ')'
  }
  return quoted
}

function comparison(column: ReadColumn, operator: string, value: unknown, dialect: string, parameter: (value: unknown) => string): string {
  let term = expression(column, dialect)
  if (value === null) return term + ' IS NULL'
  let bound: string
  if (column.type.startsWith('decimal(')) {
    const parts = column.type.slice('decimal('.length, -1).split(',').map(Number)
    const number = decimal(value), scale = Math.max(parts[1]!, number.scale)
    if (parts[0]! - parts[1]! + scale > 76) throw new QueryRefused('decimal comparison requires more than 76 digits')
    const target = dialect === 'postgres' ? 'numeric(76,' + scale + ')' : 'Nullable(Decimal(76,' + scale + '))'
    term = 'CAST(' + term + ' AS ' + target + ')'
    bound = 'CAST(' + parameter(number.text) + ' AS ' + target + ')'
  } else bound = parameter(value)
  let result = term + ' ' + operator + ' ' + bound
  if (['float32', 'float64'].includes(column.type) && operator !== '=') {
    const notNan = dialect === 'postgres' ? term + " <> 'NaN'::double precision" : 'NOT isNaN(' + term + ')'
    result = '(' + notNan + ' AND ' + result + ')'
  }
  return result
}

export function readSql(table: string, plan: ReadPlan, dialect: string, parameter: (value: unknown) => string, count = false): string {
  const operators = { eq: '=', ge: '>=', lt: '<' }
  const clauses = plan.filters.map(predicate => comparison(predicate.column, operators[predicate.operation], predicate.value, dialect, parameter))
  if (!count && plan.after !== null) {
    const alternatives: string[] = [], operator = plan.descending ? '<' : '>'
    for (let index = 0; index < plan.order.length; index++) {
      const column = plan.order[index]!, value = plan.after[index]
      if (value !== null) {
        const prefix = plan.order.slice(0, index).map((previous, position) => comparison(previous, '=', plan.after![position], dialect, parameter))
        const later = comparison(column, operator, value, dialect, parameter)
        alternatives.push('(' + [...prefix, '(' + expression(column, dialect) + ' IS NULL OR ' + later + ')'].join(' AND ') + ')')
      }
    }
    clauses.push(alternatives.length ? '(' + alternatives.join(' OR ') + ')' : 'FALSE')
  }
  const where = clauses.length ? ' WHERE ' + clauses.join(' AND ') : ''
  const quote = QUOTE[dialect]!, from = ' FROM ' + quote(table) + (dialect === 'clickhouse' ? ' FINAL' : '')
  if (count) return 'SELECT ' + (dialect === 'postgres' ? 'CAST(count(*) AS text)' : 'toString(count())') + ' AS sde_count' + from + where
  // JSON transports otherwise lose non-finite floating values before a caller can see them.
  const projection = plan.columns.map(column => {
    const name = quote(column.name)
    if (dialect === 'clickhouse' && ['timestamp', 'timestamptz'].includes(column.type)) {
      return "toTimeZone(" + name + ", 'UTC') AS " + name
    }
    if (column.type.startsWith('decimal(')) {
      return (dialect === 'postgres' ? 'CAST(' + name + ' AS text)' : 'toString(' + name + ')') + ' AS ' + name
    }
    return dialect === 'clickhouse' && ['float32', 'float64'].includes(column.type)
      ? 'toString(' + name + ') AS ' + name : name
  }).join(', ')
  const direction = plan.descending ? 'DESC' : 'ASC'
  const order = plan.order.map(column => expression(column, dialect) + ' ' + direction + ' NULLS LAST').join(', ')
  return 'SELECT ' + projection + from + where + ' ORDER BY ' + order + ' LIMIT ' + parameter(plan.limit + 1)
}

export interface NumericSummary {
  readonly count: bigint
  readonly nonNullCount: bigint
  readonly minimum: bigint | number | string | null
  readonly maximum: bigint | number | string | null
  readonly total: bigint | string | null
  readonly mean: string | null
}
export interface Summarizable {
  summarizeRows(table: string, plan: ReadPlan, column: ReadColumn): Promise<Readonly<Row>>
}
export function summaryEngine(engine: object): Summarizable {
  if (!('summarizeRows' in engine) || typeof engine.summarizeRows !== 'function') {
    throw new QueryRefused('this adapter does not support numeric summaries (summarizeRows)')
  }
  return engine as Summarizable
}
export function summaryScale(column: ReadColumn, meanScale: number): number {
  if (!Number.isInteger(meanScale) || meanScale < 0 || meanScale > 38) throw new QueryRefused('meanScale must be an integer between 0 and 38')
  if (column.type === 'int32' || column.type === 'int64') return 0
  if (column.type.startsWith('decimal(')) {
    const [precision, scale] = column.type.slice('decimal('.length, -1).split(',').map(Number)
    if (precision! <= 56) return scale!
  }
  throw new QueryRefused('summaries require int32, int64 or decimal precision up to 56')
}
export function summarySql(table: string, plan: ReadPlan, column: ReadColumn, dialect: string, parameter: (value: unknown) => string): string {
  const scale = summaryScale(column, 0), quote = QUOTE[dialect]!, name = quote(column.name)
  const target = dialect === 'postgres' ? 'numeric(76,' + scale + ')' : 'Nullable(Decimal(76,' + scale + '))'
  const text = (value: string) => dialect === 'postgres' ? 'CAST(' + value + ' AS text)' : 'toString(' + value + ')'
  const fields = [
    text('count(*)') + ' AS sde_count', text('count(' + name + ')') + ' AS sde_present',
    text('min(' + name + ')') + ' AS sde_min', text('max(' + name + ')') + ' AS sde_max',
    text('sum(CAST(' + name + ' AS ' + target + '))') + ' AS sde_total',
  ]
  const operators = { eq: '=', ge: '>=', lt: '<' }
  const clauses = plan.filters.map(item => comparison(item.column, operators[item.operation], item.value, dialect, parameter))
  return 'SELECT ' + fields.join(', ') + ' FROM ' + quote(table) + (dialect === 'clickhouse' ? ' FINAL' : '') +
    (clauses.length ? ' WHERE ' + clauses.join(' AND ') : '')
}
function decimalInteger(value: DecimalValue, scale: number): bigint {
  let integer = BigInt(value.text.replace('.', ''))
  const shift = scale - value.scale
  if (shift < 0) {
    const divisor = 10n ** BigInt(-shift)
    if (integer % divisor !== 0n) throw new Error('summary result has fractional digits outside the stored scale')
    integer /= divisor
  } else integer *= 10n ** BigInt(shift)
  return integer
}
export function numericSummary(record: Readonly<Row>, column: ReadColumn, meanScale: number): NumericSummary {
  const scale = summaryScale(column, meanScale)
  const count = BigInt(String(record['sde_count'])), nonNullCount = BigInt(String(record['sde_present']))
  if (nonNullCount < 0n || count < nonNullCount) throw new Error('summary counts are inconsistent')
  if (nonNullCount === 0n) return { count, nonNullCount, minimum: null, maximum: null, total: null, mean: null }
  const minimum = decimal(record['sde_min']), maximum = decimal(record['sde_max']), total = decimal(record['sde_total'])
  const unscaled = decimalInteger(total, scale), absolute = unscaled < 0n ? -unscaled : unscaled
  const numerator = absolute * 10n ** BigInt(meanScale), denominator = nonNullCount * 10n ** BigInt(scale)
  let rounded = numerator / denominator
  const remainder = numerator % denominator
  if (remainder * 2n > denominator || (remainder * 2n === denominator && rounded % 2n !== 0n)) rounded += 1n
  const digits = rounded.toString().padStart(meanScale + 1, '0'), point = digits.length - meanScale
  const mean = (unscaled < 0n ? '-' : '') + (meanScale === 0 ? digits : digits.slice(0, point) + '.' + digits.slice(point))
  if (column.type === 'int32' || column.type === 'int64') {
    const low = decimalInteger(minimum, 0), high = decimalInteger(maximum, 0)
    return { count, nonNullCount, minimum: column.type === 'int32' ? Number(low) : low,
      maximum: column.type === 'int32' ? Number(high) : high, total: decimalInteger(total, 0), mean }
  }
  function scaled(value: DecimalValue): string {
    const coefficient = decimalInteger(value, scale), absolute = coefficient < 0n ? -coefficient : coefficient
    const digits = absolute.toString().padStart(scale + 1, '0'), point = digits.length - scale
    return (coefficient < 0n ? '-' : '') + (scale === 0 ? digits : digits.slice(0, point) + '.' + digits.slice(point))
  }
  return { count, nonNullCount, minimum: scaled(minimum), maximum: scaled(maximum), total: scaled(total), mean }
}

/** Preserve declared Decimal scale without routing the value through Number. */
export function readRow(columns: readonly ReadColumn[], row: Readonly<Row>): Row {
  const result = { ...row }
  for (const column of columns) {
    const value = result[column.name]
    if (value !== null && column.type.startsWith('decimal(')) {
      const scale = Number(column.type.slice('decimal('.length, -1).split(',')[1])
      const coefficient = decimalInteger(decimal(value), scale)
      const absolute = coefficient < 0n ? -coefficient : coefficient
      const digits = absolute.toString().padStart(scale + 1, '0'), point = digits.length - scale
      result[column.name] = (coefficient < 0n ? '-' : '') +
        (scale === 0 ? digits : digits.slice(0, point) + '.' + digits.slice(point))
    }
  }
  return result
}

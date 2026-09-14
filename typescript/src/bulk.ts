/** Bounded application inserts; deliberately separate from idempotent migration copies. */
import { compareCodePoints } from './canonical.js'
import { BulkWriteRefused } from './errors.js'
import type { Row } from './session.js'
import { Timestamp } from './timestamp.js'

export const MAX_BATCH_ROWS = 1000
export const MAX_BATCH_VALUES = 60_000

export interface BulkWritable {
  insertMany(table: string, rows: readonly Readonly<Row>[]): Promise<void>
}

export function bulkWriter(engine: object): BulkWritable {
  if (!('insertMany' in engine) || typeof engine.insertMany !== 'function') {
    throw new BulkWriteRefused('this adapter does not support bulk writes (insertMany)')
  }
  return engine as BulkWritable
}

export function batchColumns(rows: readonly Readonly<Row>[], extraColumns = 0): string[] {
  if (!Array.isArray(rows)) throw new BulkWriteRefused('a batch must be an array of row objects')
  if (rows.length > MAX_BATCH_ROWS) {
    throw new BulkWriteRefused(`a batch may contain at most ${MAX_BATCH_ROWS} rows`)
  }
  let columns: string[] | undefined
  for (const row of rows) {
    if (row === null || typeof row !== 'object' || Array.isArray(row) ||
        (Object.getPrototypeOf(row) !== Object.prototype && Object.getPrototypeOf(row) !== null)) {
      throw new BulkWriteRefused('each batch row must be a nonempty object with string fields')
    }
    const here = Object.keys(row).sort(compareCodePoints)
    if (here.length === 0 || Reflect.ownKeys(row).length !== here.length) {
      throw new BulkWriteRefused('each batch row must be a nonempty object with string fields')
    }
    if (columns === undefined) {
      columns = here
      if (rows.length * (columns.length + extraColumns) > MAX_BATCH_VALUES) {
        throw new BulkWriteRefused(`a batch may contain at most ${MAX_BATCH_VALUES} values, including generation`)
      }
    } else if (here.length !== columns.length || columns.some((field, i) => field !== here[i])) {
      throw new BulkWriteRefused('all batch rows must have the same fields')
    }
  }
  return columns ?? []
}

function snapshot(value: unknown, active: Set<object>): unknown {
  if (value === null || ['string', 'boolean', 'number', 'bigint'].includes(typeof value)) return value
  if (value instanceof Timestamp) return value // Immutable; structuredClone would erase its type.
  if (value instanceof Date) return new Date(value.getTime())
  if (Buffer.isBuffer(value)) return Buffer.from(value)
  if (value instanceof Uint8Array) return new Uint8Array(value)
  if (typeof value !== 'object' || value === null) {
    throw new BulkWriteRefused('batch values must use the SDK scalar types or JSON containers')
  }
  if (active.has(value)) throw new BulkWriteRefused('batch values must not contain cycles')
  active.add(value)
  try {
    if (Array.isArray(value)) return Array.from(value, (item: unknown) => snapshot(item, active))
    if (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null) {
      if (Reflect.ownKeys(value).length !== Object.keys(value).length) {
        throw new BulkWriteRefused('JSON containers must have enumerable string keys')
      }
      return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, snapshot(item, active)]))
    }
    throw new BulkWriteRefused('batch values must use the SDK scalar types or JSON containers')
  } finally {
    active.delete(value)
  }
}

export function snapshotRows(rows: readonly Readonly<Row>[]): Row[] {
  try {
    return rows.map((row) => Object.fromEntries(
      Object.entries(row).map(([key, value]) => [key, snapshot(value, new Set())]),
    ))
  } catch (error) {
    if (error instanceof RangeError) throw new BulkWriteRefused('batch values are nested too deeply')
    throw error
  }
}

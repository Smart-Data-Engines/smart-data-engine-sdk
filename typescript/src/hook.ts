/**
 * What a session tells the application about each operation, when the application asks.
 *
 * A session opened with a `hook` calls it once for every operation it completes - with what the
 * operation was, where it went, how long it took, how many rows it moved and how it ended - and with
 * nothing the operation carried: no key, no row, no filter, no statement, no connection string and
 * no error message, since a driver's message can quote a key (docs/observability.md). The hook runs
 * after the operation, before its promise settles; one that throws is counted in
 * `internalFailures()` and changes nothing about the operation's result.
 *
 * The library depends on no tracing or metrics package. Turning a record into a span or a histogram
 * is a few lines of the application's own code, shown in docs/observability.md.
 */
import { SdeError } from './errors.js'

/** The methods a record can name; `transaction` is a whole transaction, from open to its end. */
export const OPERATIONS = [
  'book_at', 'book_stream', 'count', 'get', 'save', 'save_many', 'scan', 'search', 'summarize',
  'transaction',
] as const
export type Operation = (typeof OPERATIONS)[number]

/** One completed operation. Fields in the contract's order (`observability/001`), camelCased. */
export interface OperationRecord {
  /** The session method, in the contract's spelling: one of {@link OPERATIONS}. */
  readonly operation: Operation
  /** The entity, in the application's own names even when the model is hashed. */
  readonly entity: string
  /** The colocation group, also in the application's names. */
  readonly group: string
  /** The operation shape's id, as telemetry names it; empty for a transaction. */
  readonly shape: string
  /** The engine's name in the placement map - never its address. */
  readonly engine: string
  /** `postgres`, `clickhouse`, `elasticsearch` or `orderbook`. */
  readonly dialect: string
  /** The id of the materialization that served the operation. */
  readonly materialization: string
  /** How long it took, on the monotonic clock. A span's start is the time it ended minus this. */
  readonly durationNs: number
  /** Rows written or returned; 1 for a count or a summary. */
  readonly rows: number
  /** `ok`, the class of an error of the library's hierarchy, or `unexpected`. */
  readonly outcome: string
}

export type OperationHook = (record: OperationRecord) => void

/** The record's fields, in order. */
export const FIELDS = [
  'operation', 'entity', 'group', 'shape', 'engine', 'dialect', 'materialization', 'durationNs',
  'rows', 'outcome',
] as const

/**
 * `ok`, the class name of an error the library throws, or `unexpected` for anything else - a class
 * name is all that leaves, because a message can hold a value.
 */
export function outcomeOf(error: unknown): string {
  if (error === undefined) return 'ok'
  return error instanceof SdeError ? error.name : 'unexpected'
}

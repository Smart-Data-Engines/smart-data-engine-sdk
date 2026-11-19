/**
 * Structured events, through a sink the application passes in.
 *
 * There is no global logger, and a library given no sink says nothing (tests/silence.test.ts): a
 * library that prints is a library people patch. Every event has a name from a closed vocabulary -
 * the reference's, so one grep finds a line from any language - and its fields are structure:
 * versions, the names of groups, engines and tables, counts, error classes. Never a row's values.
 *
 * The vocabulary is the C++ library's (`LOG_EVENTS` in cpp/include/sde/log.hpp), a subset of the
 * reference's `sde.logging.EVENTS`: a routing decision, a model built and a telemetry window are not
 * events here, as they are not there. A test holds the list to the `emit` calls in the sources both
 * ways, and to the C++ list.
 */
import { guard } from './internal.js'

/** Receives one event: its name and an object of fields. */
export type EventSink = (event: string, fields: Readonly<Record<string, unknown>>) => void

export const EVENTS = [
  'sde.map.loaded',
  'sde.map.rejected',
  'sde.map.forward_only',
  'sde.map.rollback_unprotected',
  'sde.schema.applied',
  'sde.schema.extra_columns',
  'sde.schema.text_collation',
  'sde.schema.physical_mismatch',
  'sde.write.failed',
  'sde.orderbook.flushed',
  'sde.migration.divergence',
  'sde.migration.backfill_progress',
  'sde.telemetry.storage_unavailable',
] as const
export type EventName = (typeof EVENTS)[number]

/**
 * Hand one event to the sink, if there is one. A sink that throws changes nothing about the
 * operation it reports on: it is counted in `internalFailures()` under `events.sink`.
 */
export function emit(sink: EventSink | undefined, event: EventName, fields: Record<string, unknown>): void {
  if (sink === undefined) return
  const frozen = Object.freeze({ ...fields })
  guard('events.sink', () => sink(event, frozen))
}

/** At most 200 code points of a message, as the reference cuts a refusal's reason. */
export function truncated(message: string): string {
  return [...message].slice(0, 200).join('')
}

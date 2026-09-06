/**
 * The boundary between our bugs and the client's uptime.
 *
 * This library runs inside somebody else's application. A defect in our telemetry or our
 * aggregation must not take down their request, and the honest way to guarantee that is to route
 * every purely-internal side effect through one function that cannot throw.
 *
 * The hard part is not the try/catch. It is deciding what counts as internal, and getting that
 * wrong in either direction is bad: swallow too little and a bug in a counter becomes a customer's
 * outage; swallow too much and a write that never happened is reported as success, which is the
 * worst thing this library could do.
 *
 * So the rule is narrow and stated once here: **internal means it cannot change whether the
 * client's operation was performed correctly.** Aggregating a telemetry window is internal.
 * Choosing which materialisation a read goes to is *not* - a wrong route returns wrong data.
 * Writing a row is not. Verifying a placement map's signature is not, because the map decides where
 * data is written.
 *
 * A failure is **counted**, and that counter is what makes this honest: a library that swallows
 * silently is indistinguishable from one that works. It is not logged, unlike the reference
 * implementation, and the difference is deliberate - this library has no logging channel at all
 * (see `tests/silence.test.ts`), so a line here would go to whatever the client's process prints
 * with no handler to turn it off. The counter is readable instead.
 */

const failures = new Map<string, number>()

/**
 * How many times each guarded operation has failed, by name.
 *
 * Exposed rather than hidden. Swallowing a failure and leaving no trace of it would make this
 * library indistinguishable from one that works, and a client should be able to see that our code
 * is misbehaving inside their process even when we cannot tell them.
 */
export function internalFailures(): Record<string, number> {
  return Object.fromEntries([...failures.entries()].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)))
}

/** For tests. */
export function resetInternalFailures(): void {
  failures.clear()
}

/**
 * Run a purely-internal operation. Never throws.
 *
 * `what` names the operation and becomes the key in {@link internalFailures}, so it should be
 * stable across releases - a client may be alerting on it. The names match the reference
 * implementation's, because a client running both languages has one dashboard.
 */
export function guard<T>(what: string, operation: () => T): T | undefined {
  try {
    return operation()
  } catch {
    failures.set(what, (failures.get(what) ?? 0) + 1)
    return undefined
  }
}

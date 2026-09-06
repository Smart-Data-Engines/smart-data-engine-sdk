/**
 * Refusing a placement map that goes backwards, and the durable state that makes it possible.
 *
 * A signed map for version 3 verifies correctly forever - that is what a signature is. So replacing
 * the client's map file with an older signed one loads cleanly, routes writes to the previous
 * placement, and **nothing protests**. Today that costs a client a stale schema. Once the migration
 * state travels in the map, it costs them writes: a library reverted from dual-write to
 * single-write in the middle of a migration drops exactly the rows the migration exists not to
 * drop.
 *
 * Refusing it needs one thing a library has never had: **memory**. Everything else here is a pure
 * function of a document, a model and a key, which is why it can be verified by reading it. This
 * module is the exception, and each of the three obvious places to keep that memory is worse than
 * the one chosen:
 *
 * - **in the process** protects until the first restart, and a restart is when a swapped file is
 *   read. A protection that lapses exactly when it is needed;
 * - **in a file** needs a configured path, and in a container that path is usually ephemeral - so it
 *   degrades to the first option while continuing to look like the third. The worst property
 *   available: a guarantee present in the code and absent in production;
 * - **with us** would mean the library asking our service whether it may start, which is the one
 *   thing this product promises it will never need to do. Our outage would become the client's.
 *
 * So it lives **in the client's own engines**, in a table this library owns. Four properties, and
 * the first two are what make it safe rather than merely present.
 *
 * **Append-only, and the watermark is `max(map_version)`.** No update, no key enforcement, no
 * row-level contention - and therefore identical semantics in PostgreSQL and in ClickHouse, which is
 * the engine that has no unique constraint to offer. A stale row can never lower the bar.
 *
 * **Every participating engine is written, and the watermark is the maximum over all of them.**
 * Losing an engine cannot lose the protection, and one engine lagging cannot weaken it.
 *
 * **An engine that cannot store it does not participate, and that is reported rather than hidden.**
 * A protection whose state cannot be read is a protection taken on trust.
 *
 * **Only signed maps are checked.** An unsigned map is the client's own document: hand-writing one
 * and pointing a library at it is the no-account mode, and their business what they replace it with.
 * In pure no-account mode this module does nothing at all - no table, no query, no cost.
 *
 * The escape hatch is deliberately not a parameter. A legitimate rollback - we issued a bad map -
 * means clearing the bookkeeping, and the refusal says how. A parameter called `allowRollback` would
 * be set once during an incident and left set.
 */

import { compareCodePoints } from './canonical.js'
import { satisfies, WATERMARK_MEMBERS } from './capabilities.js'
import { MapRolledBack } from './errors.js'
import type { PlacementMap } from './placement.js'
import { WATERMARK_TABLE } from './placement.js'

export type Protection = 'enforced' | 'unavailable' | 'not_applicable'

/**
 * What an engine adapter offers to take part.
 *
 * Separate from the engine interface, and optional. Requiring these of every adapter would break
 * every one anybody has written - including the fakes in somebody else's test suite - for a
 * capability one of our own engines cannot provide anyway. So participation is discovered rather
 * than required, and non-participation is a reportable state instead of a crash.
 */
export interface WatermarkStore {
  mapWatermark(): Promise<number | null>
  recordMapVersion(version: number, options: { readonly modelVersion: string }): Promise<void>
}

/**
 * A compile-time ratchet between the interface and the list the runtime check uses.
 *
 * Add a method to `WatermarkStore` and forget `WATERMARK_MEMBERS`, and this stops being assignable.
 * The runtime list cannot be derived from the interface - types are erased - so the compiler holds
 * the other direction instead.
 */
export const WATERMARK_STORE_IS_TOTAL: (typeof WATERMARK_MEMBERS)[number] extends keyof WatermarkStore
  ? keyof WatermarkStore extends (typeof WATERMARK_MEMBERS)[number]
    ? true
    : never
  : never = true

/**
 * What the check did, in a form a client can assert on.
 *
 * Exposed rather than kept private on purpose. A protection whose state cannot be read is a
 * protection taken on trust, and this product's whole argument is that its guarantees are checkable
 * by reading the code and now by reading this.
 */
export interface WatermarkCheck {
  readonly protection: Protection
  readonly mapVersion: number
  readonly highestSeen: number | null
  readonly participating: readonly string[]
  readonly unable: readonly string[]
  readonly why: string
}

export function watermarkRecord(check: WatermarkCheck): Record<string, unknown> {
  return {
    protection: check.protection,
    map_version: check.mapVersion,
    highest_seen: check.highestSeen,
    participating: [...check.participating],
    unable: [...check.unable],
    why: check.why,
  }
}

function split(engines: Readonly<Record<string, unknown>>): {
  able: string[]
  unable: string[]
} {
  const able: string[] = []
  const unable: string[] = []
  for (const name of Object.keys(engines).sort(compareCodePoints)) {
    if (satisfies(engines[name], WATERMARK_MEMBERS)) able.push(name)
    else unable.push(name)
  }
  return { able, unable }
}

/**
 * Refuse a signed map older than the newest one these engines have seen.
 *
 * Throws {@link MapRolledBack}. Equal is allowed - restarting a process against the same map is the
 * ordinary case - and only strictly lower is refused.
 *
 * Asynchronous because reading a watermark is a query, and split in two inside: this gathers the
 * numbers and {@link decide} makes the decision from them. The decision is therefore a pure
 * function of a document and a set of integers, which is what lets the `migration/` vectors check
 * it, and what lets a reader confirm by reading that nothing here depends on the order the engines
 * answered in.
 *
 * **Nothing is read for an unsigned map.** The no-account mode promises no table, no query and no
 * cost, and this function gathered every watermark and *then* asked whether the map was signed
 * until `migration/001` existed - the right answer, with the promise broken, which is the one shape
 * of defect a record of the decision cannot show.
 */
export async function enforceForwardOnly(
  placement: PlacementMap,
  engines: Readonly<Record<string, unknown>>,
): Promise<WatermarkCheck> {
  if (!placement.signed) return decide(placement, engines, new Map())
  const { able } = split(engines)
  const seen = new Map<string, number | null>()
  for (const name of able) {
    const store = engines[name] as WatermarkStore
    seen.set(name, await store.mapWatermark())
  }
  const check = decide(placement, engines, seen)
  if (
    check.protection === 'enforced' &&
    (check.highestSeen === null || placement.mapVersion > check.highestSeen)
  ) {
    // Written only when it moves, and only after the decision - so a refusal writes nothing.
    // Recording every start would grow the table by one row per process restart and the watermark
    // would say nothing more than it does now.
    for (const name of check.participating) {
      const store = engines[name] as WatermarkStore
      await store.recordMapVersion(placement.mapVersion, { modelVersion: placement.modelVersion })
    }
  }
  return check
}

function decide(
  placement: PlacementMap,
  engines: Readonly<Record<string, unknown>>,
  seen: ReadonlyMap<string, number | null>,
): WatermarkCheck {
  if (!placement.signed) {
    return {
      protection: 'not_applicable',
      mapVersion: placement.mapVersion,
      highestSeen: null,
      participating: [],
      unable: Object.keys(engines).sort(compareCodePoints),
      why:
        'this map is unsigned, so it is your own document rather than one we issued. Replacing it ' +
        'with another is the no-account mode working as documented, and there is no newest ' +
        'version for us to be the authority on.',
    }
  }

  const { able, unable } = split(engines)
  if (able.length === 0) {
    return {
      protection: 'unavailable',
      mapVersion: placement.mapVersion,
      highestSeen: null,
      participating: [],
      unable,
      why:
        `none of the engines in this map can keep bookkeeping ([${unable.join(', ')}]), so a map ` +
        `that goes backwards cannot be recognised. An engine whose schema is fixed in its own ` +
        `source - ours is - has nowhere to put it. Nothing is wrong with your configuration; this ` +
        `protection simply does not exist for it.`,
    }
  }

  const known = able
    .map((name) => seen.get(name) ?? null)
    .filter((value): value is number => value !== null)
  const highest = known.length === 0 ? null : Math.max(...known)

  if (highest !== null && placement.mapVersion < highest) {
    throw new MapRolledBack(
      `this map is version ${placement.mapVersion} and version ${highest} has already been ` +
        `applied against these engines. Refusing to go backwards: an older signed map verifies ` +
        `perfectly - that is what a signature is - so nothing else here would notice that the ` +
        `file was replaced, and the writes would go to the previous placement. If this is ` +
        `deliberate, because the newer map was wrong, clear the bookkeeping: DELETE FROM ` +
        `${WATERMARK_TABLE} WHERE map_version > ${placement.mapVersion}; in ` +
        `[${able.join(', ')}], and on an engine that deletes asynchronously, let the deletion ` +
        `finish before restarting. That is a deliberate act with a stated consequence, which is ` +
        `why it is not a flag.`,
    )
  }

  return {
    protection: 'enforced',
    mapVersion: placement.mapVersion,
    highestSeen: highest,
    participating: able,
    unable,
    why:
      `the highest map version applied against these engines is ` +
      `${highest ?? placement.mapVersion}, kept in ${WATERMARK_TABLE} in [${able.join(', ')}]. ` +
      `A map older than that is refused.`,
  }
}

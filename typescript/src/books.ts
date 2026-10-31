/**
 * A book stream: one side of an order book at a time, sent whole or as its changes.
 *
 * The orderbook engine reads an update by price (its #233): each level sets the quantity of its
 * price, a quantity of 0 removes the price, and a price the update does not name stays. So a writer
 * of books can send two things, and a {@link BookStream} sends either:
 *
 * - `mode: 'full'`: every side the application gives is sent whole, as a side replacement - the
 *   engine removes every price the side holds and the update does not name (`MREPLACE`);
 * - `mode: 'incremental'`: only what changed is sent, as an ordinary update. From whole sides,
 *   through {@link BookStream.update}, the stream computes the difference against the side it last
 *   sent; from changes the application already has - an exchange's diff stream - through
 *   {@link BookStream.apply}. The engine stores the changes and composes the book from them.
 *
 * An incremental stream sends a side whole instead of its changes:
 *
 * - the first time it sends the side, and after a send that failed, when it does not know what the
 *   engine holds;
 * - every `resync` updates or seconds, whichever comes first, which heals a difference nobody
 *   noticed - a change the application missed;
 * - when the whole side is smaller than its changes. This also keeps an update within the engine's
 *   `maxLevels`: two sides of 1000 prices can differ in 2000.
 *
 * Two rules hold in both modes. A side holds at most `maxLevels` prices, the engine's depth:
 * `update()` refuses a deeper side, and a side `apply()` grows deeper is sent whole as its best
 * `maxLevels`. And a side's times never go back: the engine's history applies updates in time order
 * and its live book in the order they arrive, so an update earlier than the last one sent would
 * make the two disagree. A refused call sends nothing and changes nothing.
 *
 * The reference is `sde.books` in the Python library, and the shared vectors in
 * `conformance/vectors/books/` hold both to the same differences and the same sends.
 */

import { ResourceBusy } from './errors.js'

export type Side = 'bid' | 'ask'
/** One level: price, quantity, order count - the engine's integers. */
export type Level = readonly [bigint, bigint, bigint]
/** A level as an application may give it: each integer a bigint, or a number within 2^53. */
export type LevelInput = readonly [bigint | number, bigint | number, bigint | number]

export const SIDES: readonly Side[] = Object.freeze(['bid', 'ask'] as const)
export const MODES = Object.freeze(['full', 'incremental'] as const)
export type Mode = (typeof MODES)[number]
/** The orderbook engine's depth: the prices a side holds, and the levels one update carries. */
export const ENGINE_DEPTH = 1000
const MAX_TIMESTAMP_NS = (1n << 63n) - 1n

type SideBook = Map<bigint, readonly [bigint, bigint]>

function describe(value: unknown): string {
  if (typeof value === 'bigint') return `${value}n`
  if (typeof value === 'string') return JSON.stringify(value)
  if (Array.isArray(value)) return `[${value.map(describe).join(', ')}]`
  return String(value)
}

function compare(left: bigint, right: bigint): number {
  return left < right ? -1 : left > right ? 1 : 0
}

/** Prices in the side's own order: bids from the highest, asks from the lowest. */
function bestFirst(side: Side, prices: readonly bigint[]): bigint[] {
  const sorted = [...prices].sort(compare)
  return side === 'bid' ? sorted.reverse() : sorted
}

function sideOf(side: unknown): Side {
  if (side === 'bid' || side === 'ask') return side
  throw new RangeError(`side must be 'bid' or 'ask', not ${describe(side)}`)
}

function integer(value: unknown, where: string, name: string): bigint {
  if (typeof value === 'bigint') return value
  if (typeof value === 'number' && Number.isSafeInteger(value)) return BigInt(value)
  throw new RangeError(`${where}${name} is ${describe(value)}, which is not an integer`)
}

function level(raw: unknown, where: string): Level {
  if (!Array.isArray(raw) || raw.length !== 3) {
    throw new RangeError(`${where}is ${describe(raw)}: a level is (price, quantity, order count)`)
  }
  const price = integer(raw[0], where, 'price')
  const quantity = integer(raw[1], where, 'quantity')
  const count = integer(raw[2], where, 'order count')
  if (quantity < 0n || count < 0n) throw new RangeError(`${where}has a negative quantity or order count`)
  return [price, quantity, count]
}

/** A whole side, checked: each price once, none with a quantity of 0. */
function wholeSide(side: Side, levels: readonly unknown[]): SideBook {
  const out: SideBook = new Map()
  levels.forEach((raw, index) => {
    const [price, quantity, count] = level(raw, `${side} level ${index} `)
    if (quantity === 0n) {
      throw new RangeError(
        `${side} level ${index} has a quantity of 0. A whole side lists the prices it holds; a removal is a ` +
          'change, which apply() sends',
      )
    }
    if (out.has(price)) throw new RangeError(`${side} price ${price} is in the side twice`)
    out.set(price, [quantity, count])
  })
  return out
}

function levelsOf(side: Side, book: SideBook): Level[] {
  return bestFirst(side, [...book.keys()]).map((price) => {
    const [quantity, count] = book.get(price)!
    return [price, quantity, count] as const
  })
}

/**
 * What changes `before` into `after`, one side of a book, as the engine applies an update.
 *
 * First a quantity of 0 for every price of `before` that `after` does not hold, best first; then
 * every price of `after` that `before` does not hold, or holds with another quantity or count, best
 * first. The removals go first because the engine applies an update in its order, and a full side -
 * 1000 prices - has no room for a price it does not hold until they are applied.
 *
 * Empty when the two sides are the same: an update that changes nothing is not sent.
 */
export function bookDiff(side: string, before: readonly LevelInput[], after: readonly LevelInput[]): Level[] {
  const s = sideOf(side)
  const old = wholeSide(s, before)
  const now = wholeSide(s, after)
  const removals = bestFirst(s, [...old.keys()].filter((price) => !now.has(price)))
    .map((price) => [price, 0n, 0n] as const)
  const changes = bestFirst(s, [...now.keys()].filter((price) => {
    const held = old.get(price)
    const given = now.get(price)!
    return held === undefined || held[0] !== given[0] || held[1] !== given[1]
  })).map((price) => [price, now.get(price)![0], now.get(price)![1]] as const)
  return [...removals, ...changes]
}

/**
 * Changes as the application has them - a quantity of 0 a removal - in the order a stream sends
 * them: the removals best first, then the rest best first. Each price once.
 */
export function orderedChanges(side: string, changes: readonly LevelInput[]): Level[] {
  const s = sideOf(side)
  const seen = new Map<bigint, Level>()
  changes.forEach((raw, index) => {
    const change = level(raw, `${s} change ${index} `)
    if (seen.has(change[0])) throw new RangeError(`${s} price ${change[0]} is changed twice in one update`)
    seen.set(change[0], change)
  })
  const removals = bestFirst(s, [...seen.values()].filter((change) => change[1] === 0n).map((change) => change[0]))
  const rest = bestFirst(s, [...seen.values()].filter((change) => change[1] !== 0n).map((change) => change[0]))
  return [...removals.map((price) => [price, 0n, 0n] as const), ...rest.map((price) => seen.get(price)!)]
}

/**
 * When an incremental stream sends a side whole: after `updates` updates of the side since it last
 * did, or `seconds` since then, whichever comes first. `0` turns that clause off.
 */
export class Resync {
  readonly updates: number
  readonly seconds: number

  constructor(options: { readonly updates?: number; readonly seconds?: number } = {}) {
    const updates = options.updates ?? 1000
    const seconds = options.seconds ?? 60
    if (typeof updates !== 'number' || !Number.isSafeInteger(updates) || updates < 0) {
      throw new RangeError(`resync updates must be a non-negative integer, not ${describe(updates)}`)
    }
    if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) {
      throw new RangeError(`resync seconds must be a non-negative number, not ${describe(seconds)}`)
    }
    this.updates = updates
    this.seconds = seconds
    Object.freeze(this)
  }
}

/** One update of a book by price: its levels, in the order the engine applies them. */
export interface BookUpdate {
  readonly symbol: string
  readonly exchange: string
  readonly side: string
  readonly timestampNs: bigint | number
  readonly levels: readonly (readonly [bigint | number, bigint | number, bigint | number])[]
}

/** A book as the engine composes it at an instant: each side best first. */
export interface ComposedBook {
  bid: Level[]
  ask: Level[]
}

/**
 * What an engine that takes books by price offers - the orderbook engine's adapter - named here so
 * that the session reaches it without importing an adapter.
 */
export interface BookEngine {
  supportsReplaceSide(): boolean
  checkBook(symbol: string, exchange: string): void
  applyLevels(table: string, update: BookUpdate): Promise<void>
  replaceLevels(table: string, update: BookUpdate): Promise<void>
  bookAt(
    table: string,
    request: { readonly symbol: string; readonly exchange: string; readonly atNs: bigint | number; readonly depth?: number },
  ): Promise<ComposedBook>
}

/** Where a stream's updates go: the orderbook engine, through a session. */
export interface BookSink {
  sendWhole(side: Side, levels: readonly Level[], timestampNs: bigint): Promise<void>
  sendChanges(side: Side, levels: readonly Level[], timestampNs: bigint): Promise<void>
}

interface SideState {
  book: SideBook
  sentWhole: boolean   // the side has been sent whole since the stream began
  unsure: boolean      // a send failed: what the engine holds is not known
  sinceWhole: number   // updates since the side was last sent whole
  wholeAt: number      // the policy clock when it was
  lastNs: bigint       // the time of the last update accepted for the side; -1 before one
}

/** What a stream sent: updates and levels, whole and as changes - what a transfer is made of. */
export interface StreamStats {
  updates: number        // update() and apply() calls accepted
  whole: number          // sides sent whole
  changes: number        // updates of changes sent
  unchanged: number      // updates that changed nothing, and were not sent
  levelsWhole: number
  levelsChanges: number
}

export interface BookStreamOptions {
  readonly mode?: Mode
  readonly resync?: Resync
  readonly maxLevels?: number
  /** The policy clock, in seconds; monotonic. */
  readonly clock?: () => number
  /** This machine's clock in nanoseconds, for an update given without a time. */
  readonly wallClockNs?: () => bigint
}

export interface StampOptions {
  readonly timestampNs?: bigint | number
}

/** One book - one symbol on one exchange - sent whole or as its changes. See the module. */
export class BookStream {
  readonly stats: StreamStats = { updates: 0, whole: 0, changes: 0, unchanged: 0, levelsWhole: 0, levelsChanges: 0 }

  private readonly sink: BookSink
  private readonly streamMode: Mode
  private readonly policy: Resync
  private readonly maxLevels: number
  private readonly clock: () => number
  private readonly wallClockNs: () => bigint
  private readonly sides: Record<Side, SideState>
  private busy = false

  constructor(sink: BookSink, options: BookStreamOptions = {}) {
    const mode = options.mode ?? 'incremental'
    if (!(MODES as readonly string[]).includes(mode)) {
      throw new RangeError(`mode must be one of ${JSON.stringify(MODES)}, not ${describe(mode)}`)
    }
    const maxLevels = options.maxLevels ?? ENGINE_DEPTH
    if (typeof maxLevels !== 'number' || !Number.isSafeInteger(maxLevels) || maxLevels < 1) {
      throw new RangeError(`maxLevels must be a positive integer, not ${describe(maxLevels)}`)
    }
    this.sink = sink
    this.streamMode = mode
    this.policy = options.resync ?? new Resync()
    this.maxLevels = maxLevels
    this.clock = options.clock ?? (() => performance.now() / 1000)
    this.wallClockNs = options.wallClockNs ?? (() => BigInt(Date.now()) * 1_000_000n)
    const fresh = (): SideState => ({ book: new Map(), sentWhole: false, unsure: false, sinceWhole: 0, wholeAt: 0, lastNs: -1n })
    this.sides = { bid: fresh(), ask: fresh() }
  }

  get mode(): Mode {
    return this.streamMode
  }

  /**
   * The side as the stream holds it, best first: what it last sent, with every change `apply()` was
   * given - deeper than `maxLevels` if they made it so.
   */
  book(side: string): Level[] {
    const s = sideOf(side)
    return levelsOf(s, this.sides[s].book)
  }

  /**
   * The whole side as it is now: sent whole in `full` mode, its difference in `incremental`. Without a
   * time, this machine's clock when it is called.
   */
  async update(side: string, levels: readonly LevelInput[], options: StampOptions = {}): Promise<void> {
    return this.exclusive(async () => {
      const s = sideOf(side)
      const now = wholeSide(s, levels)
      if (now.size > this.maxLevels) {
        throw new RangeError(
          `the ${s} side has ${now.size} prices and the engine keeps ${this.maxLevels} a side: give its best ` +
            `${this.maxLevels}. Nothing was sent`,
        )
      }
      const state = this.sides[s]
      const stamp = this.stamp(s, state, options.timestampNs, true)
      this.stats.updates += 1
      if (this.streamMode === 'full' || this.due(state)) return this.sendWhole(s, state, now, stamp)
      const diff = bookDiff(s, levelsOf(s, state.book), levelsOf(s, now))
      if (diff.length > now.size) return this.sendWhole(s, state, now, stamp)
      return this.sendChanges(s, state, now, diff, stamp)
    })
  }

  /**
   * Changes to the side - a quantity of 0 removes a price - applied to the stream's book and sent as
   * given, removals first; or the side whole, when the module says so. `incremental` only. Without a
   * time, this machine's clock when it is called.
   */
  async apply(side: string, changes: readonly LevelInput[], options: StampOptions = {}): Promise<void> {
    return this.exclusive(async () => {
      const s = sideOf(side)
      if (this.streamMode !== 'incremental') {
        throw new RangeError(
          'apply() sends changes, which a full stream does not: it sends every side whole. Give it the whole ' +
            'side with update(), or open the stream incremental',
        )
      }
      const ordered = orderedChanges(s, changes)
      const state = this.sides[s]
      const stamp = this.stamp(s, state, options.timestampNs, true)
      this.stats.updates += 1
      const now: SideBook = new Map(state.book)
      for (const [price, quantity, count] of ordered) {
        if (quantity === 0n) now.delete(price)
        else now.set(price, [quantity, count])
      }
      if (this.due(state) || ordered.length > Math.min(now.size, this.maxLevels)) {
        return this.sendWhole(s, state, now, stamp)
      }
      // Sent as given, a removal of a price the stream's book does not hold included: the engine's
      // book may hold it, and removing a price nothing holds changes nothing.
      return this.sendChanges(s, state, now, ordered, stamp)
    })
  }

  /**
   * Send the side - both, without one - whole now, whatever the policy says. Without a time, the time
   * of the side's last update, so the side is the book as of it; this machine's clock for a side the
   * stream has not sent.
   */
  async resync(side?: string, options: StampOptions = {}): Promise<void> {
    return this.exclusive(async () => {
      const sides = side === undefined ? SIDES : [sideOf(side)]
      const stamps: [Side, bigint][] = sides.map((s) => {
        const state = this.sides[s]
        if (options.timestampNs === undefined && state.lastNs >= 0n) return [s, state.lastNs]
        return [s, this.stamp(s, state, options.timestampNs, false)]
      })
      for (const [s, stamp] of stamps) {
        const state = this.sides[s]
        if (stamp > state.lastNs) state.lastNs = stamp
        await this.sendWhole(s, state, new Map(state.book), stamp)
      }
    })
  }

  // --- internals -----------------------------------------------------------------------------

  private async exclusive(body: () => Promise<void>): Promise<void> {
    if (this.busy) {
      throw new ResourceBusy(
        'this book stream already has an update in progress: await each one, so the engine applies them in ' +
          'the order the stream computed them',
      )
    }
    this.busy = true
    try {
      await body()
    } finally {
      this.busy = false
    }
  }

  private stamp(side: Side, state: SideState, timestampNs: bigint | number | undefined, record: boolean): bigint {
    let stamp: bigint
    if (timestampNs === undefined) {
      const wall = this.wallClockNs()
      stamp = wall > state.lastNs ? wall : state.lastNs
    } else {
      let given: bigint
      if (typeof timestampNs === 'bigint') given = timestampNs
      else if (typeof timestampNs === 'number' && Number.isSafeInteger(timestampNs)) given = BigInt(timestampNs)
      else throw new RangeError(`timestampNs must be an integer of nanoseconds in [0, 2^63), not ${describe(timestampNs)}`)
      if (given < 0n || given > MAX_TIMESTAMP_NS) {
        throw new RangeError(`timestampNs must be an integer of nanoseconds in [0, 2^63), not ${describe(timestampNs)}`)
      }
      if (given < state.lastNs) {
        throw new RangeError(
          `timestampNs ${given} is earlier than ${state.lastNs}, the time of the last update of the ${side} side: ` +
            "the engine's history applies updates in time order and its live book in the order they arrive, so " +
            'the two would disagree. Nothing was sent',
        )
      }
      stamp = given
    }
    if (record) state.lastNs = stamp
    return stamp
  }

  private due(state: SideState): boolean {
    if (!state.sentWhole || state.unsure) return true
    const policy = this.policy
    if (policy.updates > 0 && state.sinceWhole >= policy.updates) return true
    return policy.seconds > 0 && this.clock() - state.wholeAt >= policy.seconds
  }

  private async sendWhole(side: Side, state: SideState, now: SideBook, timestampNs: bigint): Promise<void> {
    const levels = levelsOf(side, now).slice(0, this.maxLevels)
    state.book = now
    try {
      await this.sink.sendWhole(side, levels, timestampNs)
    } catch (error) {
      state.unsure = true
      throw error
    }
    state.unsure = false
    state.sentWhole = true
    state.sinceWhole = 0
    state.wholeAt = this.clock()
    this.stats.whole += 1
    this.stats.levelsWhole += levels.length
  }

  private async sendChanges(side: Side, state: SideState, now: SideBook, levels: readonly Level[], timestampNs: bigint): Promise<void> {
    state.book = now
    state.sinceWhole += 1
    if (levels.length === 0) {
      this.stats.unchanged += 1
      return
    }
    try {
      await this.sink.sendChanges(side, levels, timestampNs)
    } catch (error) {
      state.unsure = true
      throw error
    }
    this.stats.changes += 1
    this.stats.levelsChanges += levels.length
  }
}

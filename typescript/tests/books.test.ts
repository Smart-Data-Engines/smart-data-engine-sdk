/**
 * The book stream (`src/books.ts`, the engine's #233) against the shared vectors, which hold the
 * reference library to the same differences and the same sends.
 */

import { readdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import { BookStream, bookDiff, orderedChanges, Resync, type Level, type LevelInput, type Side } from '../src/books.js'
import { ResourceBusy } from '../src/errors.js'

const root = fileURLToPath(new URL('../../conformance/vectors/books/', import.meta.url))

interface DiffCase {
  name: string
  side: string
  before: LevelInput[]
  after: LevelInput[]
  diff?: number[][]
  error?: string
  why: string
}

interface Step {
  at: number
  op: 'update' | 'apply' | 'resync' | 'fail_next'
  side?: string
  levels?: LevelInput[]
  ts?: number
  error?: string
}

interface StreamCase {
  name: string
  mode: 'full' | 'incremental'
  resync: { updates: number; seconds: number }
  max_levels?: number
  wall_ns?: number
  steps: Step[]
  sent: { kind: string; side: string; ts: number; levels: number[][] }[]
  book?: Record<string, number[][]>
}

function cases<T>(kind: 'diff' | 'stream'): (T & { id: string })[] {
  const all = readdirSync(root).sort().map((id) => ({ id, ...(JSON.parse(readFileSync(`${root}${id}/case.json`, 'utf8')) as { kind: string }) }))
  const kinds = new Set(all.map((vector) => vector.kind))
  if (kinds.size !== 2 || !kinds.has('diff') || !kinds.has('stream')) throw new Error(`a kind this runner does not know: ${[...kinds].join(', ')}`)
  return all.filter((vector) => vector.kind === kind) as unknown as (T & { id: string })[]
}

function plain(levels: readonly Level[]): number[][] {
  return levels.map((level) => level.map(Number))
}

describe('books, kind diff', () => {
  for (const vector of cases<DiffCase>('diff')) {
    it(`books/${vector.id}`, () => {
      if (vector.error !== undefined) {
        expect(() => bookDiff(vector.side, vector.before, vector.after)).toThrow(vector.error)
        return
      }
      expect(plain(bookDiff(vector.side, vector.before, vector.after)), vector.why).toEqual(vector.diff)
    })
  }
})

class Sink {
  readonly sent: { kind: string; side: string; ts: number; levels: number[][] }[] = []
  failNext = false

  private async send(kind: string, side: Side, levels: readonly Level[], timestampNs: bigint): Promise<void> {
    if (this.failNext) {
      this.failNext = false
      throw new Error('sink refused')
    }
    this.sent.push({ kind, side, ts: Number(timestampNs), levels: plain(levels) })
  }

  async sendWhole(side: Side, levels: readonly Level[], timestampNs: bigint): Promise<void> {
    return this.send('whole', side, levels, timestampNs)
  }

  async sendChanges(side: Side, levels: readonly Level[], timestampNs: bigint): Promise<void> {
    return this.send('changes', side, levels, timestampNs)
  }
}

async function step(stream: BookStream, move: Step): Promise<void> {
  const options = move.ts === undefined ? {} : { timestampNs: move.ts }
  if (move.op === 'resync') return stream.resync(move.side, options)
  if (move.op === 'update') return stream.update(move.side!, move.levels!, options)
  return stream.apply(move.side!, move.levels!, options)
}

describe('books, kind stream', () => {
  for (const vector of cases<StreamCase>('stream')) {
    it(`books/${vector.id}`, async () => {
      const sink = new Sink()
      let now = 0
      const stream = new BookStream(sink, {
        mode: vector.mode,
        resync: new Resync(vector.resync),
        maxLevels: vector.max_levels ?? 1000,
        clock: () => now,
        wallClockNs: () => {
          if (vector.wall_ns === undefined) throw new Error('a step without a time in a case without the machine\'s clock')
          return BigInt(vector.wall_ns)
        },
      })
      for (const move of vector.steps) {
        now = move.at
        if (move.op === 'fail_next') {
          sink.failNext = true
          continue
        }
        if (move.error !== undefined) {
          const before = sink.sent.length
          await expect(step(stream, move)).rejects.toThrow(move.error)
          expect(sink.sent.length).toBe(before)
        } else {
          await step(stream, move)
        }
      }
      expect(sink.sent).toEqual(vector.sent)
      for (const [side, levels] of Object.entries(vector.book ?? {})) {
        expect(plain(stream.book(side))).toEqual(levels)
      }
    })
  }
})

describe('book stream', () => {
  it('refuses a price changed twice in one update', () => {
    expect(() => orderedChanges('bid', [[100, 1, 1], [100, 0, 0]])).toThrow('twice')
  })

  it('refuses settings it cannot mean', () => {
    expect(() => new Resync({ updates: -1 })).toThrow(RangeError)
    expect(() => new Resync({ seconds: Number.NaN })).toThrow(RangeError)
    expect(() => new BookStream(new Sink(), { mode: 'some' as 'full' })).toThrow('mode')
    expect(() => new BookStream(new Sink(), { maxLevels: 0 })).toThrow('maxLevels')
  })

  it('refuses a side it does not know and a time out of range', async () => {
    const stream = new BookStream(new Sink())
    await expect(stream.update('buy', [])).rejects.toThrow('side')
    await expect(stream.update('bid', [], { timestampNs: -1n })).rejects.toThrow('timestampNs')
  })

  it('counts what it sent', async () => {
    const stream = new BookStream(new Sink(), { clock: () => 0 })
    await stream.update('bid', [[100, 1, 1], [99, 1, 1]], { timestampNs: 1 })
    await stream.update('bid', [[100, 1, 1], [99, 2, 1]], { timestampNs: 2 })
    await stream.update('bid', [[100, 1, 1], [99, 2, 1]], { timestampNs: 3 })
    expect(stream.stats).toEqual({ updates: 3, whole: 1, changes: 1, unchanged: 1, levelsWhole: 2, levelsChanges: 1 })
  })

  it('takes one update at a time', async () => {
    let release: () => void = () => undefined
    const slow = {
      sendWhole: () => new Promise<void>((resolve) => { release = resolve }),
      sendChanges: async () => undefined,
    }
    const stream = new BookStream(slow)
    const first = stream.update('bid', [[100, 1, 1]], { timestampNs: 1 })
    await expect(stream.update('bid', [[101, 1, 1]], { timestampNs: 2 })).rejects.toThrow(ResourceBusy)
    release()
    await first
    expect(stream.book('bid').map((level) => level.map(Number))).toEqual([[100, 1, 1]])
  })
})

/** Shared batch call order, data and exact value-free metric bytes. */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { expect } from 'vitest'
import { canonicalBytes, Recorder, Session, SdeError, type LogicalModel, type PlacementMap, type Row } from '../src/index.js'
import type { MemoryEngine } from '../src/testing/memory.js'
interface Step {
  op: string; rows?: Row[]; repeat?: number; entity?: string; body?: Step[]; rollback?: boolean; error?: string
}
class Rollback extends Error {}
export async function driveBulkVector(dir: string, model: LogicalModel, map: PlacementMap, engines: Record<string, MemoryEngine>): Promise<void> {
  const want = JSON.parse(readFileSync(join(dir, 'bulk.json'), 'utf8')) as {
    operations: Step[]; calls: unknown[]; tables: unknown; errors: string[]; metrics: unknown; metrics_hex: string
  }
  const recorder = new Recorder(model.version)
  const session = await Session.open(model, map, engines, { recorder })
  const errors: string[] = []
  async function run(steps: Step[]) {
    for (const step of steps) {
      try {
        if (step.op === 'transaction') {
          try {
            await session.transaction(['Reading'], async () => {
              await run(step.body!); if (step.rollback) throw new Rollback()
            })
          } catch (error) { if (!(error instanceof Rollback)) throw error }
        } else {
          const rows = Array.from({ length: step.repeat ?? 1 }, () => step.rows!).flat()
          await session.saveMany(step.entity ?? 'Reading', rows)
        }
      } catch (error) {
        expect(error).toBeInstanceOf(SdeError)
        expect((error as Error).name).toBe(step.error)
        errors.push((error as Error).name)
        continue
      }
      expect(step.error).toBeUndefined()
    }
  }
  await run(want.operations)
  expect(Object.values(engines)[0]!.recorded.calls).toEqual(want.calls)
  expect(Object.fromEntries(Object.entries(engines).map(([name, db]) => [name, db.tables]))).toEqual(want.tables)
  expect(errors).toEqual(want.errors)
  const window = recorder.roll()
  const metrics = {
    shapes: window?.shapes.map(stats => ({ shape_id: stats.shapeId, entity: stats.entity, group: stats.group,
      kind: stats.kind, calls: stats.calls, rows: stats.rows, errors: stats.errors })) ?? [],
    copies: window?.fanned.map(stats => ({ group: stats.group, materialization: stats.materialization,
      writes: stats.writes, failures: stats.failures })) ?? [],
  }
  expect(metrics).toEqual(want.metrics)
  expect(Buffer.from(canonicalBytes(metrics)).toString('hex')).toBe(want.metrics_hex)
}

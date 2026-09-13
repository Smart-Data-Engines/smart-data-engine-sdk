/** Drain native barriers and compare stable copies. Leaves holds installed; does not activate a map. */
import { compareCodePoints } from './canonical.js'
import { MigrationRefused } from './errors.js'
import { checkEpoch, type Fencable } from './generation.js'
import type { InspectionContext } from './inspection.js'
import { CHUNK_ROWS, verify, verifyRecord, type VerifyReport } from './migration.js'
import type { Engine } from './session.js'
import type { VerificationRequest } from './verification.js'
import type { FenceState, WriteFence } from './write-fence.js'

export interface FrozenTable {
  readonly engine: string
  readonly materialization: string
  readonly table: string
  readonly identity: string
  readonly project_id: string
  readonly epoch: number
  readonly hold_id: string
}
export interface FrozenVerifyReport {
  readonly comparison: VerifyReport
  readonly barriers: readonly FrozenTable[]
  readonly elapsedMs: number
  readonly matched: boolean
}
export function frozenVerifyRecord(report: FrozenVerifyReport): Record<string, unknown> {
  return { protocol: 1, comparison: verifyRecord(report.comparison), barriers: report.barriers,
    elapsed_ms: report.elapsedMs, matched: report.matched }
}
function matches(state: FenceState, wanted: FrozenTable): void {
  if (state.identity !== wanted.identity || state.projectId !== wanted.project_id) {
    throw new MigrationRefused('a frozen comparison table changed identity or project')
  }
  if (!state.complete || state.epoch !== wanted.epoch || !state.holds.includes(wanted.hold_id)) {
    throw new MigrationRefused('a frozen comparison lost its named barrier or write generation')
  }
}

export async function verifyFrozen(context: InspectionContext, group: string, options: {
  readonly request: VerificationRequest
  readonly holdId: string
  readonly epochs: Readonly<Record<string, number>>
  readonly chunkRows?: number
  readonly at?: string
}): Promise<FrozenVerifyReport> {
  // Capture caller-owned options before the first asynchronous metadata operation.
  const { request, holdId, at } = options
  if (typeof holdId !== 'string' || !/^[0-9a-f]{32}$/.test(holdId)) {
    throw new MigrationRefused('a frozen comparison hold id must be 32 lowercase hexadecimal digits')
  }
  const chunkRows = options.chunkRows ?? CHUNK_ROWS
  if (chunkRows < 1) throw new MigrationRefused('a frozen comparison needs a positive chunk size')
  request.checkSession(context.placement, context.projectId, group)
  if (at !== undefined) request.checkTime(at)
  const spot = context.placement.groups[group]!
  const materials = [spot.source, ...spot.alsoWrite]
  const ids = materials.map((material) => material.id).sort(compareCodePoints)
  if (spot.alsoWrite.length === 0 || JSON.stringify(Object.keys(options.epochs).sort(compareCodePoints)) !== JSON.stringify(ids)) {
    throw new MigrationRefused('frozen comparison epochs must name exactly the source and copy ids')
  }
  const epochs = Object.fromEntries(Object.entries(options.epochs).map(([name, epoch]) => [name, checkEpoch(epoch)]))
  const planned: { fence: WriteFence; wanted: FrozenTable }[] = []
  for (const material of [...materials].sort((a, b) => compareCodePoints(a.engine, b.engine) || compareCodePoints(a.id, b.id))) {
    const engine = context.engineNamed(material.engine) as Engine & Partial<Fencable>
    if (typeof engine.writeFence !== 'function' || typeof engine.validateSchema !== 'function') {
      throw new MigrationRefused('frozen comparison requires native write fences and schema checks')
    }
    await engine.validateSchema(material.layout)
    for (const table of Object.values(material.layout.tables).sort(compareCodePoints)) {
      const fence = engine.writeFence(table, { projectId: context.projectId })
      const state = await fence.state(), epoch = epochs[material.id] as number
      if (state.retired.includes(holdId)) throw new MigrationRefused('a frozen comparison cannot reuse a retired barrier id')
      if (!state.complete || state.epoch !== epoch) {
        throw new MigrationRefused('a frozen comparison table is not at the expected write generation')
      }
      planned.push({ fence, wanted: Object.freeze({ engine: material.engine, materialization: material.id,
        table, identity: state.identity, project_id: context.projectId, epoch, hold_id: holdId }) })
    }
  }
  const start = performance.now()
  for (const { fence, wanted } of planned) matches(await fence.freeze(holdId), wanted)
  const comparison = await verify(context, group, { request: request, chunkRows,
    ...(at === undefined ? {} : { at: at }) })
  for (const { fence, wanted } of planned) matches(await fence.state(), wanted)
  return Object.freeze({ comparison, barriers: Object.freeze(planned.map((entry) => entry.wanted)),
    elapsedMs: Math.floor(performance.now() - start),
    matched: comparison.matched && comparison.rowsSource === comparison.rowsTarget })
}

/** Wire vocabulary shared by placement maps and native write barriers. */
import { MigrationRefused } from './errors.js'
export const DRAIN_TABLE = '__sde_fence_drains'
export const EPOCH_COLUMN = '__sde_write_epoch'
export const MAX_EPOCH = 9_007_199_254_740_991
/** The placement map contract that introduced `project_id` and per-group `write_epoch`. */
export const GENERATIONS_SINCE = 4
export function checkEpoch(epoch: number): number {
  if (!Number.isSafeInteger(epoch) || epoch < 1) {
    throw new MigrationRefused('write epoch must be a positive safe integer')
  }
  return epoch
}

import type { LogicalModel } from './model.js'
import type { PhysicalFinding } from './physical.js'
import { fingerprintOf, type PhysicalLayout, type PlacementMap } from './placement.js'
import type { Engine, Row } from './session.js'
import type { WriteFence } from './write-fence.js'

export interface Fencable {
  writeFence(table: string, options: { projectId: string }): WriteFence
  /**
   * Refuses missing columns and types; returns how tables differ from the declared physical
   * design when `keys` are given. `void` is accepted from an adapter written before findings.
   */
  validateSchema(
    layout: PhysicalLayout,
    options?: { readonly keys?: Readonly<Record<string, readonly string[]>> },
  ): Promise<readonly PhysicalFinding[] | void>
}

export function checkMapProject(placement: PlacementMap, projectId: string | undefined): string | undefined {
  if (placement.contract < 4) return undefined
  if (projectId === undefined || projectId !== placement.projectId) {
    throw new MigrationRefused('this generation-bearing map needs its locally configured project_id; ' +
      'do not learn that identity from the supplied map')
  }
  if (fingerprintOf(placement) === undefined) {
    throw new MigrationRefused('generation-bearing sessions need an immutable loaded placement map')
  }
  return projectId
}

/**
 * Check generations and columns; return how tables differ from the declared physical design.
 *
 * The physical design is reported rather than refused: a running application must not stop because
 * a table's sort key or index differs from the map (requirement 3.6). Columns, types and
 * generations still refuse.
 */
export async function validateGenerations(model: LogicalModel, placement: PlacementMap,
  engines: Readonly<Record<string, Engine>>, projectId: string | undefined): Promise<readonly PhysicalFinding[]> {
  if (placement.contract < GENERATIONS_SINCE) return []
  const findings: PhysicalFinding[] = []
  const project = checkMapProject(placement, projectId) as string
  if (model.version !== placement.modelVersion) {
    throw new MigrationRefused('the generation-bearing map names another session model')
  }
  for (const name of Object.keys(placement.groups).sort()) {
    const spot = placement.groups[name]!
    for (const material of [spot.source, ...spot.derived]) {
      const engine = engines[material.engine] as Engine & Partial<Fencable>
      if (typeof engine?.writeFence !== 'function' || typeof engine.validateSchema !== 'function') {
        throw new MigrationRefused(`engine ${material.engine} does not implement write generations`)
      }
      const keys: Record<string, readonly string[]> = {}
      for (const entity of Object.keys(material.layout.tables)) {
        const spec = model.entities.find((candidate) => candidate.name === entity)
        if (spec !== undefined) keys[entity] = spec.key
      }
      findings.push(...((await engine.validateSchema(material.layout, { keys })) ?? []))
      for (const table of Object.values(material.layout.tables).sort()) {
        const state = await engine.writeFence(table, { projectId: project }).state()
        if (!state.complete || state.epoch !== spot.writeEpoch) {
          throw new MigrationRefused(`the write generation for ${name} is not active in ${material.engine}; ` +
            'provision the signed map or load the current map before opening a session')
        }
      }
    }
  }
  return findings
}

export function stampValues(placement: PlacementMap, group: string, values: Readonly<Row>): Readonly<Row> {
  const epoch = placement.groups[group]?.writeEpoch
  if (epoch === undefined) return values
  if (EPOCH_COLUMN in values) throw new MigrationRefused('the write-epoch column is reserved for the SDK')
  return { ...values, [EPOCH_COLUMN]: epoch }
}

export function logicalRow(placement: PlacementMap, row: Row | null): Row | null {
  if (placement.contract >= 4 && row !== null) {
    return Object.fromEntries(Object.entries(row).filter(([key]) => key !== EPOCH_COLUMN))
  }
  return row
}

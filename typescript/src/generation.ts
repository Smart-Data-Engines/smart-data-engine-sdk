/** Wire vocabulary shared by placement maps and native write barriers. */
import { MigrationRefused } from './errors.js'
export const DRAIN_TABLE = '__sde_fence_drains'
export const EPOCH_COLUMN = '__sde_write_epoch'
export const MAX_EPOCH = 9_007_199_254_740_991
export function checkEpoch(epoch: number): number {
  if (!Number.isSafeInteger(epoch) || epoch < 1) {
    throw new MigrationRefused('write epoch must be a positive safe integer')
  }
  return epoch
}

import type { LogicalModel } from './model.js'
import { fingerprintOf, type PhysicalLayout, type PlacementMap } from './placement.js'
import type { Engine, Row } from './session.js'
import type { WriteFence } from './write-fence.js'

export interface Fencable {
  writeFence(table: string, options: { projectId: string }): WriteFence
  validateSchema(layout: PhysicalLayout): Promise<void>
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

export async function validateGenerations(model: LogicalModel, placement: PlacementMap,
  engines: Readonly<Record<string, Engine>>, projectId: string | undefined): Promise<void> {
  if (placement.contract < 4) return
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
      await engine.validateSchema(material.layout)
      for (const table of Object.values(material.layout.tables).sort()) {
        const state = await engine.writeFence(table, { projectId: project }).state()
        if (!state.complete || state.epoch !== spot.writeEpoch) {
          throw new MigrationRefused(`the write generation for ${name} is not active in ${material.engine}; ` +
            'provision the signed map or load the current map before opening a session')
        }
      }
    }
  }
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

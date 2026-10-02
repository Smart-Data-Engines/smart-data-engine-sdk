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
import { fingerprintOf, type GroupPlacement, type PhysicalLayout, type PlacementMap } from './placement.js'
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
 * The groups whose writes carry a generation, by name: every group of a contract-4 or 5 map.
 *
 * These are the only groups a local operator acts on. A contract-6 group without a generation is on
 * an engine that cannot fence writes, which no staging, cutover or index build can reach.
 */
export function fencedGroups(placement: PlacementMap): Record<string, GroupPlacement> {
  const out: Record<string, GroupPlacement> = {}
  for (const name of Object.keys(placement.groups).sort()) {
    const spot = placement.groups[name]!
    if (spot.writeEpoch !== undefined) out[name] = spot
  }
  return out
}

/**
 * Contract 6: a group without a write generation must be on an engine that cannot fence.
 *
 * A map names its engines and carries no dialect, so this is answerable only where the adapters
 * are - when a session opens and when a schema is prepared.
 */
export function refuseAFencingEngine(group: string, engineName: string, engine: unknown): void {
  if (typeof (engine as Partial<Fencable> | undefined)?.writeFence === 'function') {
    throw new MigrationRefused(
      `group ${group} carries no write generation on ${engineName}, which fences writes; a map we ` +
        'issue gives every group on such an engine a generation, so this one was built for another engine',
    )
  }
}

function keysOf(model: LogicalModel, layout: PhysicalLayout): Record<string, readonly string[]> {
  const keys: Record<string, readonly string[]> = {}
  for (const entity of Object.keys(layout.tables)) {
    const spec = model.entities.find((candidate) => candidate.name === entity)
    if (spec !== undefined) keys[entity] = spec.key
  }
  return keys
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
    if (spot.writeEpoch === undefined) {
      // Contract 6: a group with no write generation, on an engine that cannot fence. The map
      // carries no dialect, so this is the first place either half can be checked.
      for (const material of [spot.source, ...spot.derived]) {
        const engine = engines[material.engine] as Engine & Partial<Fencable>
        refuseAFencingEngine(name, material.engine, engine)
        if (typeof engine?.validateSchema === 'function') {
          const keys = keysOf(model, material.layout)
          findings.push(...((await engine.validateSchema(material.layout, { keys })) ?? []))
        }
      }
      continue
    }
    for (const material of [spot.source, ...spot.derived]) {
      const engine = engines[material.engine] as Engine & Partial<Fencable>
      if (typeof engine?.writeFence !== 'function' || typeof engine.validateSchema !== 'function') {
        throw new MigrationRefused(`engine ${material.engine} does not implement write generations`)
      }
      const keys = keysOf(model, material.layout)
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
  // Reserved in every group of a generation-bearing map, including one without a generation of its
  // own (contract 6): a read strips the column from every row such a map returns.
  if (placement.contract >= GENERATIONS_SINCE && EPOCH_COLUMN in values) {
    throw new MigrationRefused('the write-epoch column is reserved for the SDK')
  }
  if (epoch === undefined) return values
  return { ...values, [EPOCH_COLUMN]: epoch }
}

export function logicalRow(placement: PlacementMap, row: Row | null): Row | null {
  if (placement.contract >= 4 && row !== null) {
    return Object.fromEntries(Object.entries(row).filter(([key]) => key !== EPOCH_COLUMN))
  }
  return row
}

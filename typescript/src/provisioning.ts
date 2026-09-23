/** Prepare client-owned physical schema before opening runtime connections. */
import { compareCodePoints } from './canonical.js'
import { EngineError, MigrationRefused } from './errors.js'
import { checkMapProject, type Fencable } from './generation.js'
import { colocationGroups } from './groups.js'
import type { LogicalModel } from './model.js'
import { refuseFindings } from './physical.js'
import type { PlacementMap } from './placement.js'
import type { Engine } from './session.js'
import type { WatermarkStore } from './watermark.js'

export async function prepareSchema(model: LogicalModel, placement: PlacementMap,
  engines: Readonly<Record<string, Engine>>, options: { projectId?: string } = {}): Promise<void> {
  if (model.version !== placement.modelVersion) {
    throw new MigrationRefused('schema preparation needs the model named by the placement map')
  }
  const project = checkMapProject(placement, options.projectId)
  const needed = [...new Set(Object.values(placement.groups).flatMap((spot) =>
    [spot.source, ...spot.derived].map((material) => material.engine)))].sort()
  if (needed.some((name) => engines[name] === undefined)) {
    throw new MigrationRefused('schema preparation is missing an engine named by the map')
  }
  if (placement.contract >= 4 && needed.some((name) =>
    typeof (engines[name] as Engine & Partial<Fencable>).writeFence !== 'function')) {
    throw new MigrationRefused('schema preparation needs native write generations on every engine')
  }
  for (const group of colocationGroups(model)) {
    const spot = placement.groups[group.name]!
    const keys: Record<string, readonly string[]> = {}
    for (const name of group.members) keys[name] = model.entities.find((entity) => entity.name === name)!.key
    for (const material of [spot.source, ...spot.derived]) {
      const engine = engines[material.engine] as Engine & Fencable
      // Provisioning is where a person can act on a physical difference, so here it refuses.
      refuseFindings((await engine.ensureSchema(material.layout, { keys })) ?? [], EngineError)
      if (spot.writeEpoch !== undefined) {
        for (const table of Object.values(material.layout.tables).sort()) {
          await engine.writeFence(table, { projectId: project as string }).prepare(spot.writeEpoch)
        }
      }
    }
  }
  if (placement.signed) {
    // Prepare storage without adopting this map's version before runtime activation.
    for (const name of Object.keys(engines).sort(compareCodePoints)) {
      const store = engines[name] as Engine & Partial<WatermarkStore>
      if (typeof store.mapWatermark === 'function' && typeof store.recordMapVersion === 'function') {
        await store.mapWatermark()
      }
    }
  }
}

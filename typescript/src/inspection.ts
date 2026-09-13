/** A local operator's data view, without runtime writes or watermark adoption. */
import { MigrationRefused } from './errors.js'
import { checkMapProject } from './generation.js'
import type { LogicalModel } from './model.js'
import type { PlacementMap } from './placement.js'
import type { Engine } from './session.js'

export interface MigrationView {
  readonly model: LogicalModel
  readonly placement: PlacementMap
  readonly projectId: string | undefined
  engineNamed(name: string): Engine
}

export class InspectionContext implements MigrationView {
  private readonly engines: Readonly<Record<string, Engine>>
  readonly projectId: string
  constructor(readonly model: LogicalModel, readonly placement: PlacementMap,
    engines: Readonly<Record<string, Engine>>, options: { projectId: string }) {
    if (placement.contract < 4) throw new MigrationRefused('operator inspection requires a generation-bearing placement map')
    checkMapProject(placement, options.projectId)
    if (model.version !== placement.modelVersion) throw new MigrationRefused('operator inspection needs the model named by its map')
    const missing = [...new Set(Object.values(placement.groups).flatMap((spot) =>
      [spot.source, ...spot.derived].map((material) => material.engine)))].filter((name) => engines[name] === undefined).sort()
    if (missing.length > 0) throw new MigrationRefused(`operator inspection is missing engines ${JSON.stringify(missing)}`)
    this.engines = Object.freeze({ ...engines })
    this.projectId = options.projectId
    Object.freeze(this)
  }
  engineNamed(name: string): Engine {
    const engine = this.engines[name]
    if (engine === undefined) throw new MigrationRefused(`operator inspection has no engine ${name}`)
    return engine
  }
}

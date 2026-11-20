import { copyMarker } from '../src/ownership.js'
import type { PlacementMap } from '../src/placement.js'
import type { MemoryEngine } from '../src/testing/memory.js'
import { EPOCH_COLUMN, FENCE_PREFIX, WriteFence } from '../src/write-fence.js'
import { MemoryFences } from './_write-fence.js'

/**
 * The marks `prepareSchema` writes on a map's copies (format contract §7r), by engine and table:
 * what a fake engine of a provisioned deployment holds.
 */
export function copyMarks(placement: PlacementMap): Record<string, Record<string, string>> {
  const marks: Record<string, Record<string, string>> = {}
  if (placement.contract < 4 || placement.projectId === undefined) return marks
  for (const [group, spot] of Object.entries(placement.groups)) {
    if (spot.writeEpoch === undefined) continue
    for (const material of spot.derived) {
      for (const table of Object.values(material.layout.tables)) {
        ;(marks[material.engine] ??= {})[table] = copyMarker(placement.projectId, group, table)
      }
    }
  }
  return marks
}

export function bindGenerationMetadata(engines: Record<string, MemoryEngine>, metadata:
  Record<string, Record<string, { project_id: string; epoch: number }>>, recordFences = false,
  marks: Record<string, Record<string, string>> = {}): void {
  for (const [name, tables] of Object.entries(metadata)) {
    const backends = new Map<string, MemoryFences>()
    for (const [table, record] of Object.entries(tables)) {
      const backend = new MemoryFences()
      backend.identity = name + '/' + table
      backend.column = 'valid'
      backend.constraints = {
        [FENCE_PREFIX + 'owner_' + record.project_id]: '1',
        [FENCE_PREFIX + 'min_' + record.epoch]: `${EPOCH_COLUMN} >= ${record.epoch}`,
        [FENCE_PREFIX + 'max_' + record.epoch]: `${EPOCH_COLUMN} <= ${record.epoch}`,
      }
      if (recordFences) {
        const original = backend.done.bind(backend)
        backend.done = (...call: unknown[]): void => {
          original(...call)
          const [operation, tableName, ...args] = call
          engines[name]!.recorded.note(name, 'fence_' + String(operation), { table: tableName, arguments: args })
        }
      }
      backends.set(table, backend)
    }
    Object.defineProperty(engines[name], 'validateSchema', { value: async () => undefined })
    // Each table its own, and a copy's carrying its mark: a provisioned deployment (§7r).
    const known = new Set(Object.keys(tables))
    Object.defineProperty(engines[name], 'tableFacts', {
      value: async (names: readonly string[]) =>
        Object.fromEntries(names.map((table) => [table, {
          keys: known.has(table) ? [JSON.stringify(['memory', name, table])] : [],
          mark: marks[name]?.[table] ?? null,
        }])),
    })
    Object.defineProperty(engines[name], 'writeFence', {
      value: (table: string, options: { projectId: string }) => new WriteFence(backends.get(table)!, table, options),
    })
  }
}

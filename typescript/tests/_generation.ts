import type { MemoryEngine } from '../src/testing/memory.js'
import { EPOCH_COLUMN, FENCE_PREFIX, WriteFence } from '../src/write-fence.js'
import { MemoryFences } from './_write-fence.js'

export function bindGenerationMetadata(engines: Record<string, MemoryEngine>, metadata:
  Record<string, Record<string, { project_id: string; epoch: number }>>): void {
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
      backends.set(table, backend)
    }
    Object.defineProperty(engines[name], 'validateSchema', { value: async () => undefined })
    Object.defineProperty(engines[name], 'writeFence', {
      value: (table: string, options: { projectId: string }) => new WriteFence(backends.get(table)!, table, options),
    })
  }
}

import { expect } from 'vitest'
import { fenceState, type ColumnState, type FenceBackend, type FenceMetadata } from '../src/write-fence.js'

export class MemoryFences implements FenceBackend {
  identity = 'table-identity'
  column: ColumnState = 'absent'
  constraints: Record<string, string> = {}
  calls: unknown[][] = []
  failAfter: number | undefined
  async metadata(_table: string): Promise<FenceMetadata> {
    return { identity: this.identity, column: this.column, constraints: { ...this.constraints } }
  }
  done(...call: unknown[]): void {
    this.calls.push(call)
    if (this.calls.length === this.failAfter) throw new Error('lost the response after the DDL took effect')
  }
  async addColumn(table: string): Promise<void> { this.column = 'valid'; this.done('column', table) }
  async addConstraint(table: string, name: string, expression: string): Promise<void> {
    this.constraints[name] ??= expression
    this.done('add', table, name, expression)
  }
  async dropConstraint(table: string, name: string): Promise<void> {
    delete this.constraints[name]; this.done('drop', table, name)
  }
  async drain(table: string, options: { projectId: string; hold: string }): Promise<void> {
    const state = fenceState(await this.metadata(table))
    expect(state.projectId).toBe(options.projectId)
    expect(state.holds).toContain(options.hold)
    this.done('drain', table, options.projectId, options.hold)
  }
  async restore(table: string, options: { projectId: string; hold: string }): Promise<void> {
    this.done('restore', table, options.projectId, options.hold)
  }
  async accepts(epoch: number): Promise<boolean> {
    const state = fenceState(await this.metadata('events'))
    return state.complete && !state.closed && state.lowerEpoch !== undefined && state.lowerEpoch <= epoch &&
      state.upperEpoch !== undefined && epoch <= state.upperEpoch
  }
}

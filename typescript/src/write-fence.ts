/** Native write generations and named barriers. These primitives do not authorize a cutover. */
import { MigrationRefused } from './errors.js'
import { DRAIN_TABLE, EPOCH_COLUMN, checkEpoch } from './generation.js'
export { DRAIN_TABLE, EPOCH_COLUMN, checkEpoch } from './generation.js'
import { BACKFILL_TABLE, WATERMARK_TABLE } from './placement.js'

export const FENCE_PREFIX = '__sde_f_'
const SETUP = FENCE_PREFIX + 'setup'
export type ColumnState = 'absent' | 'valid' | 'conflict'

export interface FenceMetadata {
  readonly identity: string
  readonly column: ColumnState
  readonly constraints: Readonly<Record<string, string>>
}

/** Provisioning/execution capability, separate from a runtime role's INSERT permission. */
export interface FenceBackend {
  metadata(table: string): Promise<FenceMetadata>
  addColumn(table: string): Promise<void>
  addConstraint(table: string, name: string, expression: string): Promise<void>
  dropConstraint(table: string, name: string): Promise<void>
  drain(table: string, options: { projectId: string; hold: string }): Promise<void>
  restore(table: string, options: { projectId: string; hold: string }): Promise<void>
}

function identity(value: string, label: string): void {
  if (typeof value !== 'string' || !/^[0-9a-f]{32}$/.test(value)) {
    throw new MigrationRefused(`write fence ${label} must be 32 lowercase hexadecimal digits`)
  }
}

function predicate(raw: string): string {
  let value = raw.trim()
  if (value.startsWith('CHECK')) value = value.slice(5).trim()
  value = value.replace(/\s+NOT\s+VALID$/, '')
  while (value.startsWith('(') && value.endsWith(')')) value = value.slice(1, -1).trim()
  if (value === 'true' || value === '1') return '1'
  if (value === 'false' || value === '0') return '0'
  const matched = /^\(*\s*(?:"__sde_write_epoch"|`__sde_write_epoch`|__sde_write_epoch)\s*\)*\s*(>=|<=)\s*\(*\s*(?:([0-9]+)|'([0-9]+)'::bigint)\s*\)*$/.exec(value)
  if (matched === null) return '<unrecognized>'
  const number = BigInt(matched[2] ?? matched[3] as string)
  return EPOCH_COLUMN + matched[1] + number.toString()
}

export class FenceState {
  readonly minimums: readonly number[]
  readonly maximums: readonly number[]
  readonly holds: readonly string[]
  readonly retired: readonly string[]

  constructor(
    readonly identity: string,
    readonly projectId: string | undefined,
    readonly column: ColumnState,
    minimums: readonly number[], maximums: readonly number[], holds: readonly string[], retired: readonly string[],
  ) {
    this.minimums = Object.freeze([...minimums].sort((a, b) => a - b))
    this.maximums = Object.freeze([...maximums].sort((a, b) => a - b))
    this.holds = Object.freeze([...holds].sort())
    this.retired = Object.freeze([...retired].sort())
    Object.freeze(this)
  }

  get lowerEpoch(): number | undefined { return this.minimums.at(-1) }
  get upperEpoch(): number | undefined { return this.maximums[0] }
  get complete(): boolean {
    return this.projectId !== undefined && this.column === 'valid' &&
      this.minimums.length > 0 && this.maximums.length > 0
  }
  get closed(): boolean {
    return this.holds.length > 0 || (this.lowerEpoch !== undefined && this.upperEpoch !== undefined &&
      this.lowerEpoch > this.upperEpoch)
  }
  get epoch(): number | undefined {
    return this.complete && this.lowerEpoch === this.upperEpoch ? this.lowerEpoch : undefined
  }
  asRecord(): Record<string, unknown> {
    return {
      identity: this.identity, project_id: this.projectId ?? null, column: this.column,
      lower_epoch: this.lowerEpoch ?? null, upper_epoch: this.upperEpoch ?? null,
      holds: [...this.holds], retired: [...this.retired], closed: this.closed,
    }
  }
}

export function fenceState(metadata: FenceMetadata): FenceState {
  const owners: string[] = [], minima: number[] = [], maxima: number[] = [], holds: string[] = [], retired: string[] = []
  for (const name of Object.keys(metadata.constraints).sort()) {
    if (!name.startsWith(FENCE_PREFIX)) continue
    const suffix = name.slice(FENCE_PREFIX.length)
    const actual = predicate(metadata.constraints[name] as string)
    let expected: string
    if (/^owner_[0-9a-f]{32}$/.test(suffix)) {
      owners.push(suffix.slice(6)); expected = '1'
    } else if (/^retired_[0-9a-f]{32}$/.test(suffix)) {
      retired.push(suffix.slice(8)); expected = '1'
    } else if (suffix === 'setup' || /^hold_[0-9a-f]{32}$/.test(suffix)) {
      holds.push(suffix === 'setup' ? 'setup' : suffix.slice(5)); expected = '0'
    } else if (/^(?:min|max)_[1-9][0-9]*$/.test(suffix)) {
      const value = checkEpoch(Number(suffix.slice(4)))
      ;(suffix.startsWith('min_') ? minima : maxima).push(value)
      expected = EPOCH_COLUMN + (suffix.startsWith('min_') ? '>=' : '<=') + String(value)
    } else {
      throw new MigrationRefused('unrecognized constraint in the reserved write-fence namespace')
    }
    if (actual !== expected) {
      throw new MigrationRefused(`write fence constraint ${name} has an unexpected predicate`)
    }
  }
  if (owners.length > 1) {
    throw new MigrationRefused('the table carries write fences from more than one project')
  }
  if (owners.length === 0 && (minima.length > 0 || maxima.length > 0 || holds.length > 0 || retired.length > 0)) {
    throw new MigrationRefused('write fence constraints have no project owner')
  }
  return new FenceState(metadata.identity, owners[0], metadata.column, minima, maxima, holds, retired)
}

/** Serialize administrative calls per project. A partial command may leave the table closed. */
export class WriteFence {
  readonly projectId: string
  constructor(private readonly backend: FenceBackend, readonly table: string, options: { projectId: string }) {
    identity(options.projectId, 'project_id')
    if (typeof table !== 'string' || table.length === 0 || table.includes('\0')) {
      throw new MigrationRefused('write fence table must be a nonempty identifier')
    }
    if ([DRAIN_TABLE, BACKFILL_TABLE, WATERMARK_TABLE].includes(table)) {
      throw new MigrationRefused('write fences cannot take over an SDK metadata table')
    }
    this.projectId = options.projectId
  }

  async state(): Promise<FenceState> {
    const state = fenceState(await this.backend.metadata(this.table))
    if (state.projectId !== undefined && state.projectId !== this.projectId) {
      throw new MigrationRefused('the write fence belongs to another project')
    }
    if (state.column === 'conflict') {
      throw new MigrationRefused('the reserved write-epoch column has an incompatible definition')
    }
    return state
  }
  private async ready(): Promise<FenceState> {
    const state = await this.state()
    if (!state.complete) throw new MigrationRefused('the write fence is not fully provisioned')
    return state
  }

  async prepare(epoch: number): Promise<FenceState> {
    checkEpoch(epoch)
    const state = await this.state()
    if (state.complete && !state.holds.includes('setup')) {
      if (state.epoch !== epoch) throw new MigrationRefused('provisioning cannot change an existing write epoch')
      return state
    }
    if (state.projectId === undefined && state.column !== 'absent') {
      throw new MigrationRefused('the reserved write-epoch column is not owned by this project')
    }
    if (state.lowerEpoch !== undefined && state.lowerEpoch > epoch) {
      throw new MigrationRefused('a write epoch cannot move backwards')
    }
    if (state.projectId === undefined) {
      await this.backend.addConstraint(this.table, FENCE_PREFIX + 'owner_' + this.projectId, '1')
    }
    await this.backend.addConstraint(this.table, SETUP, '0')
    await this.backend.addColumn(this.table)
    await this.bounds(epoch)
    await this.backend.drain(this.table, { projectId: this.projectId, hold: 'setup' })
    await this.backend.dropConstraint(this.table, SETUP)
    return this.ready()
  }

  async freeze(requestId: string): Promise<FenceState> {
    identity(requestId, 'request_id')
    const state = await this.ready()
    if (state.retired.includes(requestId)) throw new MigrationRefused('a completed write barrier id cannot be reused')
    await this.backend.addConstraint(this.table, FENCE_PREFIX + 'hold_' + requestId, '0')
    // Constraint existence closes admission, not INSERTs using an older metadata snapshot.
    await this.backend.drain(this.table, { projectId: this.projectId, hold: requestId })
    return this.ready()
  }
  async resume(requestId: string): Promise<FenceState> {
    identity(requestId, 'request_id')
    await this.backend.restore(this.table, { projectId: this.projectId, hold: requestId })
    return this.freeze(requestId)
  }
  async resumePrepare(epoch: number): Promise<FenceState> {
    checkEpoch(epoch)
    await this.backend.restore(this.table, { projectId: this.projectId, hold: 'setup' })
    return this.prepare(epoch)
  }
  async advance(epoch: number): Promise<FenceState> {
    checkEpoch(epoch)
    const state = await this.ready()
    if (state.holds.length === 0) throw new MigrationRefused('changing a write epoch needs a named write barrier')
    if (state.lowerEpoch !== undefined && epoch < state.lowerEpoch) {
      throw new MigrationRefused('a write epoch cannot move backwards')
    }
    await this.bounds(epoch)
    return this.ready()
  }
  private async bounds(epoch: number): Promise<void> {
    await this.backend.addConstraint(this.table, FENCE_PREFIX + 'min_' + epoch, `${EPOCH_COLUMN} >= ${epoch}`)
    await this.backend.addConstraint(this.table, FENCE_PREFIX + 'max_' + epoch, `${EPOCH_COLUMN} <= ${epoch}`)
    const state = await this.state()
    if (state.lowerEpoch !== undefined && state.lowerEpoch > epoch) {
      throw new MigrationRefused('another executor installed a newer write epoch; table stays closed')
    }
    for (const old of state.maximums) {
      if (old < epoch) await this.backend.dropConstraint(this.table, FENCE_PREFIX + 'max_' + old)
    }
    for (const old of state.minimums) {
      if (old < epoch) await this.backend.dropConstraint(this.table, FENCE_PREFIX + 'min_' + old)
    }
  }
  async release(requestId: string): Promise<FenceState> {
    identity(requestId, 'request_id')
    const state = await this.ready()
    if (state.epoch === undefined) throw new MigrationRefused('cannot release a barrier with an incomplete epoch change')
    if (!state.holds.includes(requestId)) return state
    await this.backend.addConstraint(this.table, FENCE_PREFIX + 'retired_' + requestId, '1')
    await this.backend.dropConstraint(this.table, FENCE_PREFIX + 'hold_' + requestId)
    return this.ready()
  }
}

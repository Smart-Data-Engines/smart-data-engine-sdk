/** Native fencing DDL for dedicated provisioning connections. No placement decision lives here. */
import { EngineError, MigrationRefused } from '../errors.js'
import { indexNameRefusal, QUOTE } from '../schema.js'
import type { Row } from '../session.js'
import {
  DRAIN_TABLE, EPOCH_COLUMN, FENCE_PREFIX, fenceState, type ColumnState, type FenceBackend, type FenceMetadata,
} from '../write-fence.js'

interface PgTransport {
  query(sql: string, values?: readonly unknown[]): Promise<readonly Row[]>
  transaction(body: () => Promise<void>): Promise<void>
  busy(): boolean
}
interface ChTransport {
  query(sql: string): Promise<readonly Row[]>
  command(sql: string): Promise<void>
  literal(value: unknown): string
}

function held(metadata: FenceMetadata, projectId: string, hold: string): void {
  const state = fenceState(metadata)
  if (state.projectId !== projectId || !state.holds.includes(hold)) {
    throw new MigrationRefused("the requested project's write barrier is not installed")
  }
}

export class PostgresFences implements FenceBackend {
  private readonly quote = QUOTE['postgres'] as (name: string) => string
  constructor(private readonly transport: PgTransport) {}
  private idle(): void {
    if (this.transport.busy()) {
      throw new MigrationRefused('write-fence DDL cannot run inside an application transaction')
    }
  }
  async metadata(table: string): Promise<FenceMetadata> {
    const quoted = this.quote(table)
    const rows = await this.transport.query(
      'SELECT c.oid::text AS identity, c.relkind AS kind, EXISTS (SELECT 1 FROM pg_inherits i ' +
      'WHERE i.inhrelid=c.oid OR i.inhparent=c.oid) AS inherits FROM pg_class c ' +
      'WHERE c.oid=to_regclass($1)', [quoted],
    )
    if (rows.length !== 1) throw new EngineError('write-fence table does not exist')
    const row = rows[0] as Row
    if (row['kind'] !== 'r' || row['inherits'] !== false) {
      throw new MigrationRefused('write fences support ordinary PostgreSQL tables without inheritance')
    }
    const columns = await this.transport.query(
      "SELECT a.atttypid='bigint'::regtype AS typed, a.attnotnull AS required, " +
      'a.attgenerated AS generated, pg_get_expr(d.adbin,d.adrelid) AS expression FROM pg_attribute a ' +
      'LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum ' +
      'WHERE a.attrelid=to_regclass($1) AND a.attname=$2 AND NOT a.attisdropped', [quoted, EPOCH_COLUMN],
    )
    const column = columns[0]
    const valid = column !== undefined && column['typed'] === true && column['required'] === true &&
      column['generated'] === '' && (column['expression'] === '0' || column['expression'] === "'0'::bigint")
    const constraints = await this.transport.query(
      "SELECT conname AS name, pg_get_constraintdef(oid) AS expression FROM pg_constraint " +
      "WHERE conrelid=to_regclass($1) AND contype='c'", [quoted],
    )
    return {
      identity: String(row['identity']), column: valid ? 'valid' : columns.length > 0 ? 'conflict' : 'absent',
      constraints: Object.fromEntries(constraints.map((item) => [String(item['name']), String(item['expression'])])),
    }
  }
  async addColumn(table: string): Promise<void> {
    this.idle()
    await this.transport.query(`ALTER TABLE ${this.quote(table)} ADD COLUMN IF NOT EXISTS ` +
      `${this.quote(EPOCH_COLUMN)} bigint NOT NULL DEFAULT 0`)
  }
  async addConstraint(table: string, name: string, expression: string): Promise<void> {
    this.idle()
    if (name in (await this.metadata(table)).constraints) return
    const predicate = expression === '1' ? 'true' : expression === '0' ? 'false' : expression
    await this.transport.query(`ALTER TABLE ${this.quote(table)} ADD CONSTRAINT ${this.quote(name)} ` +
      `CHECK (${predicate}) NOT VALID`)
  }
  async dropConstraint(table: string, name: string): Promise<void> {
    this.idle()
    await this.transport.query(`ALTER TABLE ${this.quote(table)} DROP CONSTRAINT IF EXISTS ${this.quote(name)}`)
  }
  async drain(table: string, options: { projectId: string; hold: string }): Promise<void> {
    this.idle()
    await this.transport.transaction(async () => {
      await this.transport.query(`LOCK TABLE ${this.quote(table)} IN SHARE ROW EXCLUSIVE MODE`)
      held(await this.metadata(table), options.projectId, options.hold)
    })
  }
  async restore(table: string, options: { projectId: string; hold: string }): Promise<void> {
    held(await this.metadata(table), options.projectId, options.hold)
  }
}

export class ClickHouseFences implements FenceBackend {
  private readonly quote = QUOTE['clickhouse'] as (name: string) => string
  constructor(private readonly transport: ChTransport) {}
  private table(table: string): Promise<readonly Row[]> {
    return this.transport.query('SELECT toString(uuid) AS identity, engine, ' +
      'formatQuery(create_table_query) AS definition FROM system.tables ' +
      'WHERE database=currentDatabase() AND name=' + this.transport.literal(table))
  }
  async metadata(table: string): Promise<FenceMetadata> {
    const databases = await this.transport.query('SELECT engine FROM system.databases WHERE name=currentDatabase()')
    if (databases.length !== 1 || databases[0]?.['engine'] !== 'Atomic') {
      throw new MigrationRefused('write fences require a local Atomic ClickHouse database')
    }
    const rows = await this.table(table)
    if (rows.length !== 1) {
      throw new EngineError('write-fence table does not exist or is detached; resume the same operation')
    }
    const row = rows[0] as Row
    if (row['engine'] !== 'MergeTree' && row['engine'] !== 'ReplacingMergeTree') {
      throw new MigrationRefused('write fences support local MergeTree and ReplacingMergeTree tables')
    }
    const columns = await this.transport.query('SELECT type, default_kind, default_expression FROM system.columns ' +
      'WHERE database=currentDatabase() AND table=' + this.transport.literal(table) +
      ' AND name=' + this.transport.literal(EPOCH_COLUMN))
    const column = columns[0]
    const valid = columns.length === 1 && column?.['type'] === 'Int64' &&
      column['default_kind'] === 'DEFAULT' && column['default_expression'] === '0'
    const constraints: Record<string, string> = {}
    const pattern = /^\s*CONSTRAINT\s+[`"]?(__sde_f_[A-Za-z0-9_]+)[`"]?\s+CHECK\s+([^\n]+)/gm
    for (const match of String(row['definition']).matchAll(pattern)) {
      constraints[match[1] as string] = (match[2] as string).trimEnd().replace(/,$/, '')
    }
    return {
      identity: String(row['identity']), column: valid ? 'valid' : columns.length > 0 ? 'conflict' : 'absent',
      constraints,
    }
  }
  async addColumn(table: string): Promise<void> {
    await this.transport.command(`ALTER TABLE ${this.quote(table)} ADD COLUMN IF NOT EXISTS ` +
      `${this.quote(EPOCH_COLUMN)} Int64 DEFAULT 0`)
  }
  async addConstraint(table: string, name: string, expression: string): Promise<void> {
    await this.transport.command(`ALTER TABLE ${this.quote(table)} ADD CONSTRAINT IF NOT EXISTS ` +
      `${this.quote(name)} CHECK ${expression}`)
  }
  async dropConstraint(table: string, name: string): Promise<void> {
    await this.transport.command(`ALTER TABLE ${this.quote(table)} DROP CONSTRAINT IF EXISTS ${this.quote(name)}`)
  }
  private async drainLog(): Promise<void> {
    await this.transport.command(`CREATE TABLE IF NOT EXISTS ${this.quote(DRAIN_TABLE)} (` +
      'table_name String, table_uuid UUID, project_id FixedString(32), hold String) ' +
      'ENGINE=MergeTree ORDER BY (table_uuid, project_id, hold)')
    const columns = await this.transport.query('SELECT name,type FROM system.columns WHERE database=currentDatabase() ' +
      'AND table=' + this.transport.literal(DRAIN_TABLE) + ' ORDER BY name')
    const actual = columns.map((row) => [row['name'], row['type']])
    const expected = [['hold', 'String'], ['project_id', 'FixedString(32)'], ['table_name', 'String'], ['table_uuid', 'UUID']]
    if (JSON.stringify(actual) !== JSON.stringify(expected)) {
      throw new MigrationRefused('the reserved write-fence drain log has an incompatible schema')
    }
    const kinds = await this.transport.query('SELECT engine FROM system.tables WHERE database=currentDatabase() AND name=' +
      this.transport.literal(DRAIN_TABLE))
    if (kinds.length !== 1 || kinds[0]?.['engine'] !== 'MergeTree') {
      throw new MigrationRefused('the reserved write-fence drain log has an incompatible engine')
    }
  }
  async drain(table: string, options: { projectId: string; hold: string }): Promise<void> {
    const metadata = await this.metadata(table)
    held(metadata, options.projectId, options.hold)
    await this.drainLog()
    const values = [table, metadata.identity, options.projectId, options.hold].map(this.transport.literal)
    await this.transport.command(`INSERT INTO ${this.quote(DRAIN_TABLE)} ` +
      '(table_name, table_uuid, project_id, hold) SETTINGS async_insert=0, wait_for_async_insert=1 ' +
      `VALUES (${values.join(', ')})`)
    await this.transport.command(`DETACH TABLE ${this.quote(table)} PERMANENTLY SYNC`)
    await this.transport.command(`ATTACH TABLE ${this.quote(table)}`)
    const after = await this.metadata(table)
    if (after.identity !== metadata.identity) throw new EngineError('the write-fence table identity changed while draining it')
    held(after, options.projectId, options.hold)
  }
  async restore(table: string, options: { projectId: string; hold: string }): Promise<void> {
    if ((await this.table(table)).length > 0) {
      held(await this.metadata(table), options.projectId, options.hold)
      return
    }
    const literal = this.transport.literal
    const records = await this.transport.query(`SELECT DISTINCT toString(table_uuid) AS identity FROM ${this.quote(DRAIN_TABLE)} ` +
      `WHERE table_name=${literal(table)} AND project_id=${literal(options.projectId)} AND hold=${literal(options.hold)}`)
    const detached = await this.transport.query('SELECT toString(uuid) AS identity FROM system.detached_tables ' +
      `WHERE database=currentDatabase() AND table=${literal(table)} AND is_permanently=1`)
    if (records.length !== 1 || detached.length !== 1 || records[0]?.['identity'] !== detached[0]?.['identity']) {
      throw new MigrationRefused('no matching durable intent for this detached write-fence table')
    }
    await this.transport.command(`ATTACH TABLE ${this.quote(table)}`)
    const metadata = await this.metadata(table)
    if (metadata.identity !== records[0]?.['identity']) throw new EngineError('the restored write-fence table has another identity')
    held(metadata, options.projectId, options.hold)
  }
}


export async function fenceIO<T>(operation: () => Promise<T>): Promise<T> {
  try {
    return await operation()
  } catch (error) {
    if (error instanceof EngineError || error instanceof MigrationRefused) throw error
    const detail = error instanceof Error ? error.message : String(error)
    throw new EngineError('write-fence operation failed; inspect or resume its state: ' + detail)
  }
}

// --- Elasticsearch ---------------------------------------------------------------------------------

/** `request(method, path, body)`: the adapter's own transport, one request, no replay. */
export type ElasticsearchRequest = (method: string, path: string, body?: unknown) => Promise<[number, unknown]>

const PIPELINE_CONTRACT = 1
const HOLDS = /^(?:setup|hold_[0-9a-f]{32})$/
const BOUND = /^(min|max)_([1-9][0-9]*)$/

function indexName(index: string): string {
  const refusal = indexNameRefusal(index)
  if (refusal !== null) throw new MigrationRefused(`'${index}' is not an Elasticsearch index name: ${refusal}`)
  return index
}

/** The ingest pipeline holding `index`'s write fence: one per index, named after it. */
export function fencePipeline(index: string): string {
  return FENCE_PREFIX + indexName(index)
}

/**
 * The one processor of a fence pipeline, rendered from its constraints and nothing else - the same
 * bytes as Python's `fence_processor`, so that a fence one library provisions the other reads.
 */
export function fenceProcessor(constraints: Readonly<Record<string, string>>): Record<string, unknown> {
  const names = Object.keys(constraints).filter((name) => name.startsWith(FENCE_PREFIX)).sort()
  const held = names.some((name) => HOLDS.test(name.slice(FENCE_PREFIX.length)))
  const bounds = names.map((name) => BOUND.exec(name.slice(FENCE_PREFIX.length))).filter((match) => match !== null)
  const lower = bounds.filter((match) => match[1] === 'min').map((match) => Number(match[2])).sort((a, b) => a - b)
  const upper = bounds.filter((match) => match[1] === 'max').map((match) => Number(match[2])).sort((a, b) => a - b)
  const compare = [...lower.map((value) => `v < ${value}L`), ...upper.map((value) => `v > ${value}L`)]
  // No constant conditions inside a script: Painless refuses `if (true)` as an extraneous block.
  const script = held
    ? 'true'
    : bounds.length === 0
      ? 'false'
      : `def e = ctx['${EPOCH_COLUMN}']; ` +
        'if (!(e instanceof Integer || e instanceof Long)) { return true; } ' +
        'long v = ((Number) e).longValue(); ' +
        `return ${compare.join(' || ')};`
  const admits = lower.length > 0 && upper.length > 0
    ? `from ${Math.max(...lower)} to ${Math.min(...upper)}`
    : 'none recorded'
  const message = held
    ? 'a write barrier holds this index: no write is admitted until it is released'
    : `the write generation {{${EPOCH_COLUMN}}} is not one this index admits (generations ${admits})`
  return { fail: { if: script, message } }
}

function sameJson(left: unknown, right: unknown): boolean {
  return JSON.stringify(sortKeys(left)) === JSON.stringify(sortKeys(right))
}

function sortKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeys)
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, sortKeys((value as Record<string, unknown>)[key])]))
  }
  return value
}

/**
 * The write-fence protocol on Elasticsearch: a pipeline holds the constraints, a block drains. The
 * twin of Python's `ElasticsearchFences`; `docs/write-fences.md` gives the reasons.
 */
export class ElasticsearchFences implements FenceBackend {
  constructor(private readonly request: ElasticsearchRequest) {}

  private async call(method: string, path: string, body?: unknown): Promise<[number, unknown]> {
    try {
      return await this.request(method, path, body)
    } catch (error) {
      if (error instanceof EngineError) {
        throw new EngineError(`write-fence operation failed; inspect or resume its state: ${error.message}`)
      }
      throw error
    }
  }

  private async index(table: string): Promise<[string, Record<string, unknown>]> {
    // Every operation starts here or in addColumn and drain, which check the same: the name goes
    // into a path, so it is refused unless it is an index name ("a/../_cluster").
    indexName(table)
    const [status, body] = await this.call('GET', `/${table}/_settings`)
    const settings = status === 200
      ? ((body as Record<string, { settings?: { index?: Record<string, unknown> } }> | null)?.[table]?.settings?.index)
      : undefined
    if (settings === undefined || typeof settings['uuid'] !== 'string') {
      throw new EngineError(`write-fence index ${table} does not exist`)
    }
    return [settings['uuid'], settings]
  }

  private async pipeline(table: string): Promise<[Record<string, unknown> | null, number | null]> {
    const [status, body] = await this.call('GET', `/_ingest/pipeline/${fencePipeline(table)}`)
    if (status === 404) return [null, null]
    const document = status === 200 ? (body as Record<string, unknown> | null)?.[fencePipeline(table)] : undefined
    if (document === null || typeof document !== 'object') {
      throw new EngineError(`the write-fence pipeline of ${table} could not be read (${status})`)
    }
    const version = (document as Record<string, unknown>)['version']
    return [document as Record<string, unknown>, typeof version === 'number' && Number.isInteger(version) ? version : null]
  }

  private constraintsOf(table: string, uuid: string, document: Record<string, unknown> | null): Record<string, string> {
    if (document === null) return {}
    const outer = document['_meta']
    const meta = outer !== null && typeof outer === 'object' ? (outer as Record<string, unknown>)['sde'] : undefined
    const record = meta !== null && typeof meta === 'object' ? meta as Record<string, unknown> : null
    if (record === null || record['contract'] !== PIPELINE_CONTRACT || record['index'] !== table) {
      throw new MigrationRefused(`the pipeline ${fencePipeline(table)} is not a write fence of this library`)
    }
    // The fence of an earlier index of the same name: nothing of it applies to this one.
    if (record['uuid'] !== uuid) return {}
    const constraints = record['constraints']
    if (
      constraints === null || typeof constraints !== 'object' ||
      !Object.values(constraints).every((value) => typeof value === 'string')
    ) {
      throw new MigrationRefused(`the write-fence pipeline of ${table} has unreadable constraints`)
    }
    if (!sameJson(document['processors'], [fenceProcessor(constraints as Record<string, string>)])) {
      throw new MigrationRefused(
        `the write-fence pipeline of ${table} was changed outside this library: its processor is not ` +
          'the one its constraints render',
      )
    }
    return { ...(constraints as Record<string, string>) }
  }

  async metadata(table: string): Promise<FenceMetadata> {
    const [uuid, settings] = await this.index(table)
    const final = settings['final_pipeline']
    if (final !== undefined && final !== '_none' && final !== fencePipeline(table)) {
      throw new MigrationRefused(
        `index ${table} runs another final pipeline (${String(final)}); a write fence cannot be installed beside it`,
      )
    }
    const [status, body] = await this.call('GET', `/${table}/_mapping`)
    const properties = status === 200
      ? (body as Record<string, { mappings?: { properties?: Record<string, unknown> } }> | null)?.[table]?.mappings?.properties
      : undefined
    if (properties === undefined || properties === null || typeof properties !== 'object') {
      throw new EngineError(`the mapping of ${table} could not be read (${status})`)
    }
    const field = properties[EPOCH_COLUMN]
    const column: ColumnState = field === undefined ? 'absent' : sameJson(field, { type: 'long' }) ? 'valid' : 'conflict'
    const [document] = await this.pipeline(table)
    const constraints = this.constraintsOf(table, uuid, document)
    const blocked = String((settings['blocks'] as Record<string, unknown> | undefined)?.['write'] ?? 'false').toLowerCase() === 'true'
    const held = Object.keys(constraints).some((name) => HOLDS.test(name.slice(FENCE_PREFIX.length)))
    if (blocked && !held) {
      if (!Object.keys(constraints).some((name) => name.startsWith(FENCE_PREFIX + 'owner_'))) {
        throw new MigrationRefused(
          `index ${table} is write-blocked and carries no write fence of this library: somebody else ` +
            'blocked it, and this library does not lift that',
        )
      }
      constraints[FENCE_PREFIX + 'setup'] = '0'
    }
    return { identity: uuid, column, constraints }
  }

  async addColumn(table: string): Promise<void> {
    indexName(table)
    const [status] = await this.call('PUT', `/${table}/_mapping`, { properties: { [EPOCH_COLUMN]: { type: 'long' } } })
    if (status !== 200) throw new EngineError(`adding ${EPOCH_COLUMN} to ${table} failed (${status})`)
  }

  private async write(
    table: string,
    change: (constraints: Record<string, string>) => Record<string, string>,
  ): Promise<Record<string, string>> {
    const [uuid, settings] = await this.index(table)
    const [document, version] = await this.pipeline(table)
    const before = this.constraintsOf(table, uuid, document)
    const after = change({ ...before })
    const recorded = ((document?.['_meta'] as Record<string, unknown> | undefined)?.['sde'] ?? null) as Record<string, unknown> | null
    if (!sameJson(after, before) || recorded === null || recorded['uuid'] !== uuid) {
      const sorted = Object.fromEntries(Object.entries(after).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)))
      const body = {
        description: `Smart Data Engine write fence of ${table}`,
        version: (version ?? 0) + 1,
        _meta: { sde: { contract: PIPELINE_CONTRACT, index: table, uuid, constraints: sorted } },
        processors: [fenceProcessor(after)],
      }
      let path = `/_ingest/pipeline/${fencePipeline(table)}`
      if (document !== null) path += `?if_version=${version ?? 0}`
      const [status] = await this.call('PUT', path, body)
      if (status === 409) {
        throw new MigrationRefused(
          `the write fence of ${table} changed while this command ran: another executor is provisioning it. ` +
            'Serialize commands in one project state directory.',
        )
      }
      if (status !== 200) throw new EngineError(`writing the write fence of ${table} failed (${status})`)
    }
    if (settings['final_pipeline'] !== fencePipeline(table)) {
      const [status] = await this.call('PUT', `/${table}/_settings`, { 'index.final_pipeline': fencePipeline(table) })
      if (status !== 200) throw new EngineError(`installing the write fence on ${table} failed (${status})`)
    }
    return after
  }

  async addConstraint(table: string, name: string, expression: string): Promise<void> {
    await this.write(table, (constraints) => {
      if ((constraints[name] ?? expression) !== expression) {
        throw new MigrationRefused(`write fence constraint ${name} has an unexpected predicate`)
      }
      return { ...constraints, [name]: expression }
    })
  }

  async dropConstraint(table: string, name: string): Promise<void> {
    const after = await this.write(table, (constraints) =>
      Object.fromEntries(Object.entries(constraints).filter(([key]) => key !== name)))
    if (!Object.keys(after).some((key) => HOLDS.test(key.slice(FENCE_PREFIX.length)))) {
      // The last hold: the shard-level block goes with it.
      const [, settings] = await this.index(table)
      if (String((settings['blocks'] as Record<string, unknown> | undefined)?.['write'] ?? 'false').toLowerCase() === 'true') {
        const [status] = await this.call('PUT', `/${table}/_settings`, { 'index.blocks.write': false })
        if (status !== 200) throw new EngineError(`lifting the write block of ${table} failed (${status})`)
      }
    }
  }

  async drain(table: string, options: { projectId: string; hold: string }): Promise<void> {
    // Repeated on a retry: the block proves that every operation already on the shards has finished.
    indexName(table)
    let [status, body] = await this.call('PUT', `/${table}/_block/write`)
    let response = (body ?? {}) as { acknowledged?: unknown; shards_acknowledged?: unknown; indices?: { name?: string; blocked?: unknown }[] }
    if (status === 200 && response.acknowledged === true && (response.indices ?? []).length === 0) {
      // Already blocked, and a block added again proves nothing: lifted and added again, while the
      // hold already in the pipeline refuses every new write at its arrival.
      const [lifted] = await this.call('PUT', `/${table}/_settings`, { 'index.blocks.write': false })
      if (lifted !== 200) throw new EngineError(`renewing the write barrier on ${table} failed (${lifted})`)
      ;[status, body] = await this.call('PUT', `/${table}/_block/write`)
      response = (body ?? {}) as typeof response
    }
    if (
      status !== 200 || response.acknowledged !== true || response.shards_acknowledged !== true ||
      !(response.indices ?? []).some((entry) => entry.name === table && entry.blocked === true)
    ) {
      throw new EngineError(
        `the write barrier on ${table} was not confirmed by every shard (${status}); the index may be closed ` +
          'to writes - inspect or resume',
      )
    }
    // Every write the block waited for has finished on its shards, but one that waits for a refresh
    // may not be visible yet: a copy and a comparison read by searching, and a row they cannot see
    // is missing from both at once. So the barrier makes them visible.
    const [refreshed, answer] = await this.call('POST', `/${table}/_refresh`)
    const failed = (answer as { _shards?: { failed?: unknown } } | null)?._shards?.failed
    if (refreshed !== 200 || failed === undefined || Number(failed) !== 0) {
      throw new EngineError(
        `the writes before the barrier on ${table} could not be made visible (${refreshed}); inspect or resume`,
      )
    }
    held(await this.metadata(table), options.projectId, options.hold)
  }

  async restore(table: string, options: { projectId: string; hold: string }): Promise<void> {
    // Nothing is detached on Elasticsearch: the block is the drain, and the hold its record.
    held(await this.metadata(table), options.projectId, options.hold)
  }
}

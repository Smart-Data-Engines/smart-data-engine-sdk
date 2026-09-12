/** Native fencing DDL for dedicated provisioning connections. No placement decision lives here. */
import { EngineError, MigrationRefused } from '../errors.js'
import { QUOTE } from '../schema.js'
import type { Row } from '../session.js'
import { DRAIN_TABLE, EPOCH_COLUMN, fenceState, type FenceBackend, type FenceMetadata } from '../write-fence.js'

interface PgTransport {
  query(sql: string, values?: readonly unknown[]): Promise<readonly Row[]>
  transaction(body: () => Promise<void>): Promise<void>
  busy(): boolean
}
interface ChTransport {
  query(sql: string): Promise<readonly Row[]>
  command(sql: string): Promise<void>
  insert(table: string, rows: readonly Row[]): Promise<void>
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
    await this.transport.insert(DRAIN_TABLE, [{ table_name: table, table_uuid: metadata.identity,
      project_id: options.projectId, hold: options.hold }])
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

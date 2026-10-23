/**
 * An adapter for Elasticsearch: a document per row, an index per entity, and a text index.
 *
 * The TypeScript twin of `sde.engines.elasticsearch` (format contract 7m). The mapping and the
 * documents come from the shared functions the vectors hold (`schema.ts`, `elasticsearch-document.ts`),
 * so two libraries writing one index agree to the byte; this file is the transport and the queries.
 *
 * - A value backed by a `long` stays exact both ways: responses are parsed with integers past 2^53
 *   as `bigint` and requests write a `bigint` as its digits (`_json.ts`). `JSON.parse` would turn a
 *   microsecond after 2255 into another microsecond.
 * - A search's candidates come from the token fields the library writes beside each text field.
 *   The engine's analyzer decides nothing.
 * - TLS is verified against `ca_cert`, with the same peer checks as the ClickHouse adapter. Plain
 *   HTTP is accepted to a loopback address only.
 * - No request is replayed after it was sent: a lost response is an outcome nobody knows. A pooled
 *   connection idle for long is closed by us before the server or a balancer closes it under a
 *   request.
 */

import { Agent as HttpAgent, request as httpRequest, type IncomingMessage } from 'node:http'
import { Agent as HttpsAgent, request as httpsRequest } from 'node:https'
import { isIP } from 'node:net'
import { isAbsolute } from 'node:path'

import { UsageGate } from '../_usage.js'
import { canonicalString } from '../canonical.js'
import { decimalBound, document, documentId, stored, type StoredValue } from '../elasticsearch-document.js'
import { EngineError } from '../errors.js'
import type { PhysicalLayout } from '../placement.js'
import { keyColumns, sameWidth } from '../migration.js'
import { readRow, type ReadFilter, type ReadPlan } from '../query.js'
import { elasticsearchMapping, elasticsearchProperty, indexNameRefusal, schemaStatements, TOKENS_FIELD } from '../schema.js'
import type { Row } from '../session.js'
import { Timestamp } from '../timestamp.js'
import { verifiedTlsOptions } from './_clickhouse-connection.js'
import { parseExactJson, stringifyExactJson } from './_json.js'
import { ElasticsearchFences } from './_write-fences.js'
import { EPOCH_COLUMN, WriteFence } from '../write-fence.js'

/** The most this adapter reads of one response. A page is at most 1001 documents. */
export const MAX_RESPONSE_BYTES = 256 * 1024 * 1024
const OPTIONS = new Set(['api_key', 'ca_cert', 'connect_timeout', 'send_receive_timeout', 'verify'])
const IDLE_MS = 20_000
const WATERMARK = 'sde_map_state'
const BACKFILL = 'sde_backfill_state'

/**
 * Documents per search when a migration reads in key order: a copy's chunk, a window of the copy
 * compared with it, or the keys counted towards a resume point.
 */
export const KEY_PAGE = 1000

/** The mapping of `sde_map_state` here: one document per map version applied. */
export const WATERMARK_MAPPING = {
  dynamic: 'strict',
  properties: { map_version: { type: 'long' }, model_version: { type: 'keyword' } },
} as const

/** The mapping of `sde_backfill_state` here: one document per chunk copied, never updated. */
export const BACKFILL_MAPPING = {
  dynamic: 'strict',
  properties: { entity: { type: 'keyword' }, materialization: { type: 'keyword' }, rows_copied: { type: 'long' } },
} as const

/** What a staging writes on the index it creates: its project, its stage and the index's name. */
const CREATION_MARKER = /^sde\.stage1\.[0-9a-f]{32}\.[0-9a-f]{32}\.[0-9a-f]{16}$/
/** A shard that fails or times out fails the search, rather than leaving its rows out. */
const PARTIAL_REFUSED = '?allow_partial_search_results=false'

export interface ElasticsearchConnection {
  readonly host: string
  readonly port: number
  readonly tls: boolean
  readonly authorization: string | null
  readonly caCert: string | null
  readonly connectTimeoutMs: number
  readonly sendReceiveTimeoutMs: number
}

function refuse(reason: string): never {
  throw new EngineError(`${reason}; no connection was opened`)
}

function loopback(host: string): boolean {
  if (host === 'localhost' || host === '::1') return true
  return isIP(host) === 4 && host.startsWith('127.')
}

function seconds(name: string, text: string | undefined, fallback: number): number {
  if (text === undefined) return fallback * 1000
  const value = Number(text)
  if (!/^\d+(\.\d+)?$/.test(text) || !Number.isFinite(value) || value < 0.001 || value > 2147483.647) {
    return refuse(`${name} is between 0.001 and 2147483.647 seconds`)
  }
  return value * 1000
}

/** The connection profile, refused whole on anything it does not define (engine-connections.md). */
export function parseDsn(dsn: string): ElasticsearchConnection {
  if (typeof dsn !== 'string' || /[\s\\]/.test(dsn)) {
    refuse('an Elasticsearch DSN is one URL without whitespace or backslashes')
  }
  let url: URL
  try {
    url = new URL(dsn)
  } catch {
    return refuse('an Elasticsearch DSN is a URL')
  }
  const tls = url.protocol === 'elasticsearch+https:'
  if (!tls && url.protocol !== 'elasticsearch+http:') {
    refuse('an Elasticsearch DSN starts elasticsearch+http:// or elasticsearch+https://')
  }
  if (url.hash !== '' || !['', '/'].includes(url.pathname)) {
    refuse('an Elasticsearch DSN has no path and no fragment')
  }
  const host = url.hostname.startsWith('[') ? url.hostname.slice(1, -1) : url.hostname
  if (host === '' || host.includes('%')) refuse('an Elasticsearch DSN names a host, without an IPv6 zone')
  if (!tls && !loopback(host)) {
    refuse('elasticsearch+http is accepted only to a loopback address; use elasticsearch+https')
  }
  const options = new Map<string, string>()
  for (const [name, value] of url.searchParams) {
    if (!OPTIONS.has(name)) refuse(`unknown Elasticsearch DSN option '${name}'`)
    if (options.has(name)) refuse(`the Elasticsearch DSN option '${name}' is given twice`)
    options.set(name, value)
  }
  if ((options.get('verify') ?? 'true') !== 'true') {
    refuse('verify can only be true: disabling TLS verification is refused')
  }
  if (!tls && (options.has('ca_cert') || options.has('verify'))) {
    refuse('ca_cert and verify belong to elasticsearch+https')
  }
  const caCert = options.get('ca_cert') ?? null
  if (caCert !== null && !isAbsolute(caCert)) refuse('ca_cert is an absolute path')
  // A user part is present when the authority holds an `@`, empty or not - as Python reads it. The
  // URL parser reports an empty user as no user, which would drop `:secret@` and connect without it.
  const authority = (dsn.split('//', 2)[1] ?? '').split(/[/?#]/, 1)[0] ?? ''
  const user = authority.includes('@') ? decoded(url.username) : null
  const password = decoded(url.password)
  if (user !== null && options.has('api_key')) refuse('an Elasticsearch DSN gives a user or an api_key, not both')
  let authorization: string | null = null
  if (user !== null) {
    if (user === '' || user.includes(':')) refuse('an Elasticsearch user is nonempty and has no colon')
    authorization = 'Basic ' + Buffer.from(`${user}:${password}`, 'utf8').toString('base64')
  } else if (options.has('api_key')) {
    const key = options.get('api_key') as string
    if (!/^[A-Za-z0-9+/]+={0,2}$/.test(key) || key.length % 4 !== 0) {
      refuse('api_key is the encoded key Elasticsearch returns')
    }
    authorization = `ApiKey ${key}`
  }
  const port = url.port === '' ? 9200 : Number(url.port)
  const connection = {
    host, port, tls, caCert,
    connectTimeoutMs: seconds('connect_timeout', options.get('connect_timeout'), 10),
    sendReceiveTimeoutMs: seconds('send_receive_timeout', options.get('send_receive_timeout'), 30),
  }
  // Not enumerable: whatever prints or serialises the profile does not print the credentials.
  Object.defineProperty(connection, 'authorization', { value: authorization, enumerable: false })
  return Object.freeze(connection as ElasticsearchConnection)
}

function decoded(text: string): string {
  try {
    return decodeURIComponent(text)
  } catch {
    return refuse('the user and password of an Elasticsearch DSN are percent-encoded UTF-8')
  }
}

function readValue(kind: string, value: unknown): unknown {
  if (value === null || value === undefined) return null
  switch (kind) {
    case 'boolean':
    case 'keyword':
    case 'keyword/uuid':
    case 'date':
      return value
    case 'integer':
    case 'float':
    case 'double':
      return Number(value)
    case 'long':
      return BigInt(value as number | bigint)
    case 'long/timestamp':
    case 'long/timestamptz':
      return Timestamp.fromEpochMicroseconds(BigInt(value as number | bigint))
  }
  const decimal = /^long\/decimal\((\d+),(\d+)\)$/.exec(kind)
  if (decimal !== null) {
    const scale = Number(decimal[2])
    const coefficient = BigInt(value as number | bigint)
    const absolute = coefficient < 0n ? -coefficient : coefficient
    const digits = absolute.toString().padStart(scale + 1, '0'), point = digits.length - scale
    return (coefficient < 0n ? '-' : '') +
      (scale === 0 ? digits : digits.slice(0, point) + '.' + digits.slice(point))
  }
  throw new EngineError(`'${kind}' is not a column type this adapter reads`)
}

function layoutType(name: string, field: Readonly<Record<string, unknown>>): string {
  const native = String(field['type'])
  const meta = field['meta'] as Readonly<Record<string, unknown>> | undefined
  const neutral = meta?.['sde_type']
  const kind = typeof neutral === 'string' ? `${native}/${neutral}` : native
  elasticsearchProperty(kind)
  if (native === 'date' && field['format'] !== 'strict_date') {
    throw new EngineError(`${name} is a date field without the strict_date format`)
  }
  return kind
}

interface Index {
  readonly name: string
  readonly entity: string
  readonly columns: Readonly<Record<string, string>>
  readonly key: readonly string[]
  /** The index has a write generation field: every document a session writes carries one. */
  readonly fenced: boolean
}

function rowOf(index: Index, source: Readonly<Record<string, unknown>>): Row {
  return Object.fromEntries(
    Object.entries(index.columns).map(([column, kind]) => [column, readValue(kind, source[column])]),
  )
}

/** A thin adapter over Elasticsearch's REST API. Executes decisions, makes none. */
export class ElasticsearchEngine {
  readonly dialect = 'elasticsearch'
  readonly summaryRefusal =
    'this engine summarizes nothing exactly: its aggregations add in double precision, and a ' +
    'summary of this library is exact. Refused rather than rounded.'
  private readonly usage = new UsageGate()
  private readonly connection: ElasticsearchConnection
  private agent: HttpAgent | HttpsAgent | null = null
  private readonly indexes = new Map<string, Index>()

  constructor(dsn: string) {
    this.connection = parseDsn(dsn)
    if (this.connection.tls) {
      // Read and checked now, so a bad CA file is refused before any socket opens.
      verifiedTlsOptions(this.connection.host, this.connection.caCert, 'Elasticsearch')
    }
  }

  toString(): string {
    const { host, port, tls, authorization } = this.connection
    const login = authorization === null ? 'none' : authorization.split(' ', 1)[0]
    return `ElasticsearchEngine(${tls ? 'https' : 'http'} ${host}:${port}, login=${login})`
  }

  /** Never the credentials, whatever inspects this object. */
  [Symbol.for('nodejs.util.inspect.custom')](): string {
    return this.toString()
  }

  toJSON(): string {
    return this.toString()
  }

  /**
   * The write-fence protocol on this index (`write-fences.md`), for a provisioning login. Its pipeline
   * and its write block need the cluster privilege `manage_pipeline` and `manage` on the index;
   * reading its state at session open needs `read_pipeline`.
   */
  writeFence(table: string, options: { projectId: string }): WriteFence {
    this.indexes.delete(table)
    return new WriteFence(new ElasticsearchFences(async (method, path, body) => {
      // A fence changes the index's mapping and settings: forget what was read of it.
      this.indexes.clear()
      return this.request(method, path, body)
    }), table, options)
  }

  /** Open the pool and authenticate - `_security/_authenticate`, which any login may ask. */
  async connect(): Promise<void> {
    return this.usage.operation(async () => {
      if (this.agent !== null) return
      const c = this.connection
      // `timeout` on the agent is the idle limit of a pooled socket: Node destroys a free socket
      // whose timer fires, with one listener of its own. A request sets its own timeout over it.
      this.agent = c.tls
        ? new HttpsAgent({
          keepAlive: true, maxSockets: 1, timeout: IDLE_MS,
          ...verifiedTlsOptions(c.host, c.caCert, 'Elasticsearch'),
          minVersion: 'TLSv1.2',
        })
        : new HttpAgent({ keepAlive: true, maxSockets: 1, timeout: IDLE_MS })
      try {
        const [status, body] = await this.request('GET', '/_security/_authenticate')
        if (status !== 200) throw new EngineError(`could not connect to Elasticsearch: ${errorOf(status, body)}`)
      } catch (error) {
        this.drop()
        throw error instanceof EngineError
          ? error
          : new EngineError(`could not connect to Elasticsearch at ${c.host}:${c.port}`)
      }
    })
  }

  async close(): Promise<void> {
    return this.usage.operation(async () => this.drop())
  }

  private drop(): void {
    this.agent?.destroy()
    this.agent = null
  }

  private request(
    method: string,
    path: string,
    body?: unknown,
    ndjson?: string,
  ): Promise<[number, unknown]> {
    const agent = this.agent
    if (agent === null) return Promise.reject(new EngineError('not connected; call connect() first'))
    const c = this.connection
    const headers: Record<string, string> = { Accept: 'application/json' }
    if (c.authorization !== null) headers['Authorization'] = c.authorization
    let payload: Buffer | undefined
    if (ndjson !== undefined) {
      payload = Buffer.from(ndjson, 'utf8')
      headers['Content-Type'] = 'application/x-ndjson'
    } else if (body !== undefined) {
      payload = Buffer.from(stringifyExactJson(body), 'utf8')
      headers['Content-Type'] = 'application/json'
    }
    if (payload !== undefined) headers['Content-Length'] = String(payload.length)
    const send = c.tls ? httpsRequest : httpRequest
    return new Promise((resolve, reject) => {
      const lost = (why: string) => reject(new EngineError(
        `Elasticsearch ${method} ${path.split('?')[0]} lost its response (${why}); whether it ` +
          'took effect is unknown, and it is not replayed',
      ))
      const call = send({
        host: c.host, port: c.port, method, path, headers, agent,
        timeout: c.sendReceiveTimeoutMs,
      }, (response: IncomingMessage) => {
        const chunks: Buffer[] = []
        let size = 0
        response.on('data', (chunk: Buffer) => {
          size += chunk.length
          if (size > MAX_RESPONSE_BYTES) {
            response.destroy()
            reject(new EngineError(`a response to ${method} was over ${MAX_RESPONSE_BYTES} bytes`))
            return
          }
          chunks.push(chunk)
        })
        response.on('error', () => lost('the response broke off'))
        response.on('end', () => {
          const status = response.statusCode ?? 0
          if (status >= 300 && status < 400) {
            reject(new EngineError(`Elasticsearch answered ${status}; no redirect is followed`))
            return
          }
          const text = Buffer.concat(chunks).toString('utf8')
          if (text === '') {
            resolve([status, null])
            return
          }
          try {
            resolve([status, parseExactJson(text)])
          } catch {
            reject(new EngineError(`Elasticsearch answered ${status} with something that is not JSON`))
          }
        })
      })
      call.on('socket', (socket) => {
        if (socket.connecting) {
          socket.setTimeout(c.connectTimeoutMs)
          socket.once(c.tls ? 'secureConnect' : 'connect', () => socket.setTimeout(c.sendReceiveTimeoutMs))
        }
      })
      call.on('timeout', () => call.destroy(new Error('timeout')))
      call.on('error', (error: NodeJS.ErrnoException) => lost(error.code ?? error.name))
      call.end(payload)
    })
  }

  private path(index: string, ...rest: string[]): string {
    const refusal = indexNameRefusal(index)
    if (refusal !== null) throw new EngineError(`'${index}' is not an Elasticsearch index name: ${refusal}`)
    return '/' + [encodeURIComponent(index), ...rest].join('/')
  }

  // --- schema ----------------------------------------------------------------------------------

  /** Create each index that is missing, change none that exists, and check them all. */
  async ensureSchema(
    layout: PhysicalLayout,
    options: { readonly keys: Readonly<Record<string, readonly string[]>> },
  ): Promise<readonly []> {
    return this.usage.operation(async () => {
      for (const statement of schemaStatements(layout, { keys: options.keys, dialect: this.dialect })) {
        const parsed = JSON.parse(statement) as { index: string; mappings: unknown }
        const [status, body] = await this.request('PUT', this.path(parsed.index), { mappings: parsed.mappings })
        if (status !== 200 && !alreadyExists(status, body)) {
          throw new EngineError(`creating index ${parsed.index} failed: ${errorOf(status, body)}`)
        }
      }
      await this.verifySchema(layout, options.keys)
      return []
    })
  }

  /** Check the existing indexes without creating anything. */
  async validateSchema(
    layout: PhysicalLayout,
    options: { readonly keys?: Readonly<Record<string, readonly string[]>> } = {},
  ): Promise<readonly []> {
    return this.usage.operation(async () => {
      const keys: Record<string, readonly string[]> = { ...(options.keys ?? {}) }
      for (const [entity, index] of Object.entries(layout.tables)) {
        if (keys[entity] === undefined) keys[entity] = (await this.index(index)).key
      }
      await this.verifySchema(layout, keys)
      return []
    })
  }

  private async verifySchema(
    layout: PhysicalLayout,
    keys: Readonly<Record<string, readonly string[]>>,
  ): Promise<void> {
    for (const [entity, index] of Object.entries(layout.tables)) {
      this.indexes.delete(index)
      const found = await this.index(index)
      const expected = elasticsearchMapping(layout, entity, keys) as {
        _meta: { sde: { key: string[] } }
      }
      const wanted = { ...(layout.columns[entity] ?? {}) }
      const sameColumns = JSON.stringify(Object.entries(found.columns).sort()) ===
        JSON.stringify(Object.entries(wanted).sort())
      const sameKey = JSON.stringify(found.key) === JSON.stringify(expected._meta.sde.key)
      if (!sameColumns || !sameKey || found.entity !== entity) {
        throw new EngineError(
          `index ${index} exists for ${found.entity} with fields ${JSON.stringify(found.columns)} ` +
            `and key ${JSON.stringify(found.key)}, and the layout needs ${entity} with ` +
            `${JSON.stringify(wanted)} and key ${JSON.stringify(expected._meta.sde.key)}. ` +
            'Elasticsearch changes no field in place.',
        )
      }
    }
  }

  private async index(name: string): Promise<Index> {
    const known = this.indexes.get(name)
    if (known !== undefined) return known
    const [status, body] = await this.request('GET', this.path(name, '_mapping'))
    if (status === 404) throw new EngineError(`index ${name} does not exist; prepare the schema first`)
    const document = body as Record<string, { mappings?: Record<string, unknown> }> | null
    const mapping = status === 200 ? document?.[name]?.mappings : undefined
    if (mapping === undefined) throw new EngineError(`the mapping of ${name} could not be read: ${errorOf(status, body)}`)
    const meta = (mapping['_meta'] as { sde?: { key?: unknown; entity?: unknown; stage?: unknown } } | undefined)?.sde
    const key = meta?.key
    if (mapping['dynamic'] !== 'strict' || !Array.isArray(key) || key.length === 0) {
      throw new EngineError(
        `index ${name} was not created by this library: its mapping is not strict or names no ` +
          'key in _meta.sde',
      )
    }
    const properties = (mapping['properties'] ?? {}) as Record<string, Record<string, unknown>>
    const columns: Record<string, string> = {}
    for (const [column, field] of Object.entries(properties).sort(([a], [b]) => (a < b ? -1 : 1))) {
      if (column !== TOKENS_FIELD && column !== EPOCH_COLUMN) columns[column] = layoutType(column, field)
    }
    // The write generation is the fence's field, not the layout's: present exactly as the fence adds
    // it, or absent.
    const fenced = EPOCH_COLUMN in properties
    if (fenced && JSON.stringify(properties[EPOCH_COLUMN]) !== JSON.stringify({ type: 'long' })) {
      throw new EngineError(`index ${name} maps ${EPOCH_COLUMN} as something other than a long`)
    }
    if (key.some((column) => typeof column !== 'string' || columns[column] === undefined)) {
      throw new EngineError(`index ${name} names a key field it does not map`)
    }
    const entity = meta?.entity
    if (typeof entity !== 'string' || entity === '') throw new EngineError(`index ${name} names no entity in _meta.sde`)
    // A staging's index says which staging created it, in the request that created it - as a SQL
    // staging comments its table in the transaction that creates it. Its form is fixed.
    const stage = meta?.stage
    if (stage !== undefined && (typeof stage !== 'string' || !CREATION_MARKER.test(stage))) {
      throw new EngineError(`index ${name} carries a staging marker that is not one`)
    }
    // The whole mapping, not only each field's type: a normalizer, a null_value, an ignore_above, a
    // token field mapped as text or a disabled _source would each change an answer while every type
    // still matched. It is what this library creates or nothing.
    const layout = { tables: { [entity]: name }, columns: { [entity]: columns }, indexes: [], partitionBy: {} }
    const rendered = elasticsearchMapping(layout, entity, { [entity]: key as string[] }) as {
      properties: Record<string, unknown>
      _meta: { sde: Record<string, unknown> }
    }
    if (fenced) rendered.properties[EPOCH_COLUMN] = { type: 'long' }
    if (stage !== undefined) rendered._meta.sde['stage'] = stage
    if (!sameJson(rendered, mapping)) {
      throw new EngineError(
        `index ${name} has a mapping this library would not create: something beyond each ` +
          "field's type differs - a field parameter, the token fields or _source - and each of " +
          'those can change an answer',
      )
    }
    const index = { name, entity, columns, key: key as string[], fenced }
    this.indexes.set(name, index)
    return index
  }

  // --- writes ----------------------------------------------------------------------------------

  /** One document under its key's `_id`; a second save of the key replaces the first. */
  async insert(table: string, values: Readonly<Row>): Promise<void> {
    return this.usage.operation(async () => {
      if (Object.keys(values).length === 0) throw new EngineError('nothing to insert')
      const index = await this.index(table)
      const { id, source } = documentOf(index, values)
      const [status, body] = await this.request('PUT', this.path(table, '_doc', id) + '?refresh=wait_for', source)
      if (status !== 200 && status !== 201) throw new EngineError(`insert into ${table} failed: ${writeErrorOf(status, body)}`)
    })
  }

  /** One `_bulk` request. Not atomic: a refusal says how many were written, never which values. */
  async insertMany(table: string, rows: readonly Readonly<Row>[]): Promise<void> {
    return this.usage.operation(async () => {
      if (rows.length === 0) return
      const index = await this.index(table)
      const lines: string[] = []
      for (const row of rows) {
        const { id, source } = documentOf(index, row)
        lines.push(JSON.stringify({ index: { _index: table, _id: id } }), stringifyExactJson(source))
      }
      const [status, body] = await this.request('POST', '/_bulk?refresh=wait_for', undefined, lines.join('\n') + '\n')
      const result = body as { errors?: boolean; items?: { index?: { status?: number; error?: { type?: string } } }[] } | null
      if (status !== 200 || result === null) {
        throw new EngineError(`batch insert into ${table} failed: ${writeErrorOf(status, body)}`)
      }
      if (result.errors === true) {
        const items = (result.items ?? []).map((item) => item.index ?? {})
        const failed = items.filter((item) => (item.status ?? 500) >= 300)
        throw new EngineError(
          `batch insert into ${table}: ${failed.length} of ${rows.length} documents were refused ` +
            `(${failed[0]?.error?.type ?? 'unknown'}) and ${items.length - failed.length} were written`,
        )
      }
    })
  }

  // --- reads -----------------------------------------------------------------------------------

  /** One document by its key's `_id`, immediate, and checked to be that key. */
  async get(table: string, key: Readonly<Row>): Promise<Row | null> {
    return this.usage.operation(async () => {
      const index = await this.index(table)
      if (JSON.stringify(Object.keys(key).sort()) !== JSON.stringify([...index.key].sort())) {
        throw new EngineError(`a read of ${table} by key names exactly ${JSON.stringify(index.key)}`)
      }
      const values = index.key.map((column) => stored(index.columns[column] as string, key[column]))
      const [status, body] = await this.request('GET', this.path(table, '_doc', documentId(values)))
      if (status === 404) return null
      const source = (body as { _source?: Record<string, unknown> } | null)?._source
      if (status !== 200 || source === undefined) throw new EngineError(`select from ${table} failed: ${errorOf(status, body)}`)
      const same = index.key.every((column, position) =>
        normalised(source[column]) === normalised(values[position] as StoredValue))
      // A document under this _id that holds another key is not the row asked for.
      return same ? rowOf(index, source) : null
    })
  }

  private filter(index: Index, predicate: ReadFilter): Record<string, unknown> {
    const name = predicate.column.name
    const kind = index.columns[name] as string
    if (predicate.operation === 'eq' && predicate.value === null) {
      return { bool: { must_not: [{ exists: { field: name } }] } }
    }
    let value: unknown
    if (kind.startsWith('long/decimal(')) {
      const bound = decimalBound(kind, predicate.value, predicate.operation)
      if (bound === null) return { bool: { must_not: [{ match_all: {} }] } }
      value = bound
    } else {
      value = stored(kind, predicate.value)
    }
    if (predicate.operation === 'eq') return { term: { [name]: value } }
    return { range: { [name]: { [predicate.operation === 'ge' ? 'gte' : 'lt']: value } } }
  }

  /** Rows past `plan.after` in the plan's order, nulls last: the keyset rule of the SQL dialects. */
  private after(index: Index, plan: ReadPlan): Record<string, unknown> {
    const alternatives: Record<string, unknown>[] = []
    const later = plan.descending ? 'lt' : 'gt'
    const after = plan.after as readonly unknown[]
    plan.order.forEach((column, position) => {
      const value = after[position]
      if (value === null || value === undefined) return
      const clauses = plan.order.slice(0, position).map((previous, earlier) =>
        this.filter(index, { column: previous, operation: 'eq', value: after[earlier] }))
      const beyond = { range: { [column.name]: { [later]: stored(index.columns[column.name] as string, value) } } }
      const missing = { bool: { must_not: [{ exists: { field: column.name } }] } }
      clauses.push({ bool: { should: [beyond, missing], minimum_should_match: 1 } })
      alternatives.push({ bool: { filter: clauses } })
    })
    if (alternatives.length === 0) return { bool: { must_not: [{ match_all: {} }] } }
    return { bool: { should: alternatives, minimum_should_match: 1 } }
  }

  private async page(table: string, plan: ReadPlan, extra: readonly Record<string, unknown>[] = []): Promise<Row[]> {
    const index = await this.index(table)
    const clauses = plan.filters.map((predicate) => this.filter(index, predicate))
    if (plan.after !== null) clauses.push(this.after(index, plan))
    clauses.push(...extra)
    const order = plan.descending ? 'desc' : 'asc'
    const body = {
      query: { bool: { filter: clauses } },
      sort: plan.order.map((column) => ({ [column.name]: { order, missing: '_last' } })),
      size: plan.limit + 1,
      track_total_hits: false,
    }
    const [status, response] = await this.request('POST', this.path(table, '_search') + PARTIAL_REFUSED, body)
    const hits = (response as { hits?: { hits?: { _source?: Record<string, unknown> }[] } } | null)?.hits?.hits
    if (status !== 200 || hits === undefined) throw new EngineError(`logical scan of ${table} failed: ${errorOf(status, response)}`)
    whole(response, `a page of ${table}`)
    return hits.map((hit) => readRow(plan.columns, rowOf(index, hit._source ?? {})))
  }

  async selectRows(table: string, plan: ReadPlan): Promise<Row[]> {
    return this.usage.operation(async () => this.page(table, plan))
  }

  /** A page of rows whose token fields hold every token of the query, in the plan's order. */
  async searchRows(table: string, plan: ReadPlan, query: readonly string[], fields: readonly string[]): Promise<Row[]> {
    return this.usage.operation(async () => {
      const index = await this.index(table)
      const bad = fields.filter((field) => index.columns[field] !== 'keyword')
      if (bad.length > 0) throw new EngineError(`${table} has no text field ${JSON.stringify(bad)} to search`)
      const extra = query.map((token) => ({
        bool: {
          should: fields.map((field) => ({ term: { [`${TOKENS_FIELD}.${field}`]: token } })),
          minimum_should_match: 1,
        },
      }))
      return this.page(table, plan, extra)
    })
  }

  async countRows(table: string, plan: ReadPlan): Promise<bigint> {
    return this.usage.operation(async () => {
      const index = await this.index(table)
      const body = { query: { bool: { filter: plan.filters.map((predicate) => this.filter(index, predicate)) } } }
      const [status, response] = await this.request('POST', this.path(table, '_count'), body)
      const count = (response as { count?: number | bigint } | null)?.count
      if (status !== 200 || count === undefined) throw new EngineError(`count of ${table} failed: ${errorOf(status, response)}`)
      whole(response, `the count of ${table}`)
      return BigInt(count)
    })
  }

  // --- migration -------------------------------------------------------------------------------
  //
  // `Migratable`, so a group can be copied onto this engine and off it. A migration reads in key
  // order, and a key is never null: the position is a filter, as a page's is (§7m), so the engine's
  // sort values stand in for no value. A copy is `create`, so a document already there stays -
  // PostgreSQL's ON CONFLICT DO NOTHING, not an overwrite.

  /** Keys beyond `bound` in `order` - after it when `above`, before it otherwise - as stored. */
  private keyset(order: readonly string[], bound: readonly StoredValue[], above: boolean, inclusive: boolean): Record<string, unknown> {
    const alternatives: Record<string, unknown>[] = order.map((column, position) => {
      const clauses: Record<string, unknown>[] = order.slice(0, position).map((earlier, at) => ({ term: { [earlier]: bound[at] } }))
      clauses.push(beyond(column, bound[position] as StoredValue, above))
      return { bool: { filter: clauses } }
    })
    if (inclusive) alternatives.push({ bool: { filter: order.map((column, at) => ({ term: { [column]: bound[at] } })) } })
    return { bool: { should: alternatives, minimum_should_match: 1 } }
  }

  private storedKey(index: Index, order: readonly string[], bound: readonly unknown[], name: string): StoredValue[] {
    sameWidth(bound, order, name)
    if (bound.some((value) => value === null || value === undefined)) {
      throw new EngineError(`${name} holds a null, and no key does`)
    }
    return order.map((column, at) => stored(index.columns[column] as string, bound[at]))
  }

  private async ordered(table: string, order: readonly string[]): Promise<[Index, readonly string[]]> {
    const index = await this.index(table)
    const columns = keyColumns(order, table)
    const unknown = columns.filter((column) => index.columns[column] === undefined)
    if (unknown.length > 0) throw new EngineError(`${table} has no field ${JSON.stringify(unknown)} to order by`)
    return [index, columns]
  }

  /** One search in key order: the sources of up to `size` documents after `after` and up to `upto`. */
  private async keyPage(
    table: string,
    order: readonly string[],
    after: readonly StoredValue[] | null,
    upto: readonly StoredValue[] | null,
    size: number,
    fields?: readonly string[],
  ): Promise<Record<string, unknown>[]> {
    const clauses: Record<string, unknown>[] = []
    if (after !== null) clauses.push(this.keyset(order, after, true, false))
    if (upto !== null) clauses.push(this.keyset(order, upto, false, true))
    const body: Record<string, unknown> = {
      query: { bool: { filter: clauses } },
      sort: order.map((column) => ({ [column]: { order: 'asc' } })),
      size,
      track_total_hits: false,
    }
    if (fields !== undefined) body['_source'] = [...fields]
    const [status, response] = await this.request('POST', this.path(table, '_search') + PARTIAL_REFUSED, body)
    const hits = (response as { hits?: { hits?: { _source?: Record<string, unknown> }[] } } | null)?.hits?.hits
    if (status !== 200 || hits === undefined) throw new EngineError(`key range select from ${table} failed: ${errorOf(status, response)}`)
    whole(response, `a key range of ${table}`)
    return hits.map((hit) => hit._source ?? {})
  }

  /**
   * Rows in key order, strictly after one key and up to another inclusive, with the write generation
   * where the index has one - what `SELECT *` returns in the SQL dialects. In searches of at most
   * `KEY_PAGE` documents, each starting after the last one read.
   */
  async keyRange(
    table: string,
    order: readonly string[],
    options: { readonly after?: readonly unknown[]; readonly upto?: readonly unknown[]; readonly limit?: number } = {},
  ): Promise<Row[]> {
    return this.usage.operation(async () => {
      const [index, columns] = await this.ordered(table, order)
      let position = options.after === undefined ? null : this.storedKey(index, columns, options.after, 'after')
      const ceiling = options.upto === undefined ? null : this.storedKey(index, columns, options.upto, 'upto')
      const limit = options.limit
      if (limit !== undefined && limit < 1) return []
      const rows: Row[] = []
      while (limit === undefined || rows.length < limit) {
        const size = limit === undefined ? KEY_PAGE : Math.min(KEY_PAGE, limit - rows.length)
        const sources = await this.keyPage(table, columns, position, ceiling, size)
        for (const source of sources) {
          const row = rowOf(index, source)
          if (index.fenced) {
            const epoch = source[EPOCH_COLUMN]
            row[EPOCH_COLUMN] = epoch === null || epoch === undefined ? null : BigInt(epoch as number | bigint)
          }
          rows.push(row)
        }
        if (sources.length < size) break
        const last = sources[sources.length - 1] as Record<string, unknown>
        position = columns.map((column) => last[column] as StoredValue)
      }
      return rows
    })
  }

  /** The key of the `position`-th row in key order, one-based, or null: counted through searches. */
  async nthKey(table: string, order: readonly string[], options: { readonly position: number }): Promise<unknown[] | null> {
    return this.usage.operation(async () => {
      const [index, columns] = await this.ordered(table, order)
      if (options.position < 1) throw new EngineError(`position is one-based; ${options.position} is not a row`)
      let seen = 0
      let after: StoredValue[] | null = null
      for (;;) {
        const size = Math.min(KEY_PAGE, options.position - seen)
        const sources = await this.keyPage(table, columns, after, null, size, columns)
        if (sources.length < size) return null
        seen += size
        const last = sources[sources.length - 1] as Record<string, unknown>
        if (seen === options.position) return columns.map((column) => readValue(index.columns[column] as string, last[column]))
        after = columns.map((column) => last[column] as StoredValue)
      }
    })
  }

  /** Every document of the index, exactly. */
  async count(table: string): Promise<number> {
    return this.usage.operation(async () => {
      await this.index(table)
      const [status, response] = await this.request('POST', this.path(table, '_count'), { query: { match_all: {} } })
      const count = (response as { count?: number | bigint } | null)?.count
      if (status !== 200 || count === undefined) throw new EngineError(`count on ${table} failed: ${errorOf(status, response)}`)
      whole(response, `the count of ${table}`)
      return Number(count)
    })
  }

  /**
   * Copy rows in, leaving any document whose key is already there: one `_bulk` of `create` actions,
   * visible to a search on return. A document already there is refused with 409 and kept.
   */
  async copyIn(table: string, rows: readonly Row[]): Promise<void> {
    return this.usage.operation(async () => {
      if (rows.length === 0) return
      const columns = Object.keys(rows[0] as Row).sort()
      for (const row of rows) {
        const here = Object.keys(row).sort()
        if (here.join(' ') !== columns.join(' ')) {
          throw new EngineError(
            `copyIn into ${table} was given rows with different columns ([${columns.join(', ')}] ` +
              `and [${here.join(', ')}]). A chunk comes from one table, so this is a caller ` +
              'assembling it from two.',
          )
        }
      }
      const index = await this.index(table)
      const lines: string[] = []
      for (const row of rows) {
        const { id, source } = documentOf(index, row)
        lines.push(JSON.stringify({ create: { _index: table, _id: id } }), stringifyExactJson(source))
      }
      const [status, body] = await this.request('POST', '/_bulk?refresh=true', undefined, lines.join('\n') + '\n')
      const result = body as { items?: { create?: BulkItem }[] } | null
      if (status !== 200 || result === null) {
        throw new EngineError(`copying ${rows.length} rows into ${table} failed: ${writeErrorOf(status, body)}`)
      }
      const items = (result.items ?? []).map((item) => item.create ?? {})
      const present = items.filter(alreadyThere)
      const failed = items.filter((item) => (item.status ?? 500) >= 300 && !alreadyThere(item))
      if (items.length !== rows.length || failed.length > 0) {
        throw new EngineError(
          `copying ${rows.length} rows into ${table}: ${failed.length} were refused ` +
            `(${failed[0]?.error?.type ?? 'unknown'}), ${present.length} were there already and ` +
            `${items.length - failed.length - present.length} were written`,
        )
      }
    })
  }

  /** How many rows of this entity have been copied into this engine: the largest marker, or zero. */
  async backfillMarker(options: { readonly materialization: string; readonly entity: string }): Promise<number> {
    return this.usage.operation(async () => {
      await this.bookkeeping(BACKFILL, BACKFILL_MAPPING)
      const body = {
        size: 1,
        query: { bool: { filter: [{ term: { materialization: options.materialization } }, { term: { entity: options.entity } }] } },
        sort: [{ rows_copied: { order: 'desc' } }],
        _source: ['rows_copied'],
        track_total_hits: false,
      }
      const [status, response] = await this.request('POST', this.path(BACKFILL, '_search') + PARTIAL_REFUSED, body)
      const hits = (response as { hits?: { hits?: { _source?: { rows_copied?: number | bigint } }[] } } | null)?.hits?.hits
      if (status !== 200 || hits === undefined) throw new EngineError(`reading ${BACKFILL} failed: ${errorOf(status, response)}`)
      whole(response, `the marker in ${BACKFILL}`)
      const rows = hits[0]?._source?.rows_copied
      return rows === undefined ? 0 : Number(rows)
    })
  }

  /** Append the new marker, visible on return. Never update, so a trail stays readable. */
  async recordBackfillMarker(options: {
    readonly materialization: string
    readonly entity: string
    readonly rows: number
  }): Promise<void> {
    return this.usage.operation(async () => {
      await this.bookkeeping(BACKFILL, BACKFILL_MAPPING)
      const source = { materialization: options.materialization, entity: options.entity, rows_copied: options.rows }
      const [status, body] = await this.request('POST', this.path(BACKFILL, '_doc') + '?refresh=true', source)
      if (status !== 201) throw new EngineError(`recording backfill progress in ${BACKFILL} failed: ${errorOf(status, body)}`)
    })
  }

  // --- bookkeeping ---------------------------------------------------------------------------
  //
  // Two indexes of our own, append-only, read as a maximum: the map watermark and the backfill
  // markers. Each is read before anything creates it, as the SQL adapters look in their catalogue
  // before CREATE: a login that only reads and writes it needs no create_index, and an index of
  // that name with another mapping is refused rather than trusted.

  private async bookkeeping(name: string, mapping: Readonly<Record<string, unknown>>): Promise<void> {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const [status, body] = await this.request('GET', this.path(name, '_mapping'))
      if (status === 200) {
        const found = (body as Record<string, { mappings?: unknown }> | null)?.[name]?.mappings
        if (!sameJson(found, mapping)) {
          throw new EngineError(
            `${name} exists with a mapping this library would not create; it keeps this library's ` +
              'bookkeeping, so it is refused rather than read',
          )
        }
        return
      }
      if (status !== 404) throw new EngineError(`reading ${name} failed: ${errorOf(status, body)}`)
      const [created, answer] = await this.request('PUT', this.path(name), { mappings: mapping })
      if (created === 200) return
      // Created by somebody else between the two requests: read what they created.
      if (!alreadyExists(created, answer)) throw new EngineError(`creating ${name} failed: ${errorOf(created, answer)}`)
    }
    throw new EngineError(`${name} could not be read or created`)
  }

  private async watermarkIndex(): Promise<void> {
    await this.bookkeeping(WATERMARK, WATERMARK_MAPPING)
  }

  async mapWatermark(): Promise<number | null> {
    return this.usage.operation(async () => {
      await this.watermarkIndex()
      const body = { size: 1, sort: [{ map_version: { order: 'desc' } }], _source: ['map_version'] }
      const [status, response] = await this.request('POST', this.path(WATERMARK, '_search') + PARTIAL_REFUSED, body)
      const hits = (response as { hits?: { hits?: { _source?: { map_version?: number } }[] } } | null)?.hits?.hits
      if (status !== 200 || hits === undefined) throw new EngineError(`reading ${WATERMARK} failed: ${errorOf(status, response)}`)
      whole(response, `the watermark in ${WATERMARK}`)
      const version = hits[0]?._source?.map_version
      return version === undefined ? null : Number(version)
    })
  }

  async recordMapVersion(version: number, options: { readonly modelVersion: string }): Promise<void> {
    return this.usage.operation(async () => {
      await this.watermarkIndex()
      const source = { map_version: version, model_version: options.modelVersion }
      const [status, body] = await this.request('PUT', this.path(WATERMARK, '_doc', String(version)) + '?refresh=wait_for', source)
      if (status !== 200 && status !== 201) throw new EngineError(`recording map version failed: ${errorOf(status, body)}`)
    })
  }

  // --- transactions ----------------------------------------------------------------------------

  /** Refuses. Elasticsearch writes one document at a time, and so does this adapter. */
  async transaction<T>(_body: () => Promise<T>): Promise<T> {
    throw new EngineError(
      'Elasticsearch has no multi-document transactions, so this adapter will not pretend to ' +
        'start one. If these writes have to commit together, declare it: `atomicWith` on the ' +
        'entities makes them one colocation group, and the planner is then not permitted to put ' +
        'them here.',
    )
  }
}

function documentOf(index: Index, values: Readonly<Row>): { id: string; source: Record<string, unknown> } {
  try {
    const stored = index.fenced ? { ...index.columns, [EPOCH_COLUMN]: 'long' } : index.columns
    return document(stored, index.key, values)
  } catch (error) {
    throw new EngineError(`${index.name}: ${(error as Error).message}`)
  }
}

interface BulkItem {
  readonly status?: number
  readonly error?: { readonly type?: string }
}

/** A `create` refused because its document exists: what a copy leaves as it is. */
function alreadyThere(item: BulkItem): boolean {
  return item.status === 409 && item.error?.type === 'version_conflict_engine_exception'
}

/** `column` strictly above `value` (or below), the value as stored: a boolean too, false before true. */
function beyond(column: string, value: StoredValue, above: boolean): Record<string, unknown> {
  return { range: { [column]: { [above ? 'gt' : 'lt']: value } } }
}

function normalised(value: unknown): string {
  return typeof value === 'bigint' || typeof value === 'number' ? `n${BigInt(value as number | bigint)}` : `v${String(value)}`
}

function alreadyExists(status: number, body: unknown): boolean {
  const error = (body as { error?: { type?: string } } | null)?.error
  return status === 400 && error?.type === 'resource_already_exists_exception'
}

/**
 * Refuse an answer some shard did not contribute to: a page missing rows would move the cursor past
 * them for good, and a count would not be exact.
 */
export function whole(response: unknown, what: string): void {
  const body = response as { timed_out?: unknown; _shards?: { failed?: unknown } } | null
  const failed = Number(body?._shards?.failed ?? 0)
  if (body?.timed_out === true || failed !== 0) {
    throw new EngineError(
      `${what} came back partial - timed out or with ${failed} failed shard(s) - and a partial ` +
        'answer is not one this library returns',
    )
  }
}

/** Two JSON documents equal as values: key order aside, nothing the canonical encoding cannot hold. */
function sameJson(left: unknown, right: unknown): boolean {
  try {
    return canonicalString(left) === canonicalString(right)
  } catch {
    // A float or a value of no canonical form: nothing this library renders holds one.
    return false
  }
}

/** A refused write, by its type only: the engine's reason can quote the document's value. */
export function writeErrorOf(status: number, body: unknown): string {
  const error = (body as { error?: { type?: string } } | null)?.error
  return error === undefined ? String(status) : `${status} ${error.type ?? ''}`.trim()
}

function errorOf(status: number, body: unknown): string {
  const error = (body as { error?: { type?: string; reason?: string } } | null)?.error
  return error === undefined ? String(status) : `${status} ${error.type ?? ''}: ${error.reason ?? ''}`.trim()
}

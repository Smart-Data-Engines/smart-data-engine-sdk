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
import { document, documentId, stored, type StoredValue } from '../elasticsearch-document.js'
import { EngineError } from '../errors.js'
import type { PhysicalLayout } from '../placement.js'
import { readRow, type ReadFilter, type ReadPlan } from '../query.js'
import { elasticsearchMapping, elasticsearchProperty, indexNameRefusal, schemaStatements, TOKENS_FIELD } from '../schema.js'
import type { Row } from '../session.js'
import { Timestamp } from '../timestamp.js'
import { verifiedTlsOptions } from './_clickhouse-connection.js'
import { parseExactJson, stringifyExactJson } from './_json.js'

/** The most this adapter reads of one response. A page is at most 1001 documents. */
export const MAX_RESPONSE_BYTES = 256 * 1024 * 1024
const OPTIONS = new Set(['api_key', 'ca_cert', 'connect_timeout', 'send_receive_timeout', 'verify'])
const IDLE_MS = 20_000
const WATERMARK = 'sde_map_state'

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
  const user = url.username === '' ? null : decodeURIComponent(url.username)
  const password = decodeURIComponent(url.password)
  if (user !== null && options.has('api_key')) refuse('an Elasticsearch DSN gives a user or an api_key, not both')
  let authorization: string | null = null
  if (user !== null) {
    if (user.includes(':')) refuse('an Elasticsearch user has no colon')
    authorization = 'Basic ' + Buffer.from(`${user}:${password}`, 'utf8').toString('base64')
  } else if (options.has('api_key')) {
    const key = options.get('api_key') as string
    if (!/^[A-Za-z0-9+/]+={0,2}$/.test(key)) refuse('api_key is the encoded key Elasticsearch returns')
    authorization = `ApiKey ${key}`
  }
  const port = url.port === '' ? 9200 : Number(url.port)
  return Object.freeze({
    host, port, tls, authorization, caCert,
    connectTimeoutMs: seconds('connect_timeout', options.get('connect_timeout'), 10),
    sendReceiveTimeoutMs: seconds('send_receive_timeout', options.get('send_receive_timeout'), 30),
  })
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
  readonly columns: Readonly<Record<string, string>>
  readonly key: readonly string[]
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
      if (!sameColumns || JSON.stringify(found.key) !== JSON.stringify(expected._meta.sde.key)) {
        throw new EngineError(
          `index ${index} exists with fields ${JSON.stringify(found.columns)} and key ` +
            `${JSON.stringify(found.key)}, and the layout needs ${JSON.stringify(wanted)} and key ` +
            `${JSON.stringify(expected._meta.sde.key)}. Elasticsearch changes no field in place.`,
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
    const meta = (mapping['_meta'] as { sde?: { key?: unknown } } | undefined)?.sde
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
      if (column !== TOKENS_FIELD) columns[column] = layoutType(column, field)
    }
    if (key.some((column) => typeof column !== 'string' || columns[column] === undefined)) {
      throw new EngineError(`index ${name} names a key field it does not map`)
    }
    const index = { name, columns, key: key as string[] }
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
      if (status !== 200 && status !== 201) throw new EngineError(`insert into ${table} failed: ${errorOf(status, body)}`)
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
      if (status !== 200 || result === null) throw new EngineError(`batch insert into ${table} failed: ${errorOf(status, body)}`)
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
    if (predicate.operation === 'eq') {
      if (predicate.value === null) return { bool: { must_not: [{ exists: { field: name } }] } }
      return { term: { [name]: stored(index.columns[name] as string, predicate.value) } }
    }
    const bound = predicate.operation === 'ge' ? 'gte' : 'lt'
    return { range: { [name]: { [bound]: stored(index.columns[name] as string, predicate.value) } } }
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
    const [status, response] = await this.request('POST', this.path(table, '_search'), body)
    const hits = (response as { hits?: { hits?: { _source?: Record<string, unknown> }[] } } | null)?.hits?.hits
    if (status !== 200 || hits === undefined) throw new EngineError(`logical scan of ${table} failed: ${errorOf(status, response)}`)
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
      return BigInt(count)
    })
  }

  // --- rollback protection -------------------------------------------------------------------

  private async watermarkIndex(): Promise<void> {
    const mappings = {
      dynamic: 'strict',
      properties: { map_version: { type: 'long' }, model_version: { type: 'keyword' } },
    }
    const [status, body] = await this.request('PUT', this.path(WATERMARK), { mappings })
    if (status !== 200 && !alreadyExists(status, body)) throw new EngineError(`creating ${WATERMARK} failed: ${errorOf(status, body)}`)
  }

  async mapWatermark(): Promise<number | null> {
    return this.usage.operation(async () => {
      await this.watermarkIndex()
      const body = { size: 1, sort: [{ map_version: { order: 'desc' } }], _source: ['map_version'] }
      const [status, response] = await this.request('POST', this.path(WATERMARK, '_search'), body)
      const hits = (response as { hits?: { hits?: { _source?: { map_version?: number } }[] } } | null)?.hits?.hits
      if (status !== 200 || hits === undefined) throw new EngineError(`reading ${WATERMARK} failed: ${errorOf(status, response)}`)
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
    return document(index.columns, index.key, values)
  } catch (error) {
    throw new EngineError(`${index.name}: ${(error as Error).message}`)
  }
}

function normalised(value: unknown): string {
  return typeof value === 'bigint' || typeof value === 'number' ? `n${BigInt(value as number | bigint)}` : `v${String(value)}`
}

function alreadyExists(status: number, body: unknown): boolean {
  const error = (body as { error?: { type?: string } } | null)?.error
  return status === 400 && error?.type === 'resource_already_exists_exception'
}

function errorOf(status: number, body: unknown): string {
  const error = (body as { error?: { type?: string; reason?: string } } | null)?.error
  return error === undefined ? String(status) : `${status} ${error.type ?? ''}: ${error.reason ?? ''}`.trim()
}

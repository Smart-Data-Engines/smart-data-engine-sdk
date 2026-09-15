/** Strict, portable connection intent; no driver-specific DSN fallback. */
import { X509Certificate } from 'node:crypto'
import { closeSync, constants, fstatSync, openSync, readSync } from 'node:fs'
import { isIP } from 'node:net'
import { isAbsolute } from 'node:path'
import { checkServerIdentity, createSecureContext, type ConnectionOptions } from 'node:tls'

import { EngineError } from '../errors.js'

export interface ConnectionParameters {
  readonly host: string
  readonly port: number
  readonly username: string
  readonly password: string
  readonly database: string
  readonly secure: boolean
  readonly ca_cert: string | null
  readonly connect_timeout: number
  readonly send_receive_timeout: number
  readonly receive_timeout_supplied: boolean
  readonly protocol: 'http:' | 'https:'
}

const PARAMETERS = new Set(['secure', 'verify', 'ca_cert', 'connect_timeout', 'send_receive_timeout'])
function refuse(field: string): never {
  throw new EngineError(`Invalid ClickHouse connection ${field}; no connection was opened.`)
}
function decode(value: string, field: string, form = false): string {
  try {
    const result = decodeURIComponent(form ? value.replace(/\+/g, ' ') : value)
    if (/[\u0000-\u001f\u007f]/u.test(result)) return refuse(field)
    return result
  } catch { return refuse(field) }
}
function seconds(value: string | undefined, fallback: number): number {
  if (value === undefined) return fallback
  if (!/^(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$/.test(value)) return refuse('timeout')
  const parsed = Number(value)
  if (!Number.isFinite(parsed) || parsed < 0.001 || parsed > 2147483.647) return refuse('timeout')
  return parsed
}

export function parseDsn(dsn: string): ConnectionParameters {
  if (typeof dsn !== 'string' || /[\u0000-\u0020\u007f\\#\p{White_Space}]/u.test(dsn)) return refuse('URI')
  try { encodeURIComponent(dsn) } catch { return refuse('URI') }
  const matched = /^([A-Za-z][A-Za-z0-9+.-]*):\/\/([^/?#]*)(\/[^?#]*)?(?:\?([^#]*))?$/.exec(dsn)
  if (matched === null) return refuse('URI')
  const scheme = matched[1]!.toLowerCase()
  if (!['http', 'https', 'clickhouse', 'clickhouses'].includes(scheme)) return refuse('scheme')
  const authority = matched[2]!
  const pieces = authority.split('@')
  if (pieces.length > 2) return refuse('authority')
  const hostPort = pieces[pieces.length - 1]!
  let username = 'default', password = ''
  if (pieces.length === 2) {
    const userinfo = pieces[0]!, separator = userinfo.indexOf(':')
    username = decode(separator < 0 ? userinfo : userinfo.slice(0, separator), 'username')
    password = separator < 0 ? '' : decode(userinfo.slice(separator + 1), 'password')
    if (username === '' || username.includes(':')) return refuse('username')
  }
  let host: string, rawPort: string | undefined
  if (hostPort.startsWith('[')) {
    const close = hostPort.indexOf(']')
    if (close < 0) return refuse('host')
    const rawHost = hostPort.slice(1, close), remainder = hostPort.slice(close + 1)
    if (isIP(rawHost) !== 6 || (remainder !== '' && !remainder.startsWith(':'))) return refuse('host')
    host = new URL(`http://[${rawHost}]/`).hostname.slice(1, -1).toLowerCase()
    rawPort = remainder === '' ? undefined : remainder.slice(1)
  } else {
    const pair = hostPort.split(':')
    if (pair.length > 2) return refuse('host')
    host = pair[0]!.toLowerCase()
    rawPort = pair[1]
    const labels = (host.endsWith('.') ? host.slice(0, -1) : host).split('.')
    if (host.length > 253 || labels.some((label) => !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))) return refuse('host')
    if (isIP(host) !== 4 && /^(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*\.?$/.test(host)) return refuse('host')
  }
  const path = matched[3]
  if (path === undefined || !/^\/[^/]+$/.test(path)) return refuse('database')
  const database = decode(path.slice(1), 'database')
  if (database === '' || database.includes('/') || database === '.' || database === '..') return refuse('database')
  const options = new Map<string, string>()
  const query = matched[4]
  if (query !== undefined && query !== '') for (const entry of query.split('&')) {
    const separator = entry.indexOf('=')
    if (separator <= 0) return refuse('query option')
    const key = decode(entry.slice(0, separator), 'query option', true)
    const value = decode(entry.slice(separator + 1), 'query option', true)
    if (!PARAMETERS.has(key) || options.has(key)) return refuse('query option')
    options.set(key, value)
  }
  let secure = scheme === 'https' || scheme === 'clickhouses'
  const requested = options.get('secure')
  if (requested !== undefined) {
    if (requested !== 'true' && requested !== 'false') return refuse('secure option')
    const wanted = requested === 'true'
    if ((scheme === 'http' && wanted) || (secure && !wanted)) return refuse('conflicting transport')
    secure = wanted
  }
  let port: number
  if (rawPort !== undefined) {
    if (!/^[0-9]+$/.test(rawPort)) return refuse('port')
    port = Number(rawPort)
    if (!Number.isInteger(port) || port < 1 || port > 65535) return refuse('port')
  } else port = scheme === 'http' ? 80 : scheme === 'https' ? 443 : secure ? 8443 : 8123
  if (scheme === 'clickhouse' && requested === undefined && (port === 443 || port === 8443)) return refuse('ambiguous TLS port; use an explicit secure scheme or option')
  if (options.has('verify') && (options.get('verify') !== 'true' || !secure)) return refuse('verify option')
  const ca = options.get('ca_cert') ?? null
  if (ca !== null && (!secure || !isAbsolute(ca))) return refuse('CA option')
  return Object.freeze({ host, port, username, password, database, secure, ca_cert: ca,
    connect_timeout: seconds(options.get('connect_timeout'), 10),
    send_receive_timeout: seconds(options.get('send_receive_timeout'), 15),
    receive_timeout_supplied: options.has('send_receive_timeout'),
    protocol: secure ? 'https:' : 'http:',
  })
}

/** Read the selected CA once for this connection; never change process-global trust. */
export function verifiedTls(target: ConnectionParameters): ConnectionOptions {
  let ca: Buffer | undefined
  if (target.ca_cert !== null) {
    try {
      const limit = 1024 * 1024
      const fd = openSync(target.ca_cert, constants.O_RDONLY | constants.O_NONBLOCK)
      try {
        const info = fstatSync(fd)
        if (!info.isFile() || info.size === 0 || info.size > limit) return refuse('CA file')
        const buffer = Buffer.alloc(limit + 1)
        let size = 0
        while (size < buffer.length) {
          const received = readSync(fd, buffer, size, buffer.length - size, null)
          if (received === 0) break
          size += received
        }
        if (size === 0 || size > limit) return refuse('CA file')
        ca = buffer.subarray(0, size)
      } finally { closeSync(fd) }
      const text = ca.toString('utf8')
      const blocks = text.match(/-----BEGIN CERTIFICATE-----[\s\S]*?-----END CERTIFICATE-----/g)
      if (blocks === null || blocks.length === 0) return refuse('CA file')
      for (const block of blocks) new X509Certificate(block)
      createSecureContext({ ca })
    } catch { return refuse('CA file') }
  }
  return {
    ...(ca === undefined ? {} : { ca }), rejectUnauthorized: true,
    ...(isIP(target.host) === 0 ? { servername: target.host } : {}),
    checkServerIdentity: (_host, certificate) => checkServerIdentity(target.host, certificate),
  }
}

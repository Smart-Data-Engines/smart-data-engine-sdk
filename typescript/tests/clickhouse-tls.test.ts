/** Connection intent must be observed on the wire before claiming TLS. */
import { createServer as createTcpServer, type Socket } from 'node:net'
import { createServer as createHttpServer } from 'node:http'
import type { AddressInfo } from 'node:net'
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { resolve, join } from 'node:path'
import { createServer as createHttpsServer, type Server as HttpsServer } from 'node:https'
import { parseDsn } from '../src/engines/_clickhouse-connection.js'

import { ClickHouseEngine } from '../src/engines/clickhouse.js'

async function firstBytes(scheme: string, suffix: string): Promise<Buffer> {
  const sockets: Socket[] = []
  let finish: (bytes: Buffer) => void = () => {}
  const received = new Promise<Buffer>((resolve) => { finish = resolve })
  const server = createTcpServer((socket) => {
    sockets.push(socket)
    socket.once('data', (bytes) => { finish(bytes.subarray(0, 5)); socket.destroy() })
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  const port = (server.address() as AddressInfo).port
  const engine = new ClickHouseEngine(`${scheme}://probe:synthetic@127.0.0.1:${port}/db${suffix}`)
  const opened = engine.connect().catch(() => {})
  let timeout: NodeJS.Timeout | undefined
  try {
    return await Promise.race([received, new Promise<never>((_, reject) => {
      timeout = setTimeout(() => reject(new Error('no transport bytes from the client')), 2000)
    })])
  } finally {
    if (timeout !== undefined) clearTimeout(timeout)
    sockets.forEach((socket) => socket.destroy())
    await opened
    await engine.close()
    await new Promise<void>((resolve) => server.close(() => resolve()))
  }
}

describe('ClickHouse connection intent', () => {
  it('has a functioning explicit plain control', async () => {
    expect((await firstBytes('clickhouse', '')).toString('ascii')).toBe('POST ')
  })
  it('does not ignore secure=true on an arbitrary port', async () => {
    const observed = await firstBytes('clickhouse', '?secure=true')
    expect(observed[0]).toBe(0x16)
  })
  it.each(['false', '', 'tru'])('refuses verify=%s before opening a socket', (value) => {
    expect(() => new ClickHouseEngine(`https://probe:synthetic@tls.invalid/db?verify=${value}`)).toThrow()
  })
  it('refuses conflicting duplicate transport options', () => {
    expect(() => new ClickHouseEngine('clickhouse://probe:synthetic@tls.invalid/db?secure=false&secure=true')).toThrow()
  })
  it('refuses unsupported schemes instead of treating them as HTTP', () => {
    expect(() => new ClickHouseEngine('ftp://probe:synthetic@tls.invalid/db')).toThrow()
  })
  it('sends the decoded database exactly once', async () => {
    let observed: string | null = null
    const server = createHttpServer((request, response) => {
      observed = new URL(request.url!, 'http://test.invalid').searchParams.get('database')
      response.writeHead(200, { 'Content-Type': 'application/json' })
      response.end(JSON.stringify({ data: [{ version: '24.8.14.39' }] }))
    })
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
    const engine = new ClickHouseEngine(`clickhouse://probe:synthetic@127.0.0.1:${(server.address() as AddressInfo).port}/db%20space`)
    try { await engine.connect(); expect(observed).toBe('db space') }
    finally { await engine.close(); await new Promise<void>((resolve) => server.close(() => resolve())) }
  })
})

const fixture = JSON.parse(readFileSync(resolve('../testdata/clickhouse-dsns.json'), 'utf8')) as {
  protocol: number, cases: Array<{ id: string, dsn: string, expected?: Record<string, unknown>, reject?: boolean }>
}
describe('shared ClickHouse DSN intent', () => {
  it.each(fixture.cases)('$id', (entry) => {
    if (entry.reject) {
      expect(() => parseDsn(entry.dsn)).toThrow()
      try { parseDsn(entry.dsn) } catch (error) {
        expect(String(error)).not.toContain(entry.dsn)
        expect(String(error)).not.toContain('synthetic-secret')
      }
    } else expect(parseDsn(entry.dsn)).toMatchObject(entry.expected!)
  })
})

let material: Record<string, string>
let certRoot: string
const activeServers: HttpsServer[] = []
beforeAll(() => {
  certRoot = mkdtempSync(join(tmpdir(), 'sde-tls-'))
  const python = process.env.SDE_TEST_PYTHON ?? resolve('../python/.venv/bin/python')
  material = JSON.parse(execFileSync(python, [resolve('../python/tests/tls_certificates.py'), certRoot], {
    encoding: 'utf8', timeout: 30000,
  })) as Record<string, string>
})
afterEach(async () => {
  vi.unstubAllEnvs()
  while (activeServers.length) {
    const server = activeServers.pop()!
    server.closeAllConnections()
    await new Promise<void>((resolve) => server.close(() => resolve()))
  }
})
// The filesystem fixture is test-owned and never carries a production key.
afterAll(() => { if (certRoot) rmSync(certRoot, { recursive: true, force: true }) })

async function tlsEndpoint(certificate = 'server_cert', host = '127.0.0.1'): Promise<{ port: number, observed: { tcp: number, requests: number, authorization: string | undefined } }> {
  const observed = { tcp: 0, requests: 0, authorization: undefined as string | undefined }
  const server = createHttpsServer({ key: readFileSync(material.server_key!), cert: readFileSync(material[certificate]!) }, (request, response) => {
    observed.requests++
    observed.authorization = request.headers.authorization
    response.writeHead(200, { 'Content-Type': 'application/json', Connection: 'close' })
    response.end(JSON.stringify({ data: [{ version: '24.8.14.39' }] }))
  })
  server.on('connection', () => { observed.tcp++ })
  server.on('tlsClientError', () => {})
  await new Promise<void>((resolve) => server.listen(0, host, resolve))
  activeServers.push(server)
  return { port: (server.address() as AddressInfo).port, observed }
}

describe('verified ClickHouse TLS', () => {
  it('uses the selected CA and verifies an IP certificate on a nonstandard port', async () => {
    const endpoint = await tlsEndpoint()
    const engine = new ClickHouseEngine(`https://user:synthetic@127.0.0.1:${endpoint.port}/db?ca_cert=${encodeURIComponent(material.ca!)}`)
    try { await engine.connect(); expect(engine.version).toBe('24.8.14.39') }
    finally { await engine.close() }
    expect(endpoint.observed.requests).toBe(1)
    expect(endpoint.observed.authorization).toBe(`Basic ${Buffer.from('user:synthetic').toString('base64')}`)
  })
  it.each([
    ['server_cert', 'other_ca'], ['wrong_host_cert', 'ca'], ['expired_cert', 'ca'],
  ])('refuses certificate %s with trust %s before credentials reach HTTP', async (certificate, trust) => {
    const endpoint = await tlsEndpoint(certificate)
    const engine = new ClickHouseEngine(`https://user:synthetic@127.0.0.1:${endpoint.port}/db?ca_cert=${encodeURIComponent(material[trust]!)}`)
    try { await expect(engine.connect()).rejects.toThrow() }
    finally { await engine.close() }
    expect(endpoint.observed.tcp).toBeGreaterThan(0)
    expect(endpoint.observed.requests).toBe(0)
  })
  it('does not weaken explicit verification because of an insecure ambient setting', async () => {
    vi.stubEnv('NODE_TLS_REJECT_UNAUTHORIZED', '0')
    const endpoint = await tlsEndpoint('wrong_host_cert')
    const engine = new ClickHouseEngine(`https://user:synthetic@127.0.0.1:${endpoint.port}/db?ca_cert=${encodeURIComponent(material.ca!)}`)
    try { await expect(engine.connect()).rejects.toThrow() }
    finally { await engine.close() }
    expect(endpoint.observed.tcp).toBeGreaterThan(0)
    expect(endpoint.observed.requests).toBe(0)
  })
  it('keeps two client CA configurations separate', async () => {
    const endpoint = await tlsEndpoint()
    const good = new ClickHouseEngine(`https://user:synthetic@127.0.0.1:${endpoint.port}/db?ca_cert=${encodeURIComponent(material.ca!)}`)
    const bad = new ClickHouseEngine(`https://user:synthetic@127.0.0.1:${endpoint.port}/db?ca_cert=${encodeURIComponent(material.other_ca!)}`)
    try {
      await good.connect()
      await expect(bad.connect()).rejects.toThrow()
      await good.close(); await good.connect()
      expect(endpoint.observed.requests).toBe(2)
    } finally { await good.close(); await bad.close() }
  })
  it.each(['missing', 'empty', 'bad'])('refuses %s CA before opening a connection', async (kind) => {
    const endpoint = await tlsEndpoint()
    const file = join(certRoot, `ca-${kind}.pem`)
    if (kind !== 'missing') writeFileSync(file, kind === 'empty' ? '' : 'not a certificate')
    const engine = new ClickHouseEngine(`https://user:synthetic@127.0.0.1:${endpoint.port}/db?ca_cert=${encodeURIComponent(file)}`)
    try { await expect(engine.connect()).rejects.toThrow('CA file') }
    finally { await engine.close() }
    expect(endpoint.observed.tcp).toBe(0)
    expect(endpoint.observed.requests).toBe(0)
  })
})

describe('TLS transport continuity', () => {
  it('verifies a real IPv6 endpoint using its IP SAN', async () => {
    const endpoint = await tlsEndpoint('server_cert', '::1')
    const engine = new ClickHouseEngine(`https://user:synthetic@[::1]:${endpoint.port}/db?ca_cert=${encodeURIComponent(material.ca!)}`)
    try { await engine.connect(); expect(engine.version).toBe('24.8.14.39') }
    finally { await engine.close() }
    expect(endpoint.observed.requests).toBe(1)
  })
  it('does not follow a TLS response to a plaintext endpoint', async () => {
    let leaked = 0, redirected = 0
    const plain = createHttpServer((_request, response) => { leaked++; response.end('unexpected') })
    await new Promise<void>((resolve) => plain.listen(0, '127.0.0.1', resolve))
    const server = createHttpsServer({ key: readFileSync(material.server_key!), cert: readFileSync(material.server_cert!) }, (_request, response) => {
      redirected++
      response.writeHead(302, { Location: `http://127.0.0.1:${(plain.address() as AddressInfo).port}/` })
      response.end()
    })
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
    activeServers.push(server)
    const engine = new ClickHouseEngine(`https://user:synthetic@127.0.0.1:${(server.address() as AddressInfo).port}/db?ca_cert=${encodeURIComponent(material.ca!)}`)
    try { await expect(engine.connect()).rejects.toThrow(); expect(redirected).toBe(1); expect(leaked).toBe(0) }
    finally { await engine.close(); plain.closeAllConnections(); await new Promise<void>((resolve) => plain.close(() => resolve())) }
  })
  it('does not replay the first accepted handshake request after losing its response', async () => {
    let calls = 0
    const server = createHttpsServer({ key: readFileSync(material.server_key!), cert: readFileSync(material.server_cert!) }, (request) => {
      calls++; request.socket.destroy()
    })
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
    activeServers.push(server)
    const engine = new ClickHouseEngine(`https://user:synthetic@127.0.0.1:${(server.address() as AddressInfo).port}/db?ca_cert=${encodeURIComponent(material.ca!)}`)
    try { await expect(engine.connect()).rejects.toThrow(); expect(calls).toBe(1) }
    finally { await engine.close() }
  })
})


describe('connection-local trust and credentials', () => {
  it('pins the selected CA across requests and reloads it only after close', async () => {
    const endpoint = await tlsEndpoint()
    const file = join(certRoot, 'mutable-ca.pem')
    writeFileSync(file, readFileSync(material.ca!))
    const engine = new ClickHouseEngine(`https://user:synthetic@127.0.0.1:${endpoint.port}/db?ca_cert=${encodeURIComponent(file)}`)
    try {
      await engine.connect()
      writeFileSync(file, readFileSync(material.other_ca!))
      await engine.get('test_table', { id: 'synthetic' })
      expect(endpoint.observed.requests).toBe(2)
      expect(endpoint.observed.tcp).toBe(2)
      await engine.close()
      await expect(engine.connect()).rejects.toThrow()
      expect(endpoint.observed.requests).toBe(2)
    } finally { await engine.close() }
  })
  it('transmits decoded UTF-8 Basic credentials without Latin-1 loss', async () => {
    const endpoint = await tlsEndpoint()
    const user = 'zażółć', password = 'gęślą:jaźń'
    const engine = new ClickHouseEngine(`https://${encodeURIComponent(user)}:${encodeURIComponent(password)}@127.0.0.1:${endpoint.port}/db?ca_cert=${encodeURIComponent(material.ca!)}`)
    try { await engine.connect() } finally { await engine.close() }
    expect(endpoint.observed.authorization).toBe(`Basic ${Buffer.from(`${user}:${password}`, 'utf8').toString('base64')}`)
  })
  it('refuses an oversized CA before opening a socket', async () => {
    const endpoint = await tlsEndpoint()
    const file = join(certRoot, 'large-ca.pem')
    writeFileSync(file, Buffer.alloc(1024 * 1024 + 1, 32))
    const engine = new ClickHouseEngine(`https://user:synthetic@127.0.0.1:${endpoint.port}/db?ca_cert=${encodeURIComponent(file)}`)
    try { await expect(engine.connect()).rejects.toThrow('CA file') } finally { await engine.close() }
    expect(endpoint.observed.tcp).toBe(0)
  })
})

/** Peer identity must match the configured PG host, not a TLS library's default hostname. */
import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { createServer, type AddressInfo, type Server, type Socket } from 'node:net'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { createSecureContext, TLSSocket } from 'node:tls'
import { afterAll, beforeAll, expect, it, vi } from 'vitest'

import { Client } from 'pg'

import { PostgresEngine } from '../src/engines/postgres.js'

let material: Record<string, string>, directory: string
beforeAll(() => {
  directory = mkdtempSync(join(tmpdir(), 'sde-pg-tls-'))
  const python = process.env.SDE_TEST_PYTHON ?? resolve('../python/.venv/bin/python')
  material = JSON.parse(execFileSync(python, [resolve('../python/tests/tls_certificates.py'), directory],
    { encoding: 'utf8', timeout: 30000 })) as Record<string, string>
})
afterAll(() => { if (directory) rmSync(directory, { recursive: true, force: true }) })

async function endpoint(certificate: string, bindHost = '127.0.0.1') {
  const context = createSecureContext({ key: readFileSync(material.server_key!), cert: readFileSync(material[certificate]!) })
  const sockets: Socket[] = []
  const observed = { tcp: 0, sslRequests: 0, startup: [] as Buffer[], tlsErrors: [] as string[] }
  const servers: Server[] = []
  const handleConnection = (raw: Socket): void => {
    sockets.push(raw); observed.tcp++
    raw.setTimeout(2000, () => raw.destroy())
    let initial = Buffer.alloc(0)
    const negotiate = (chunk: Buffer): void => {
      initial = Buffer.concat([initial, chunk])
      if (initial.length < 8) return
      if (initial.readUInt32BE(4) === 196608) {
        if (initial.length < initial.readUInt32BE(0)) return
        observed.startup.push(initial)
        raw.destroy()
        return
      }
      expect(initial.readUInt32BE(0)).toBe(8)
      expect(initial.readUInt32BE(4)).toBe(80877103)
      observed.sslRequests++
      raw.off('data', negotiate)
      raw.write('S')
      const secure = new TLSSocket(raw, { isServer: true, secureContext: context })
      sockets.push(secure)
      let message = Buffer.alloc(0)
      secure.on('data', (piece: Buffer) => {
        message = Buffer.concat([message, piece])
        if (message.length < 4 || message.length < message.readUInt32BE(0)) return
        expect(message.readUInt32BE(4)).toBe(196608)
        observed.startup.push(message)
        // Intentionally stop after StartupMessage; no authentication or query server is simulated.
        secure.destroy()
      })
      secure.on('error', (error) => { observed.tlsErrors.push(error.message); secure.destroy() })
    }
    raw.on('data', negotiate)
    raw.on('error', () => raw.destroy())
  }
  const close = async (): Promise<void> => {
    for (const socket of sockets) socket.destroy()
    await Promise.all(servers.map((server) => new Promise<void>((resolve) => {
      if (server.listening) server.close(() => resolve())
      else resolve()
    })))
  }
  // localhost resolution may prefer either family. Bind only the two loopback addresses,
  // on one port with the same handler; a wildcard listener would expose the test endpoint.
  const addresses = bindHost === 'localhost' ? ['127.0.0.1', '::1'] : [bindHost]
  let port = 0
  try {
    for (const address of addresses) {
      const server = createServer(handleConnection)
      servers.push(server)
      await new Promise<void>((resolve, reject) => {
        server.once('error', reject)
        server.listen({ port, host: address, ...(address === '::1' ? { ipv6Only: true } : {}) }, () => {
          server.off('error', reject)
          resolve()
        })
      })
      port = (server.address() as AddressInfo).port
    }
  } catch (error) {
    await close()
    throw error
  }
  return { port, observed, close }
}

it.each([
  ['127.0.0.1', 'dns_only_cert', false],
  ['127.0.0.1', 'ip_only_cert', true],
  ['localhost', 'dns_only_cert', true],
  ['localhost', 'ip_only_cert', false],
  ['[::1]', 'dns_only_cert', false],
  ['[::1]', 'ip_only_cert', true],
] as const)('verify-full host %s with %s permits StartupMessage=%s', async (host, certificate, accepted) => {
  const server = await endpoint(certificate, host === '[::1]' ? '::1' : host)
  const dsn = `postgresql://tls_probe:synthetic@${host}:${server.port}/tls_probe?sslmode=verify-full&sslrootcert=${encodeURIComponent(material.ca!)}&connect_timeout=2`
  const engine = new PostgresEngine(dsn)
  try {
    await expect(engine.connect()).rejects.toThrow()
  } finally {
    await engine.close()
    await server.close()
  }
  expect(server.observed.tcp).toBeGreaterThan(0)
  expect(server.observed.sslRequests).toBe(1)
  expect(server.observed.startup.length).toBe(accepted ? 1 : 0)
  if (accepted) expect(server.observed.startup[0]!.includes(Buffer.from('user\0tls_probe\0'))).toBe(true)
})


it.each(['no-verify', 'disable'] as const)('preserves native legacy sslmode=%s', async (mode) => {
  const server = await endpoint('dns_only_cert')
  const engine = new PostgresEngine(`postgresql://tls_probe:synthetic@127.0.0.1:${server.port}/tls_probe?sslmode=${mode}&sslrootcert=${encodeURIComponent(material.ca!)}&connect_timeout=2`)
  try { await expect(engine.connect()).rejects.toThrow() }
  finally { await engine.close(); await server.close() }
  expect(server.observed.tcp).toBe(1)
  expect(server.observed.startup.length).toBe(1)
  expect(server.observed.sslRequests).toBe(mode === 'disable' ? 0 : 1)
})

it('binds boolean SSL true in the actual connection object without changing TLS policy', async () => {
  let captured: Client | undefined
  let original: unknown
  class InspectClient extends Client {
    constructor(config: Record<string, unknown>) {
      super(config)
      captured = this
      original = (this as unknown as { connection: { ssl: unknown } }).connection.ssl
    }
    override async connect(): Promise<Client> { throw new Error('stop before native I/O') }
  }
  const engine = new PostgresEngine('postgresql://tls_probe:synthetic@127.0.0.1:1/tls_probe?ssl=true',
    { driver: { Client: InspectClient } })
  try { await expect(engine.connect()).rejects.toThrow('stop before native I/O') }
  finally { await engine.close() }
  expect(original).toBe(true)
  const native = captured as unknown as {
    ssl: Record<string, unknown>, connection: { ssl: Record<string, unknown> },
    connectionParameters: { ssl: Record<string, unknown> },
  }
  expect(native.connection.ssl).toEqual({ host: '127.0.0.1', rejectUnauthorized: true,
    checkServerIdentity: expect.any(Function) })
  expect(native.ssl).toBe(native.connection.ssl)
  expect(native.connectionParameters.ssl).toBe(native.connection.ssl)
})


it('does not mutate SSL options shared by two native clients and preserves private-key descriptors', async () => {
  const checkServerIdentity = (): undefined => undefined
  const shared = { ca: Buffer.from('synthetic-ca'), checkServerIdentity, rejectUnauthorized: true }
  Object.defineProperty(shared, 'key', { value: Buffer.from('synthetic-key'),
    enumerable: false, writable: false, configurable: false })
  const before = Object.getOwnPropertyDescriptors(shared)
  const clients: Client[] = []
  class InspectClient extends Client {
    constructor(config: Record<string, unknown>) {
      // The same native options object may come from pg.defaults.ssl or a caller's config.
      super({ ...config, ssl: shared })
      clients.push(this)
    }
    override async connect(): Promise<Client> { throw new Error('stop before native I/O') }
  }
  for (const host of ['127.0.0.1', '127.0.0.2']) {
    const engine = new PostgresEngine(`postgresql://tls_probe:synthetic@${host}:1/tls_probe`,
      { driver: { Client: InspectClient } })
    try { await expect(engine.connect()).rejects.toThrow('stop before native I/O') }
    finally { await engine.close() }
  }
  expect(Object.getOwnPropertyDescriptors(shared)).toEqual(before)
  const options = clients.map((client) => (client as unknown as {
    connection: { ssl: Record<string, unknown> },
  }).connection.ssl)
  expect(options[0]).not.toBe(shared)
  expect(options[1]).not.toBe(shared)
  expect(options[0]).not.toBe(options[1])
  expect(options.map((value) => value.host)).toEqual(['127.0.0.1', '127.0.0.2'])
  for (const value of options) {
    expect(Object.getOwnPropertyDescriptor(value, 'key')).toEqual(before.key)
    expect(value.ca).toBe(shared.ca)
    expect(value.checkServerIdentity).toBe(checkServerIdentity)
    expect(value.rejectUnauthorized).toBe(true)
  }
})


it.each([
  ['127.0.0.1', 'ip_only_cert', 'ca', true],
  ['127.0.0.1', 'ip_only_cert', 'other_ca', false],
  ['localhost', 'dns_only_cert', 'ca', true],
  ['localhost', 'dns_only_cert', 'other_ca', false],
  ['[::1]', 'ip_only_cert', 'ca', true],
  ['[::1]', 'ip_only_cert', 'other_ca', false],
] as const)(
  'explicit verify-full host %s with %s and %s ignores insecure ambient defaults', async (host, certificate, trust, accepted) => {
    vi.stubEnv('NODE_TLS_REJECT_UNAUTHORIZED', '0')
    const server = await endpoint(certificate, host === '[::1]' ? '::1' : host)
    const engine = new PostgresEngine(`postgresql://tls_probe:synthetic@${host}:${server.port}/tls_probe?sslmode=verify-full&sslrootcert=${encodeURIComponent(material[trust]!)}&connect_timeout=2`)
    try { await expect(engine.connect()).rejects.toThrow() }
    finally {
      await engine.close(); await server.close(); vi.unstubAllEnvs()
    }
    expect(server.observed.sslRequests).toBe(1)
    expect(server.observed.startup.length).toBe(accepted ? 1 : 0)
  })


it.each(['127.0.0.1', '[::1]'])('preserves an explicit peer checker on real TLS for %s', async (host) => {
  const server = await endpoint('ip_only_cert', host === '[::1]' ? '::1' : host)
  const checker = vi.fn(() => new Error('caller rejected peer'))
  class ConfiguredClient extends Client {
    constructor(config: Record<string, unknown>) {
      super({ ...config, ssl: { ca: readFileSync(material.ca!), checkServerIdentity: checker } })
    }
  }
  const engine = new PostgresEngine(`postgresql://tls_probe:synthetic@${host}:${server.port}/tls_probe?connect_timeout=2`,
    { driver: { Client: ConfiguredClient } })
  try { await expect(engine.connect()).rejects.toThrow('caller rejected peer') }
  finally { await engine.close(); await server.close() }
  expect(checker).toHaveBeenCalledTimes(1)
  expect(server.observed.sslRequests).toBe(1)
  expect(server.observed.startup).toHaveLength(0)
})

it('does not install an IP checker for explicit no-verify options', async () => {
  let selected: unknown
  class InspectClient extends Client {
    constructor(config: Record<string, unknown>) { super({ ...config, ssl: { rejectUnauthorized: false } }) }
    override async connect(): Promise<Client> {
      selected = (this as unknown as { connection: { ssl: unknown } }).connection.ssl
      throw new Error('stop before native I/O')
    }
  }
  const engine = new PostgresEngine('postgresql://tls_probe:synthetic@[::1]:1/tls_probe',
    { driver: { Client: InspectClient } })
  try { await expect(engine.connect()).rejects.toThrow('stop before native I/O') }
  finally { await engine.close() }
  expect(selected).toEqual({ host: '::1', rejectUnauthorized: false })
})

it('preserves explicit no-verify with a mismatched IPv6 certificate', async () => {
  const server = await endpoint('dns_only_cert', '::1')
  const engine = new PostgresEngine(`postgresql://tls_probe:synthetic@[::1]:${server.port}/tls_probe?sslmode=no-verify&connect_timeout=2`)
  try { await expect(engine.connect()).rejects.toThrow() }
  finally { await engine.close(); await server.close() }
  expect(server.observed.sslRequests).toBe(1)
  expect(server.observed.startup).toHaveLength(1)
})


it('serves the DNS TLS fixture on both loopback addresses on one port', async () => {
  const server = await endpoint('server_cert', 'localhost')
  try {
    for (const host of ['127.0.0.1', '[::1]']) {
      const engine = new PostgresEngine(`postgresql://tls_probe:synthetic@${host}:${server.port}/tls_probe?sslmode=verify-full&sslrootcert=${encodeURIComponent(material.ca!)}&connect_timeout=2`)
      try { await expect(engine.connect()).rejects.toThrow() }
      finally { await engine.close() }
    }
  } finally { await server.close() }
  expect(server.observed.tcp).toBe(2)
  expect(server.observed.sslRequests).toBe(2)
  expect(server.observed.startup).toHaveLength(2)
})

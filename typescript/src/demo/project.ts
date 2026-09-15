/** Runtime-only local configuration. This module never reads operator credentials. */
import { createHash, randomUUID } from 'node:crypto'
import { closeSync, existsSync, fsyncSync, lstatSync, mkdirSync, openSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { canonicalBytes, digest16 } from '../canonical.js'
import { checkMapProject } from '../generation.js'
import { loadMap } from '../placement.js'
import { neutralDeclaration } from '../model.js'
import { weatherModel } from './model.js'

export class DemoRefused extends Error {
  constructor(message: string) { super(message); this.name = 'DemoRefused' }
}
export function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new DemoRefused('Expected a local JSON object.')
  return value as Record<string, unknown>
}
export function read(path: string, privateFile = false) {
  const stat = lstatSync(path)
  if (!stat.isFile() || stat.size > 2 * 1024 * 1024 || (privateFile && (stat.mode & 0o777) !== 0o600)) {
    throw new DemoRefused('A required local file is invalid; credential files need mode 0600.')
  }
  const payload = readFileSync(path)
  if (payload.length > 2 * 1024 * 1024) throw new DemoRefused('The local input exceeds 2 MiB.')
  return { value: object(JSON.parse(payload.toString('utf8'))), payload }
}
function ensureDirectory(path: string): void {
  if (existsSync(path)) return
  const parent = dirname(path)
  ensureDirectory(parent)
  try { mkdirSync(path, { mode: 0o700 }) }
  catch (error) { if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error }
  const fd = openSync(parent, 'r')
  try { fsyncSync(fd) } finally { closeSync(fd) }
}
export function write(path: string, value: unknown) {
  const directory = dirname(path)
  ensureDirectory(directory)
  const temporary = join(directory, `.weather-${randomUUID()}.tmp`)
  const fd = openSync(temporary, 'wx', 0o600)
  try {
    writeFileSync(fd, JSON.stringify(value) + '\n')
    fsyncSync(fd)
  } finally { closeSync(fd) }
  try {
    renameSync(temporary, path)
    const parent = openSync(directory, 'r')
    try { fsyncSync(parent) } finally { closeSync(parent) }
  } finally { if (existsSync(temporary)) unlinkSync(temporary) }
}
export interface Binding { dialect: 'postgres' | 'clickhouse'; dsn: string }
export function project(root: string) {
  if (existsSync(join(root, 'reset-request.json'))) throw new DemoRefused('Reset was requested; set up a fresh directory.')
  const config = read(join(root, 'config.json')).value
  const marker = read(join(root, 'setup-complete.json')).value
  if (Object.keys(config).sort().join(',') !== 'engines,model,project_id,protocol,public_keys' || config.protocol !== 1 ||
      marker.protocol !== 1 || marker.config_digest !== digest16(config)) {
    throw new DemoRefused('Setup is incomplete or config changed; restore the enrolled configuration.')
  }
  const model = weatherModel()
  if (!canonicalBytes(object(config.model)).equals(canonicalBytes(neutralDeclaration(model)))) {
    throw new DemoRefused('The starter requires the Weather model.')
  }
  const publicKey: Record<string, Uint8Array> = Object.create(null)
  for (const [name, value] of Object.entries(object(config.public_keys))) {
    if (typeof value !== 'string' || !name) throw new DemoRefused('Invalid trusted public key.')
    const decoded = Buffer.from(value, 'base64')
    if (decoded.length !== 32 || decoded.toString('base64') !== value) throw new DemoRefused('Invalid Ed25519 key encoding.')
    publicKey[name] = decoded
  }
  if (!Object.keys(publicKey).length || typeof config.project_id !== 'string') throw new DemoRefused('Missing project or trusted keys.')
  const metadata = read(join(root, 'resources.json')).value
  const credentials = read(join(root, 'runtime-credentials.json'), true)
  if (metadata.status !== 'ready' || object(metadata.credential_hashes).runtime !==
      createHash('sha256').update(credentials.payload).digest('hex')) {
    throw new DemoRefused('Runtime credentials differ from the ready resource allocation.')
  }
  const bindings: Record<string, Binding> = {}
  const engines = object(config.engines)
  if (Object.keys(engines).sort().join(',') !== Object.keys(credentials.value).sort().join(',')) {
    throw new DemoRefused('Runtime credential bindings are incomplete.')
  }
  for (const [name, value] of Object.entries(engines)) {
    const entry = object(value), dsn = credentials.value[name]
    if (!/^[a-z][a-z0-9_-]{0,47}$/.test(name) || !['postgres', 'clickhouse'].includes(String(entry.dialect)) ||
        typeof dsn !== 'string' || !dsn) throw new DemoRefused('Invalid runtime engine binding.')
    bindings[name] = { dialect: entry.dialect as Binding['dialect'], dsn }
  }
  const projectId = config.project_id
  function activeMap() {
    if (existsSync(join(root, 'reset-request.json'))) throw new DemoRefused('Reset was requested; the workload has stopped.')
    const placement = loadMap(read(join(root, 'state', 'active-map.json')).value,
      { model, publicKey, requireSignature: true })
    checkMapProject(placement, projectId)
    return placement
  }
  activeMap()
  return { model, bindings, projectId, activeMap }
}

/** Synthetic application worker; load an installed SDK artifact and keep all values local. */
import assert from 'node:assert/strict'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const args = Object.fromEntries(Array.from({ length: (process.argv.length - 2) / 2 }, (_, index) =>
  [process.argv[2 + index * 2].replace(/^--/, ''), process.argv[3 + index * 2]]))
/** @type {typeof import('../../src/index.js')} */
const sdk = await import(pathToFileURL(join(args.sdk, 'dist/index.js')).href)
const { PostgresEngine } = await import(pathToFileURL(join(args.sdk, 'dist/engines/postgres.js')).href)
const { ClickHouseEngine } = await import(pathToFileURL(join(args.sdk, 'dist/engines/clickhouse.js')).href)
const config = JSON.parse(readFileSync(args.config, 'utf8'))
const model = sdk.assemble(config.model.entities.map(entity => ({
  name: entity.name, fields: entity.fields.map(field => ({ ...field, nullable: field.nullable === true })),
  key: entity.key, pii: entity.pii ?? [], residency: entity.residency ?? null,
})), (config.model.relations ?? []).map(relation => ({ name: relation.name, source: relation.from, target: relation.to })),
config.model.atomic ?? [], config.model.cost_ceiling ?? null)
const keys = Object.fromEntries(Object.entries(config.public_keys).map(([name, value]) => [name, Buffer.from(value, 'base64')]))
const worker = Number(args.worker), refreshPeriod = Number(args['refresh-period-ms'] ?? 500)
const base = sdk.Timestamp.from('2026-09-14T12:00:00.123456Z').epochMicroseconds
const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds))
const now = () => process.hrtime.bigint()
const recorder = new sdk.Recorder(model.version)
/** @type {Record<string, import('../../src/engines/postgres.js').PostgresEngine | import('../../src/engines/clickhouse.js').ClickHouseEngine>} */
const engines = {}
const errors = {}, samples = [], windows = [], failures = []
let acknowledged = 0

function reading(sequence) {
  const cents = 1525 + sequence % 1000
  return { station: `qualification-station-${worker}`,
    at: sdk.Timestamp.fromEpochMicroseconds(base + BigInt(sequence)),
    celsius: `${Math.floor(cents / 100)}.${String(cents % 100).padStart(2, '0')}`,
    humidity: BigInt(30 + sequence % 70),
    id: `${worker.toString(16).padStart(8, '0')}-0000-4000-8000-${sequence.toString(16).padStart(12, '0')}` }
}

function cents(value) {
  assert.equal(typeof value, 'string')
  assert.match(value, /^\d+(\.\d{1,2})?$/)
  const [whole, fraction = ''] = value.split('.')
  return BigInt(whole) * 100n + BigInt(fraction.padEnd(2, '0'))
}

function sameReading(actual, expected) {
  assert.ok(actual)
  assert.deepEqual(Object.keys(actual).sort(), Object.keys(expected).sort())
  assert.equal(actual.station, expected.station)
  assert.equal(actual.at.epochMicroseconds, expected.at.epochMicroseconds)
  assert.equal(actual.id, expected.id)
  assert.equal(actual.humidity, expected.humidity)
  assert.equal(cents(actual.celsius), cents(expected.celsius))
}

let activeFingerprint
async function openSession(current) {
  const placement = sdk.loadMap(JSON.parse(readFileSync(join(args['project-dir'], 'active-map.json'), 'utf8')),
    { model, publicKey: keys, requireSignature: true })
  if (current !== undefined && placement.fingerprint === activeFingerprint) return current
  const opened = await sdk.Session.open(model, placement, engines, { recorder, projectId: config.project_id })
  activeFingerprint = placement.fingerprint
  return opened
}
function roll() {
  const window = recorder.roll()
  if (window !== undefined) { windows.push(sdk.windowRecord(window, model)); recorder.acknowledge(1) }
}
function failed(operation, error) {
  if (!(error instanceof sdk.SdeError)) throw error
  const key = `${operation}_${error.name}`
  errors[key] = (errors[key] ?? 0) + 1
  failures.push({ op: operation, error: error.name, time_ns: String(now()) })
}

async function main() {
  for (const [name, binding] of Object.entries(config.engines)) {
    const Engine = binding.dialect === 'postgres' ? PostgresEngine : ClickHouseEngine
    engines[name] = new Engine(process.env[binding.runtime_dsn_envs[0]])
    await engines[name].connect()
  }
  let session = await openSession()
  console.log('READY')
  const waitingUntil = now() + 30_000_000_000n
  while (!existsSync(args.start)) {
    if (now() >= waitingUntil) throw new Error('qualification coordinator missed its start deadline')
    await pause(10)
  }
  const start = JSON.parse(readFileSync(args.start, 'utf8'))
  const origin = BigInt(start.monotonic_ns), duration = BigInt(start.duration_ms) * 1_000_000n
  const rate = BigInt(start.writes_per_second_per_worker)
  assert.ok(rate > 0n && rate <= 1_000_000_000n && duration > 0n && worker >= 1 && worker <= 100)
  const period = 1_000_000_000n / rate, deadline = origin + duration
  let refreshAt = origin, rollAt = origin, reconnect = false
  while (now() < deadline) {
    const due = origin + BigInt(acknowledged) * period, tick = now()
    if (tick < due) { await pause(Math.min(Number(due - tick) / 1e6, 20)); continue }
    if (tick >= rollAt) { roll(); rollAt = tick + 5_000_000_000n }
    try {
      if (reconnect) for (const engine of Object.values(engines)) { await engine.close(); await engine.connect() }
      if (reconnect || (refreshPeriod > 0 && tick >= refreshAt)) {
        session = await openSession(reconnect ? undefined : session); reconnect = false
        refreshAt = tick + BigInt(refreshPeriod) * 1_000_000n
      }
    } catch (error) { failed('refresh', error); reconnect = true; await pause(50); continue }
    const row = reading(acknowledged + 1)
    let began = now()
    try { await session.save('WeatherReading', row) }
    catch (error) {
      failed('save', error); reconnect = true
      samples.push({ op: 'save', ok: false, start_ns: String(began), duration_ns: Number(now() - began), scheduled_ns: String(due) })
      continue
    }
    samples.push({ op: 'save', ok: true, start_ns: String(began), duration_ns: Number(now() - began), scheduled_ns: String(due) })
    acknowledged++
    if (acknowledged % 5 === 0) {
      began = now()
      let observed
      try { observed = await session.get('WeatherReading', { station: row.station, at: row.at }) }
      catch (error) {
        failed('get', error); reconnect = true
        samples.push({ op: 'get', ok: false, start_ns: String(began), duration_ns: Number(now() - began) })
        continue
      }
      sameReading(observed, row)
      samples.push({ op: 'get', ok: true, start_ns: String(began), duration_ns: Number(now() - began) })
    }
  }
  roll()
  writeFileSync(args.output, JSON.stringify({ protocol: 1, language: 'typescript', worker,
    sdk_module: args.sdk, acknowledged, scheduled_writes: Number(duration / period),
    finished_ns: String(now()), errors, failures, max_rss_kib: process.resourceUsage().maxRSS, samples, windows }))
  console.log('DONE')
}
try { await main() }
catch (error) { console.error(JSON.stringify({ error: error.name })); process.exitCode = 1 }
finally { for (const engine of Object.values(engines)) await engine.close() }

/** Measure installed SDK calls, with generation and oracle work outside call timers. */
import assert from 'node:assert/strict'
import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
const args = Object.fromEntries(Array.from({ length: (process.argv.length - 2) / 2 }, (_, n) =>
  [process.argv[2 + n * 2].replace(/^--/, ''), process.argv[3 + n * 2]]))
/** @type {typeof import('../../src/index.js')} */
const sdk = await import(pathToFileURL(join(args.sdk, 'dist/index.js')).href)
const { PostgresEngine } = await import(pathToFileURL(join(args.sdk, 'dist/engines/postgres.js')).href)
const { ClickHouseEngine } = await import(pathToFileURL(join(args.sdk, 'dist/engines/clickhouse.js')).href)
const config = JSON.parse(readFileSync(args.config, 'utf8'))
const model = sdk.assemble(config.model.entities.map(entity => ({ ...entity,
  fields: entity.fields.map(field => ({ ...field, nullable: field.nullable === true })),
  pii: entity.pii ?? [], residency: entity.residency ?? null })), [], config.model.atomic ?? [], config.model.cost_ceiling ?? null)
const keys = Object.fromEntries(Object.entries(config.public_keys).map(([key, value]) => [key, Buffer.from(value, 'base64')]))
const placement = sdk.loadMap(JSON.parse(readFileSync(join(args['project-dir'], 'active-map.json'), 'utf8')),
  { model, publicKey: keys, requireSignature: true })
const bindingName = placement.groups.WeatherReading.source.engine, binding = config.engines[bindingName]
const Engine = binding.dialect === 'postgres' ? PostgresEngine : ClickHouseEngine
const engine = new Engine(process.env[binding.runtime_dsn_envs[0]])
const recorder = args.telemetry === 'on' ? new sdk.Recorder(model.version) : undefined
const rows = Number(args.rows), batch = Number(args['batch-size']), repetitions = Number(args.samples)
assert.ok(Number.isSafeInteger(rows) && rows > 0 && rows <= 1_000_000)
assert.ok(Number.isSafeInteger(batch) && batch > 0 && batch <= 1000)
assert.ok(Number.isSafeInteger(repetitions) && repetitions > 0 && repetitions <= 10000)
assert.ok(['write', 'read'].includes(args.mode) && ['save', 'save_many'].includes(args.method))
assert.ok(args.method !== 'save' || batch === 1)
const base = sdk.Timestamp.from('2026-09-14T12:00:00.123456Z').epochMicroseconds
function reading(worker, sequence) {
  const cents = 1525 + sequence % 1000
  return { station: `qualification-station-${worker}`,
    at: sdk.Timestamp.fromEpochMicroseconds(base + BigInt(sequence)),
    celsius: `${Math.floor(cents / 100)}.${String(cents % 100).padStart(2, '0')}`,
    humidity: BigInt(30 + sequence % 70),
    id: `${worker.toString(16).padStart(8, '0')}-0000-4000-8000-${sequence.toString(16).padStart(12, '0')}` }
}
const samples = []
/** @type {Record<string, any>} */
const report = { protocol: 1, language: 'typescript', mode: args.mode, method: args.method,
  batch_size: batch, rows, sdk_module: args.sdk, status: 'incomplete', acknowledged: 0,
  samples, telemetry_enabled: recorder !== undefined }
let session, began = process.hrtime.bigint()
try {
  await engine.connect()
  session = await sdk.Session.open(model, placement, { [bindingName]: engine }, { recorder, projectId: config.project_id })
  if (args.mode === 'write') {
    for (let offset = 0; offset < 2; offset++) {
      const values = Array.from({ length: batch }, (_, n) => reading(98, offset * batch + n + 1))
      if (args.method === 'save') await session.save('WeatherReading', values[0])
      else await session.saveMany('WeatherReading', values)
    }
    began = process.hrtime.bigint()
    for (let first = 1; first <= rows; first += batch) {
      const values = Array.from({ length: Math.min(batch, rows - first + 1) }, (_, n) => reading(1, first + n))
      const start = process.hrtime.bigint()
      if (args.method === 'save') await session.save('WeatherReading', values[0])
      else await session.saveMany('WeatherReading', values)
      const duration = Number(process.hrtime.bigint() - start)
      samples.push({ operation: args.method, duration_ns: duration, rows: values.length })
      report.acknowledged += values.length
    }
  } else {
    const where = { station: reading(99, 1).station }
    const cents = 1525n * BigInt(rows) + BigInt(Math.floor(rows / 1000)) * 499500n + BigInt(rows % 1000) * BigInt(rows % 1000 + 1) / 2n
    const total = `${cents / 100n}.${String(cents % 100n).padStart(2, '0')}`
    for (let n = 0; n < 20; n++) assert.deepEqual(await session.get('WeatherReading', { ...where, at: reading(99, 1).at }), reading(99, 1))
    began = process.hrtime.bigint()
    for (let n = 0; n < repetitions; n++) {
      const sequence = 1 + n * 104729 % rows, expected = reading(99, sequence)
      for (const operation of ['get', 'scan', 'count', 'summarize']) {
        const start = process.hrtime.bigint()
        let result
        if (operation === 'get') result = await session.get('WeatherReading', { ...where, at: expected.at })
        else if (operation === 'scan') result = await session.scan('WeatherReading', { where, bounds: { field: 'at', low: expected.at }, limit: 1000 })
        else if (operation === 'count') result = await session.count('WeatherReading', { where })
        else result = await session.summarize('WeatherReading', 'celsius', { where })
        const duration = Number(process.hrtime.bigint() - start)
        let returned = 1
        if (operation === 'get') assert.deepEqual(result, expected)
        else if (operation === 'scan') {
          assert.ok(result !== null && typeof result === 'object' && 'rows' in result)
          returned = Math.min(1000, rows - sequence + 1)
          assert.deepEqual(result.rows, Array.from({ length: returned }, (_, k) => reading(99, sequence + k)))
        } else if (operation === 'count') assert.equal(result, BigInt(rows))
        else {
          assert.ok(result !== null && typeof result === 'object' && 'count' in result && 'nonNullCount' in result && 'total' in result)
          assert.equal(result.count, BigInt(rows)); assert.equal(result.nonNullCount, BigInt(rows)); assert.equal(result.total, total)
        }
        samples.push({ operation, duration_ns: duration, rows: returned })
      }
    }
  }
  report.elapsed_ns = Number(process.hrtime.bigint() - began)
  report.status = 'complete'
} finally {
  try { if (session) await session.close(); await engine.close() }
  finally {
    report.max_rss_kib = process.resourceUsage().maxRSS
    if (recorder) { const window = recorder.roll(); assert.ok(window); report.window = sdk.windowRecord(window, model) }
    writeFileSync(args.output, JSON.stringify(report))
  }
}

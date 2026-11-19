/**
 * One soak application process in TypeScript, one engine, on an installed npm package: the
 * protocol of python/tests/qualification/soak_worker.py, with the values of soak_traffic.py.
 *
 *   node soak-worker.mjs --sdk <node_modules/@smart-data-engines/sde> --soak <soak.json>
 *
 * Monotonic times are written as numbers of nanoseconds: exact while the machine has been up for
 * less than 104 days (2^53 ns), which is longer than any soak.
 */
import { appendFileSync, existsSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const args = Object.fromEntries(Array.from({ length: (process.argv.length - 2) / 2 }, (_, index) =>
  [process.argv[2 + index * 2].replace(/^--/, ''), process.argv[3 + index * 2]]))
/** @type {typeof import('../../src/index.js')} */
const sdk = await import(pathToFileURL(join(args.sdk, 'dist/index.js')).href)
const soak = JSON.parse(readFileSync(args.soak, 'utf8'))
const project = JSON.parse(readFileSync(soak.project_config, 'utf8'))
const dialect = soak.dialect
const engineModule = dialect === 'postgres' ? 'dist/engines/postgres.js' : 'dist/engines/clickhouse.js'
const { PostgresEngine, ClickHouseEngine } = await import(pathToFileURL(join(args.sdk, engineModule)).href)
const model = sdk.assemble(project.model.entities.map(entity => ({
  name: entity.name, fields: entity.fields.map(field => ({ ...field, nullable: field.nullable === true })),
  key: entity.key, pii: entity.pii ?? [], residency: entity.residency ?? null,
})), (project.model.relations ?? []).map(relation => ({ name: relation.name, source: relation.from, target: relation.to })),
project.model.atomic ?? [], project.model.cost_ceiling ?? null)
const keys = Object.fromEntries(Object.entries(project.public_keys).map(([name, value]) => [name, Buffer.from(value, 'base64')]))
const dsn = process.env[project.engines[dialect].runtime_dsn_envs[0]]

// --- the values (soak_traffic.py) -----------------------------------------------------------------
const LANGUAGE = 'typescript', LANGUAGE_NUMBER = 2, BATCH = 20, ACCOUNTS = 3
const BASE = BigInt(Date.UTC(2026, 10, 20)) * 1000n
const CYCLES = {
  postgres: ['write', 'get', 'write', 'get', 'write', 'get', 'write', 'get', 'write', 'get',
    'write', 'get', 'write', 'get', 'write', 'get', 'write', 'get', 'scan', 'count'],
  clickhouse: ['write', 'get', 'get', 'get', 'get', 'write', 'get', 'get', 'get', 'scan',
    'write', 'get', 'get', 'get', 'get', 'write', 'get', 'get', 'get', 'count'],
}
const WINDOW = { postgres: 150, clickhouse: 10 }
const hex = (value, width) => value.toString(16).padStart(width, '0')
const uuid = (kind, number) => `${hex(LANGUAGE_NUMBER, 8)}-${hex(kind, 4)}-4000-8000-${hex(number, 12)}`
const at = micros => sdk.Timestamp.fromEpochMicroseconds(BASE + BigInt(micros))
const account = s => `soak-${LANGUAGE}-${s % ACCOUNTS}`
const station = `soak-${LANGUAGE}`
const isTransaction = s => s % 3 === 0
function order(s) {
  return { account: account(s), id: uuid(1, s), placed_at: at(s * 10),
    price: `${100 + s % 50}.12345678`, qty: `${1 + s % 5}.50000000`, side: s % 2 === 0 ? 'buy' : 'sell' }
}
function fills(s) {
  const placed = order(s)
  return [1, 2].map(part => ({ account: placed.account, at: at(s * 10 + part), id: uuid(2, s * 2 + part),
    order_id: placed.id, price: placed.price, qty: '0.75000000' }))
}
function reading(r) {
  const cents = 1525 + r % 1000
  return { at: at(r), celsius: `${Math.floor(cents / 100)}.${String(cents % 100).padStart(2, '0')}`,
    humidity: BigInt(30 + r % 70), id: uuid(3, r), station }
}
const batch = b => Array.from({ length: BATCH }, (_, i) => reading(b * BATCH + i))
const micros = timestamp => Number(timestamp.epochMicroseconds - BASE)

/** A decimal's value at a scale, so '1.5' and '1.50000000' are the same number. */
function scaled(text, scale) {
  if (typeof text !== 'string' || !/^-?\d+(\.\d+)?$/.test(text)) return null
  const [whole, fraction = ''] = text.split('.')
  if (fraction.length > scale && /[^0]/.test(fraction.slice(scale))) return null
  return BigInt(whole + fraction.slice(0, scale).padEnd(scale, '0'))
}
function same(actual, expected) {
  if (actual === null || actual === undefined) return false
  const names = Object.keys(expected).sort()
  if (JSON.stringify(Object.keys(actual).sort()) !== JSON.stringify(names)) return false
  return names.every(name => {
    const a = actual[name], e = expected[name]
    if (e instanceof sdk.Timestamp) return a instanceof sdk.Timestamp && a.epochMicroseconds === e.epochMicroseconds
    if (typeof e === 'bigint') return BigInt(a) === e
    if (name === 'price' || name === 'qty') return scaled(a, 8) !== null && scaled(a, 8) === scaled(e, 8)
    if (name === 'celsius') return scaled(a, 2) !== null && scaled(a, 2) === scaled(e, 2)
    return String(a).toLowerCase() === String(e).toLowerCase()
  })
}

// --- timings ----------------------------------------------------------------------------------------
const bucket = nanoseconds => {
  const us = Number(nanoseconds) / 1000
  return us < 1 ? 0 : 1 + Math.floor(Math.log2(us) * 8)
}
const now = () => process.hrtime.bigint()
const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds))

// --- state --------------------------------------------------------------------------------------------
const out = soak.output
let stop = false
process.on('SIGTERM', () => { stop = true })
let engine, session, connected = false
let attempted = -1, highestAcked = -1
const failed = new Set()
let failuresSinceSuccess = 0, failingSince = null
let running = null
const reportedStuck = new Set()
const totals = {}
const count = (name, by = 1) => { totals[name] = (totals[name] ?? 0) + by }

function event(record) {
  appendFileSync(join(out, 'events.jsonl'),
    JSON.stringify({ t: Number(now()), wall: new Date().toISOString(), ...record }) + '\n')
}
function newMinute() { return { started: now(), ops: {} } }
function op(minute, kind) {
  return (minute.ops[kind] ??= { ok: 0, errors: {}, skipped: 0, unavailable: 0, duration: {}, delay: {} })
}
function record(minute, kind, duration, delay, error) {
  const entry = op(minute, kind)
  if (error === null) entry.ok++
  else entry.errors[error] = (entry.errors[error] ?? 0) + 1
  const d = bucket(duration), l = bucket(delay > 0n ? delay : 0n)
  entry.duration[d] = (entry.duration[d] ?? 0) + 1
  entry.delay[l] = (entry.delay[l] ?? 0) + 1
}
function flushMinute(minute) {
  appendFileSync(join(out, 'minutes.jsonl'), JSON.stringify({ t: Number(minute.started), end: Number(now()),
    wall: new Date().toISOString(), ops: minute.ops, attempted, highest_acked: highestAcked, connected }) + '\n')
}

async function connect() {
  if (engine === undefined) engine = dialect === 'postgres' ? new PostgresEngine(dsn) : new ClickHouseEngine(dsn)
  else await engine.close()
  await engine.connect()
  const placement = sdk.loadMap(JSON.parse(readFileSync(join(soak.project_dir, 'active-map.json'), 'utf8')),
    { model, publicKey: keys, requireSignature: true })
  session = await sdk.Session.open(model, placement, { [dialect]: engine }, { projectId: project.project_id })
  connected = true
}

// --- operations ---------------------------------------------------------------------------------------
function checkFailed(kind, number, what) { count('check_failed'); event({ check_failed: kind, number, what }) }

async function write() {
  const number = attempted + 1
  attempted = number
  if (dialect === 'postgres') {
    if (isTransaction(number)) {
      await session.transaction(['Order', 'Fill'], async inner => {
        await inner.save('Order', order(number))
        for (const fill of fills(number)) await inner.save('Fill', fill)
      })
    } else {
      await session.save('Order', order(number))
    }
  } else {
    await session.saveMany('WeatherReading', batch(number))
  }
}
function pick(slot) {
  if (highestAcked < 0) return null
  let candidate = slot % 2 === 0 ? highestAcked : (slot * 7919) % (highestAcked + 1)
  while (candidate >= 0 && failed.has(candidate)) candidate--
  return candidate >= 0 ? candidate : null
}
async function get(slot) {
  const number = pick(slot)
  if (number === null) return
  let expected, found
  if (dialect === 'postgres') {
    expected = order(number)
    found = await session.get('Order', { account: expected.account, placed_at: expected.placed_at })
  } else {
    expected = reading(number * BATCH)
    found = await session.get('WeatherReading', { station: expected.station, at: expected.at })
  }
  if (!same(found, expected)) checkFailed('get', number, 'the row read is not the row written')
}
function window() {
  if (highestAcked < 0) return null
  return [Math.max(0, highestAcked - WINDOW[dialect] + 1), highestAcked]
}
function range(low, high) {
  return dialect === 'postgres'
    ? { field: 'placed_at', low: order(low).placed_at, high: at(high * 10 + 1) }
    : { field: 'at', low: at(low * BATCH), high: at(high * BATCH + BATCH) }
}
async function scan(slot) {
  const bounds = window()
  if (bounds === null) return
  const [low, high] = bounds
  const seen = new Set()
  let expected
  if (dialect === 'postgres') {
    const name = account(slot)
    const page = await session.scan('Order', { where: { account: name }, bounds: range(low, high), limit: 200 })
    for (const row of page.rows) {
      const number = micros(row.placed_at) / 10
      if (!same(row, order(number))) checkFailed('scan', number, 'a scanned row is not the row written')
      seen.add(number)
    }
    expected = []
    for (let n = low; n <= high; n++) if (!failed.has(n) && account(n) === name) expected.push(n)
  } else {
    const page = await session.scan('WeatherReading',
      { where: { station }, bounds: range(low, high), limit: BATCH * WINDOW.clickhouse })
    for (const row of page.rows) {
      const number = micros(row.at)
      if (!same(row, reading(number))) checkFailed('scan', number, 'a scanned row is not the row written')
      seen.add(Math.floor(number / BATCH))
    }
    expected = []
    for (let n = low; n <= high; n++) if (!failed.has(n)) expected.push(n)
  }
  const missing = expected.filter(n => !seen.has(n))
  if (missing.length > 0) checkFailed('scan', Math.min(...missing), `${missing.length} acknowledged writes missing`)
}
async function countOp(slot) {
  const bounds = window()
  if (bounds === null) return
  const [low, high] = bounds
  let counted, least = 0, most = 0
  if (dialect === 'postgres') {
    const name = account(slot)
    counted = await session.count('Order', { where: { account: name }, bounds: range(low, high) })
    for (let n = low; n <= high; n++) {
      if (account(n) !== name) continue
      most++
      if (!failed.has(n)) least++
    }
  } else {
    counted = await session.count('WeatherReading', { where: { station }, bounds: range(low, high) })
    for (let n = low; n <= high; n++) { most += BATCH; if (!failed.has(n)) least += BATCH }
  }
  const value = Number(counted)
  if (!(least <= value && value <= most)) checkFailed('count', low, `counted ${value}, expected ${least} to ${most}`)
}

// --- the loop -------------------------------------------------------------------------------------------
const STUCK_REPORTS_S = [60, 300, 900, 3600]
const SLOW_NS = 1_000_000_000n
const watchdog = setInterval(() => {
  if (running === null) return
  const elapsed = Number(now() - running.started) / 1e9
  for (const mark of STUCK_REPORTS_S) {
    const key = `${running.started}:${mark}`
    if (elapsed >= mark && !reportedStuck.has(key)) {
      reportedStuck.add(key)
      event({ stuck: running.kind, running_s: mark, started: Number(running.started) })
    }
  }
}, 1000)

async function operate(minute, kind, slot, due) {
  const began = now()
  running = { kind, started: began }
  let number = null, error = null, named = true, message = ''
  try {
    if (kind === 'write') { number = attempted + 1; await write(); highestAcked = Math.max(highestAcked, number) }
    else if (kind === 'get') await get(slot)
    else if (kind === 'scan') await scan(slot)
    else await countOp(slot)
  } catch (failure) {
    named = failure instanceof sdk.SdeError
    error = failure?.name ?? typeof failure
    message = String(failure?.message ?? failure).slice(0, 300)
    if (!named) event({ traceback: String(failure?.stack ?? failure).slice(-2000) })
  } finally {
    running = null
  }
  const ended = now()
  record(minute, kind, ended - began, began - due, error)
  if (ended - began >= SLOW_NS) event({ slow: kind, duration_ns: Number(ended - began), ok: error === null })
  if (error === null) {
    count(kind)
    if (failuresSinceSuccess > 0) {
      event({ recovered: kind, after_failures: failuresSinceSuccess,
        since_first_failure_ns: Number(ended - (failingSince ?? ended)) })
    }
    failuresSinceSuccess = 0; failingSince = null
    return
  }
  count(`error_${error}`)
  if (number !== null) failed.add(number)
  if (failuresSinceSuccess === 0) failingSince = began
  failuresSinceSuccess++
  event({ error, named, op: kind, number, duration_ns: Number(ended - began), message })
  connected = false
}

async function main() {
  const start = JSON.parse(readFileSync(soak.start_file, 'utf8'))
  const zero = BigInt(start.monotonic_ns), period = BigInt(soak.period_ms) * 1_000_000n
  const cycle = CYCLES[dialect]
  try { await connect() } catch (failure) {
    connected = false
    event({ connect_failed: failure?.name, message: String(failure?.message ?? failure).slice(0, 300) })
  }
  let minute = newMinute()
  let slot = now() > zero ? (now() - zero) / period : 0n
  while (!stop && !existsSync(soak.stop_file)) {
    const tick = now()
    if (tick - minute.started >= 60_000_000_000n) { flushMinute(minute); minute = newMinute() }
    const due = zero + slot * period
    if (tick < due) { await pause(Math.min(Number(due - tick) / 1e6, 50)); continue }
    const kind = cycle[Number(slot % BigInt(cycle.length))]
    if (tick - due > period) { op(minute, kind).skipped++; slot++; continue }
    if (!connected) {
      const began = now()
      running = { kind: 'connect', started: began }
      try { await connect() } catch (failure) {
        running = null
        event({ connect_failed: failure?.name, duration_ns: Number(now() - began),
          message: String(failure?.message ?? failure).slice(0, 300) })
        op(minute, kind).unavailable++
        slot++
        continue
      }
      running = null
      event({ connected: true, duration_ns: Number(now() - began) })
    }
    await operate(minute, kind, Number(slot), due)
    slot++
  }
  flushMinute(minute)
}

console.log('READY')
try {
  await main()
} finally {
  clearInterval(watchdog)
  if (engine !== undefined) { try { await engine.close() } catch { /* closing after a failure */ } }
  writeFileSync(join(out, 'done.json'), JSON.stringify({ language: LANGUAGE, dialect, attempted,
    highest_acked: highestAcked, failed: [...failed].sort((a, b) => a - b), totals, sdk_module: args.sdk }))
}

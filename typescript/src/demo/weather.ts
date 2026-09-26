/** Bounded customer workload. Values and runtime credentials remain in this process. */
import { isDeepStrictEqual } from 'node:util'
import { randomUUID } from 'node:crypto'
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'
import { EngineError, MapRolledBack, MigrationRefused } from '../errors.js'
import { PostgresEngine } from '../engines/postgres.js'
import { ClickHouseEngine } from '../engines/clickhouse.js'
import { Session } from '../session.js'
import type { ManagedEngine, Row } from '../session.js'
import { Recorder, windowRecord } from '../telemetry.js'
import { baseTime, celsiusBaseCents, celsiusModulus, generatorId, humidityBase, humidityModulus, reading } from './model.js'
import { DemoRefused, project, read, write } from './project.js'

/** What a run can drive. The CLI offers and the runtime accepts this one list. */
export const workloads = ['mixed', 'point', 'analytics', 'fleet', 'alerts'] as const
export type Workload = typeof workloads[number]
/** The fleet checks one cross-station page exactly; its count and sum cover the whole window. */
const fleetPage = 100
const maxRunRows = 10000
/**
 * The alert threshold. Humidity is 30 + sequence % 70, so a reading alerts when sequence % 70 is 65
 * or more - five readings in seventy, known exactly from the sequence number.
 */
export const alertHumidity = 95
export const alertPage = 100

export interface RunOptions {
  iterations?: number; batchSize?: number; intervalMs?: number; recoveryMs?: number
  workload?: Workload
}
export interface RunReport {
  protocol: 2; generator_id: string; run_id: string; language: 'typescript'; status: 'running' | 'complete' | 'incomplete'
  workload: string; sdk_version: string; sdk_module: string; project_id: string; model_version: string
  acknowledged_rows: number; verified_after_uncertain_rows: number; verified_rows: number
  pending: { first: number; count: number } | null; map_versions: number[]; read_retries: number
  elapsed_ns?: string; failure?: string; cleanup_failed?: boolean
}
function recoverable(error: unknown) {
  return error instanceof EngineError || error instanceof MapRolledBack || error instanceof MigrationRefused
}
function same(actual: Row | null, expected: Row) {
  if (!isDeepStrictEqual(actual, expected)) throw new DemoRefused('A logical read did not match this run.')
}
/**
 * Every earlier run of this project, by run ID, with the rows it verified.
 *
 * Fleet analytics reads every station's rows in one time window, and every run in a directory writes
 * the same timeline, so the exact answer is a sum over the runs - known exactly, because generated
 * values depend only on the sequence number and each run's local report says how many rows it
 * verified. A run that did not complete makes that sum unknowable, so the workload refuses instead
 * of comparing a read with a guess. The same rules as the Python starter's fleet_runs.
 */
export function fleetRuns(root: string, projectId: string): Map<string, number> {
  const runs = new Map<string, number>()
  const directory = join(root, 'runs')
  if (!existsSync(directory)) return runs
  for (const name of readdirSync(directory).sort()) {
    const path = join(directory, name, 'report.json')
    if (!existsSync(path)) continue
    const report = read(path).value
    if (report.project_id !== projectId) continue
    const identity = report.run_id, rows = report.verified_rows
    if (typeof identity !== 'string' || !/^[0-9a-f]{32}$/.test(identity) || name !== identity ||
        report.status !== 'complete' || report.pending !== null || report.generator_id !== generatorId ||
        typeof rows !== 'number' || !Number.isSafeInteger(rows) || rows < 0 || rows > maxRunRows) {
      throw new DemoRefused("Fleet analytics reads every run's rows, so every earlier run of this project must " +
        'be complete; inspect the unfinished run first.')
    }
    runs.set(identity, rows)
  }
  return runs
}
/**
 * The first page, row count and celsius total of the window holding sequences 1..through. A page is
 * in key order, station then time; a station is its run's namespace and is ASCII, so the default
 * string order is the engines' byte order.
 */
export function fleetExpected(runs: Map<string, number>, through: number, limit: number): [Row[], number, string] {
  const page: Row[] = []
  let total = 0, cents = 0n
  const ordered = [...runs.keys()].sort((left, right) => {
    const a = reading(left, 0, 1).station, b = reading(right, 0, 1).station
    return a < b ? -1 : a > b ? 1 : 0
  })
  for (const identity of ordered) {
    const rows = Math.min(through, runs.get(identity)!)
    total += rows
    for (let sequence = 1; sequence <= rows; sequence++) {
      cents += BigInt(celsiusBaseCents + sequence % celsiusModulus)
      if (page.length < limit) page.push(reading(identity, 0, sequence))
    }
  }
  return [page, total, `${cents / 100n}.${String(cents % 100n).padStart(2, '0')}`]
}
/**
 * The first page, count and celsius total of this run's alerts among sequences 1..through. One
 * station, so key order - station, then time - is sequence order. With no alert yet the total is
 * null, as the library reports a summary of no values on every engine. The same rules as the Python
 * starter's alert_expected.
 */
export function alertExpected(runId: string, through: number, limit: number): [Row[], number, string | null] {
  const page: Row[] = []
  let total = 0, cents = 0n
  for (let sequence = 1; sequence <= through; sequence++) {
    if (humidityBase + sequence % humidityModulus < alertHumidity) continue
    total++
    cents += BigInt(celsiusBaseCents + sequence % celsiusModulus)
    if (page.length < limit) page.push(reading(runId, 0, sequence))
  }
  return [page, total, total ? `${cents / 100n}.${String(cents % 100n).padStart(2, '0')}` : null]
}
export async function runWeather(root: string, options: RunOptions = {}): Promise<RunReport> {
  const { iterations = 10, batchSize = 10, intervalMs = 100, recoveryMs = 10000, workload = 'mixed' } = options
  if (![iterations, batchSize, intervalMs, recoveryMs].every(Number.isSafeInteger) ||
      iterations < 1 || iterations > 1000 || batchSize < 1 || batchSize > 1000 || iterations * batchSize > maxRunRows ||
      intervalMs < 0 || intervalMs > 1000 || recoveryMs < 0 || recoveryMs > 30000 ||
      !(workloads as readonly string[]).includes(workload)) throw new DemoRefused('Invalid bounded workload options.')
  const { model, bindings, projectId, activeMap } = project(root)
  // Read before this run's own report exists; a run that starts later is not in the window.
  const earlier = workload === 'fleet' ? fleetRuns(root, projectId) : new Map<string, number>()
  const factories: Record<string, () => ManagedEngine> = {}
  for (const [name, binding] of Object.entries(bindings)) factories[name] = () =>
    binding.dialect === 'postgres' ? new PostgresEngine(binding.dsn) : new ClickHouseEngine(binding.dsn)
  const runId = randomUUID().replaceAll('-', ''), directory = join(root, 'runs', runId)
  const recorder = new Recorder(model.version)
  let session: Session | undefined, fingerprint: string | undefined
  const report: RunReport = { protocol: 2, generator_id: generatorId, run_id: runId, language: 'typescript', status: 'running', workload,
    sdk_version: String(JSON.parse(readFileSync(new URL('../../package.json', import.meta.url), 'utf8')).version),
    sdk_module: new URL('../index.js', import.meta.url).href, project_id: projectId, model_version: model.version,
    acknowledged_rows: 0, verified_after_uncertain_rows: 0, verified_rows: 0, pending: null,
    map_versions: [], read_retries: 0 }
  function checkpoint() { write(join(directory, 'report.json'), report) }
  async function opened(fresh = false): Promise<Session> {
    const placement = activeMap()
    if (fresh || session === undefined || placement.fingerprint !== fingerprint) {
      if (session !== undefined) { await session.close(); session = undefined }
      const required = new Set(Object.values(placement.groups).flatMap(group =>
        [group.source, ...group.derived].map(material => material.engine)))
      const activeFactories = Object.fromEntries([...required].map(name => {
        if (!factories[name]) throw new DemoRefused('An active materialization has no local binding.')
        return [name, factories[name]!]
      }))
      session = await Session.connect(model, placement, activeFactories, { recorder, projectId })
      fingerprint = placement.fingerprint
      if (!report.map_versions.includes(placement.mapVersion)) report.map_versions.push(placement.mapVersion)
    }
    return session
  }
  async function readRetry<T>(action: (client: Session) => Promise<T>): Promise<T> {
    const deadline = performance.now() + recoveryMs
    let fresh = false
    for (;;) {
      try { return await action(await opened(fresh)) }
      catch (error) {
        if (!recoverable(error) || performance.now() >= deadline) throw error
        report.read_retries++; fresh = true; await pause(50)
      }
    }
  }
  async function checkRows(client: Session, rows: Row[]) {
    for (const row of rows) {
      const actual = await client.get('WeatherReading', { station: row.station, at: row.at }, { fresh: true })
      if (actual === null) return false
      same(actual, row)
    }
    return true
  }
  checkpoint()
  const began = process.hrtime.bigint()
  try {
    // The group's size at the start of the run and again at its end, for the window: the engine's
    // catalogue answers with numbers, and a refused read leaves the size unknown.
    await readRetry(current => current.measureStorage())
    for (let iteration = 0; iteration < iterations; iteration++) {
      const first = iteration * batchSize + 1
      const rows = Array.from({ length: batchSize }, (_, index) => reading(runId, 0, first + index))
      const client = await readRetry(async current => current)
      report.pending = { first, count: batchSize }; checkpoint()
      try { await client.saveMany('WeatherReading', rows); report.acknowledged_rows += batchSize }
      catch (error) {
        if (!(error instanceof EngineError)) throw error
        const deadline = performance.now() + recoveryMs
        for (;;) {
          try {
            if (await checkRows(await opened(true), rows)) { report.verified_after_uncertain_rows += batchSize; break }
          } catch (readError) { if (!recoverable(readError)) throw readError }
          if (performance.now() >= deadline) throw new DemoRefused(
            'Write outcome is uncertain. Inspect the pending range locally; the starter did not replay it.')
          await pause(50)
        }
      }
      report.pending = null; checkpoint()
      const last = rows.at(-1)!, count = first + batchSize - 1, where = { station: last.station }
      for (let index = 0; index < (workload === 'point' ? 20 : 1); index++) {
        same(await readRetry(current => current.get('WeatherReading', { station: last.station, at: last.at })), last)
      }
      if (workload === 'fleet') {
        // Every station, one time window: the traffic a time-first layout is for. The expected
        // answer is exact, from this directory's run reports (fleetRuns).
        const window = new Map(earlier).set(runId, count)
        const [expected, rows, celsius] = fleetExpected(window, count, fleetPage)
        const bounds = { field: 'at', low: reading(runId, 0, 1).at, high: reading(runId, 0, count + 1).at }
        const page = await readRetry(current => current.scan('WeatherReading', { bounds, limit: expected.length }))
        if (page.rows.length !== expected.length) throw new DemoRefused("The fleet page did not match this directory's runs.")
        page.rows.forEach((row, index) => same(row, expected[index]!))
        if (await readRetry(current => current.count('WeatherReading', { bounds })) !== BigInt(rows)) {
          throw new DemoRefused("The fleet count did not match this directory's runs.")
        }
        const summary = await readRetry(current => current.summarize('WeatherReading', 'celsius', { bounds }))
        if (summary.count !== BigInt(rows) || summary.total !== celsius) {
          throw new DemoRefused("The exact fleet summary did not match this directory's runs.")
        }
      } else if (workload === 'alerts') {
        // One station's readings at or above the alert threshold: a range on humidity, a field
        // outside the key, which the key cannot serve and an index can. Exact from the generator.
        const [expected, alerts, celsius] = alertExpected(runId, count, alertPage)
        const bounds = { field: 'humidity', low: BigInt(alertHumidity) }
        const page = await readRetry(current => current.scan('WeatherReading', { where, bounds, limit: alertPage }))
        if (page.rows.length !== expected.length) throw new DemoRefused('The alert page did not match this run.')
        page.rows.forEach((row, index) => same(row, expected[index]!))
        if (await readRetry(current => current.count('WeatherReading', { where, bounds })) !== BigInt(alerts)) {
          throw new DemoRefused('The alert count did not match this run.')
        }
        const summary = await readRetry(current => current.summarize('WeatherReading', 'celsius', { where, bounds }))
        if (summary.count !== BigInt(alerts) || summary.total !== celsius) {
          throw new DemoRefused('The exact alert summary did not match this run.')
        }
      } else if (workload !== 'point' || iteration === iterations - 1) {
        const page = await readRetry(current => current.scan('WeatherReading', {
          where, bounds: { field: 'at', low: baseTime }, limit: Math.min(count, 1000),
        }))
        if (page.rows.length !== Math.min(count, 1000)) throw new DemoRefused('The bounded page has a different length.')
        page.rows.forEach((row, index) => same(row, reading(runId, 0, index + 1)))
        if (await readRetry(current => current.count('WeatherReading', { where })) !== BigInt(count)) {
          throw new DemoRefused('The logical count did not match this run.')
        }
        const summary = await readRetry(current => current.summarize('WeatherReading', 'celsius', { where }))
        let cents = 0n
        for (let index = 1; index <= count; index++) cents += BigInt(1525 + index % 1000)
        const total = `${cents / 100n}.${String(cents % 100n).padStart(2, '0')}`
        if (summary.count !== BigInt(count) || summary.total !== total) throw new DemoRefused('The exact summary did not match this run.')
      }
      report.verified_rows = count; checkpoint()
      if (intervalMs && iteration + 1 < iterations) await pause(intervalMs)
    }
    await readRetry(current => current.measureStorage())
    report.status = 'complete'
  } catch (error) {
    report.status = 'incomplete'; report.failure = error instanceof Error ? error.name : 'UnknownError'; throw error
  } finally {
    report.elapsed_ns = String(process.hrtime.bigint() - began)
    try { if (session !== undefined) await session.close() }
    catch (error) { report.status = 'incomplete'; report.cleanup_failed = true; throw error }
    finally {
      const window = recorder.roll()
      if (window !== undefined) { write(join(directory, 'window.json'), windowRecord(window, model)); recorder.acknowledge(1) }
      checkpoint()
    }
  }
  return report
}

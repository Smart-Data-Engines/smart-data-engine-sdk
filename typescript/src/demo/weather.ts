/** Bounded customer workload. Values and runtime credentials remain in this process. */
import { isDeepStrictEqual } from 'node:util'
import { randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'
import { EngineError, MapRolledBack, MigrationRefused } from '../errors.js'
import { PostgresEngine } from '../engines/postgres.js'
import { ClickHouseEngine } from '../engines/clickhouse.js'
import { Session } from '../session.js'
import type { ManagedEngine, Row } from '../session.js'
import { Recorder, windowRecord } from '../telemetry.js'
import { baseTime, generatorId, reading } from './model.js'
import { DemoRefused, project, write } from './project.js'

export interface RunOptions {
  iterations?: number; batchSize?: number; intervalMs?: number; recoveryMs?: number
  workload?: 'mixed' | 'point' | 'analytics'
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
export async function runWeather(root: string, options: RunOptions = {}): Promise<RunReport> {
  const { iterations = 10, batchSize = 10, intervalMs = 100, recoveryMs = 10000, workload = 'mixed' } = options
  if (![iterations, batchSize, intervalMs, recoveryMs].every(Number.isSafeInteger) ||
      iterations < 1 || iterations > 1000 || batchSize < 1 || batchSize > 1000 || iterations * batchSize > 10000 ||
      intervalMs < 0 || intervalMs > 1000 || recoveryMs < 0 || recoveryMs > 30000 ||
      !['mixed', 'point', 'analytics'].includes(workload)) throw new DemoRefused('Invalid bounded workload options.')
  const { model, bindings, projectId, activeMap } = project(root)
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
      if (workload !== 'point' || iteration === iterations - 1) {
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

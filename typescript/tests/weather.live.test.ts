/** Real local bootstrap plus the TypeScript runtime; values never cross to a controller. */
import { expect, it, vi } from 'vitest'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { PostgresEngine } from '../src/engines/postgres.js'
import { ClickHouseEngine } from '../src/engines/clickhouse.js'
import { EngineError } from '../src/errors.js'
import { runWeather } from '../src/demo/weather.js'

const workspace = fileURLToPath(new URL('../../', import.meta.url))
const python = process.env.SDE_TEST_PYTHON ?? resolve(workspace, 'python/.venv/bin/python')
const peer = resolve(workspace, 'python/tests/weather_starter_peer.py')
const enabled = Boolean(process.env.SDE_POSTGRES_DSN && process.env.SDE_CLICKHOUSE_DSN)
for (const source of ['postgres', 'clickhouse']) it.skipIf(!enabled)(`runs Weather from a restricted ${source} source`, async () => {
  const directory = mkdtempSync(join(tmpdir(), 'sde-weather-live-'))
  function call(command: string) {
    return execFileSync(python, [peer, command, '--directory', directory, '--source', source], {
      timeout: 60000, encoding: 'utf8', env: { ...process.env, PYTHONPATH: resolve(workspace, 'python/src') },
    })
  }
  try {
    expect(JSON.parse(call('setup')).status).toBe('ready')
    const unused = source === 'postgres' ? ClickHouseEngine : PostgresEngine
    const offline = vi.spyOn(unused.prototype, 'connect').mockRejectedValue(new EngineError('unused engine unavailable'))
    const first = await runWeather(directory, { iterations: 2, batchSize: 3, intervalMs: 0 })
    const second = await runWeather(directory, { iterations: 1, batchSize: 2, intervalMs: 0 })
    expect(first.status).toBe('complete'); expect(first.verified_rows).toBe(6)
    expect(second.status).toBe('complete'); expect(second.verified_rows).toBe(2)
    expect(first.run_id).not.toBe(second.run_id)
    expect(offline).not.toHaveBeenCalled()
    const window = readFileSync(join(directory, 'runs', first.run_id, 'window.json'), 'utf8')
    expect(window).toContain(first.model_version)
    expect(window).not.toContain('weather-' + first.run_id)
    // Measured at the start and the end of the run, exactly as the Python starter does: a size,
    // no secondary index, how the writes arrived, and a share of calls whose filter named `at`
    // equal to what the same window's `filtered_on` reports. A day is not projected from seconds.
    const body = JSON.parse(window).groups.WeatherReading
    expect(body.total_bytes).toBeGreaterThan(0)
    expect(body.index_to_table_ratio).toBe(0)
    expect(body.write_burstiness).toBeGreaterThanOrEqual(1)
    let timed = 0
    for (const shape of body.shapes as { filtered_on?: { equal: string[]; range?: string; calls: number }[] }[]) {
      for (const entry of shape.filtered_on ?? []) if (entry.range === 'at' || entry.equal.includes('at')) timed += entry.calls
    }
    expect(body.time_filtered_share).toBe(timed / body.calls)
    expect(body.missing).toEqual(['daily_growth_bytes'])
    // A Python run in the same directory, then fleet analytics across every station: the exact
    // expectation includes the other language's rows, read from its local report.
    const written = JSON.parse(call('run'))
    expect(written.status).toBe('complete'); expect(written.verified_rows).toBe(3)
    const fleet = await runWeather(directory, { iterations: 2, batchSize: 4, intervalMs: 0, workload: 'fleet' })
    expect(fleet.status).toBe('complete'); expect(fleet.verified_rows).toBe(8)
    const shapes = JSON.parse(readFileSync(join(directory, 'runs', fleet.run_id, 'window.json'), 'utf8')).groups.WeatherReading.shapes
    const filters = Object.fromEntries(shapes.map((entry: { kind: string; filtered_on?: unknown }) => [entry.kind, entry.filtered_on]))
    expect(filters.range_read).toEqual([{ equal: [], range: 'at', calls: 2 }])
    expect(filters.aggregate).toEqual([{ equal: [], range: 'at', calls: 4 }])
    // Alerts read one station's readings at or above a humidity: no alert in the first iteration
    // (an empty page and a summary of nothing), five in the second.
    const alerts = await runWeather(directory, { iterations: 2, batchSize: 40, intervalMs: 0, workload: 'alerts' })
    expect(alerts.status).toBe('complete'); expect(alerts.verified_rows).toBe(80)
    const alertShapes = JSON.parse(readFileSync(join(directory, 'runs', alerts.run_id, 'window.json'), 'utf8')).groups.WeatherReading.shapes
    const alertFilters = Object.fromEntries(alertShapes.map((entry: { kind: string; filtered_on?: unknown }) => [entry.kind, entry.filtered_on]))
    expect(alertFilters.range_read).toEqual([{ equal: ['station'], range: 'humidity', calls: 2 }])
    expect(alertFilters.aggregate).toEqual([{ equal: ['station'], range: 'humidity', calls: 4 }])
    expect(JSON.parse(call('reset')).status).toBe('reset')
    expect(JSON.parse(call('reset')).status).toBe('reset')
  } finally {
    vi.restoreAllMocks()
    call('reset')
    rmSync(directory, { recursive: true, force: true })
  }
}, 120000)

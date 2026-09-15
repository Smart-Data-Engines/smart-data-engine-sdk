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
    expect(JSON.parse(call('reset')).status).toBe('reset')
    expect(JSON.parse(call('reset')).status).toBe('reset')
  } finally {
    vi.restoreAllMocks()
    call('reset')
    rmSync(directory, { recursive: true, force: true })
  }
}, 120000)

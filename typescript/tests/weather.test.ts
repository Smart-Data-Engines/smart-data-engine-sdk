/** Public starter regressions: no uncertain replay, exact values, owned-session cleanup. */
import { afterEach, expect, it, vi } from 'vitest'
import { createHash, generateKeyPairSync, sign as edSign } from 'node:crypto'
import { mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { isDeepStrictEqual } from 'node:util'
import { canonicalBytes, digest16 } from '../src/canonical.js'
import { neutralDeclaration } from '../src/model.js'
import { Session } from '../src/session.js'
import type { Row } from '../src/session.js'
import { EngineError } from '../src/errors.js'
import { generatorId, reading, weatherModel } from '../src/demo/model.js'
import { project, read, write } from '../src/demo/project.js'
import { alertExpected, alertHumidity, fleetExpected, fleetRuns, requireDrivers, runWeather, workloads } from '../src/demo/weather.js'

const roots: string[] = []
afterEach(() => { vi.restoreAllMocks(); for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true }) })
function fixture() {
  const root = mkdtempSync(join(tmpdir(), 'sde-weather-unit-')); roots.push(root)
  const key = generateKeyPairSync('ed25519'), model = weatherModel(), projectId = '1'.repeat(32)
  const publicKey = key.publicKey.export({ type: 'spki', format: 'der' }).subarray(-32).toString('base64')
  const config = { protocol: 1, project_id: projectId, model: neutralDeclaration(model), public_keys: { test: publicKey },
    engines: { db: { dialect: 'postgres', operator_dsn_env: 'UNUSED', runtime_dsn_envs: ['UNUSED_RUNTIME'] } } }
  function signed(raw: Record<string, unknown>) {
    const value = structuredClone(raw); delete value.signature
    return { ...value, signature: { alg: 'ed25519', key_id: 'test', value: edSign(null, canonicalBytes(value), key.privateKey).toString('base64') } }
  }
  const current = { contract: 4, project_id: projectId, model_version: model.version, map_version: 1,
    groups: { WeatherReading: { write_epoch: 1, source: { id: 'source', engine: 'db', layout: {
      tables: { WeatherReading: 'weather_reading' }, columns: { WeatherReading: {
        at: 'timestamptz', celsius: 'numeric(8,2)', humidity: 'bigint', id: 'uuid', station: 'text',
      } },
    } } } } }
  write(join(root, 'config.json'), config)
  write(join(root, 'setup-complete.json'), { protocol: 1, config_digest: digest16(config) })
  write(join(root, 'state', 'active-map.json'), signed(current))
  write(join(root, 'runtime-credentials.json'), { db: 'runtime-secret-marker' })
  write(join(root, 'resources.json'), { status: 'ready', credential_hashes: {
    runtime: createHash('sha256').update(read(join(root, 'runtime-credentials.json')).payload).digest('hex'),
  } })
  return { root, current, signed }
}
function workload(mode = 'ok', afterSave = () => {}) {
  const rows: Row[] = [], copyRows: Row[] = [], sessions: { closed: boolean }[] = []
  let saves = 0, measured = 0
  vi.spyOn(Session, 'connect').mockImplementation(async () => {
    const client = { closed: false,
      async close() { client.closed = true },
      async measureStorage() { measured++; return { sizes: [], unavailable: {} } },
      async saveMany(_entity: string, batch: Row[]) {
        saves++
        if (mode === 'copy_only') copyRows.push(...batch)
        else if (mode !== 'absent') rows.push(...(mode === 'partial' ? batch.slice(0, 1) : batch))
        afterSave()
        if (mode !== 'ok') throw new EngineError('driver may echo runtime-secret-marker')
      },
      async get(_entity: string, key: Row, options: { fresh?: boolean } = {}) {
        const visible = options.fresh ? rows : [...rows, ...copyRows]
        return visible.find(row => row.station === key.station && isDeepStrictEqual(row.at, key.at)) ?? null
      },
      async scan(_entity: string, options: { limit: number }) { return { rows: [...rows, ...copyRows].slice(0, options.limit), nextAfter: null } },
      async count() { return BigInt(rows.length + copyRows.length) },
      async summarize() {
        const cents = [...rows, ...copyRows].reduce((total, row) => total + BigInt(String(row.celsius).replace('.', '')), 0n)
        return { count: BigInt(rows.length + copyRows.length), total: `${cents / 100n}.${String(cents % 100n).padStart(2, '0')}` }
      },
    }
    sessions.push(client)
    return client as unknown as Session
  })
  return { rows, sessions, saves: () => saves, measured: () => measured }
}
for (const mode of ['absent', 'partial', 'copy_only']) it(`does not replay or complete an uncertain ${mode} batch`, async () => {
  const { root } = fixture(), fake = workload(mode)
  await expect(runWeather(root, { iterations: 1, batchSize: 2, recoveryMs: 0 })).rejects.toThrow('uncertain')
  expect(fake.saves()).toBe(1); expect(fake.rows).toHaveLength(mode === 'partial' ? 1 : 0)
  const report = read(join(root, 'runs', readdirSync(join(root, 'runs'))[0]!, 'report.json')).value
  expect(report.status).toBe('incomplete'); expect(report.pending).toEqual({ first: 1, count: 2 })
  expect(report.acknowledged_rows).toBe(0); expect(report.verified_rows).toBe(0)
  expect(fake.sessions.every(client => client.closed)).toBe(true)
  expect(JSON.stringify(report)).not.toContain('runtime-secret-marker')
})
it('resolves an exact visible batch without another write', async () => {
  const { root } = fixture(), fake = workload('visible')
  const report = await runWeather(root, { iterations: 1, batchSize: 2, recoveryMs: 0 })
  expect(fake.saves()).toBe(1); expect(report.status).toBe('complete')
  expect(report.acknowledged_rows).toBe(0); expect(report.verified_after_uncertain_rows).toBe(2)
  expect(fake.sessions.every(client => client.closed)).toBe(true)
  expect(fake.measured(), 'the size is measured at the start of the run and at its end').toBe(2)
})
it('closes the prior owned session when the local map changes', async () => {
  const { root, current, signed } = fixture()
  const fake = workload('ok', () => write(join(root, 'state', 'active-map.json'), signed({ ...current, map_version: 2 })))
  const report = await runWeather(root, { iterations: 2, batchSize: 1, intervalMs: 0 })
  expect(report.map_versions).toEqual([1, 2]); expect(fake.sessions).toHaveLength(2)
  expect(fake.sessions.every(client => client.closed)).toBe(true)
})
it('needs no operator credentials and refuses a changed runtime file', () => {
  const { root } = fixture()
  expect(project(root).model.version).toBe(weatherModel().version)
  write(join(root, 'runtime-credentials.json'), { db: 'changed' })
  expect(() => project(root)).toThrow('credentials differ')
})
it('refuses missing credential hash and a reset marker', () => {
  const { root } = fixture()
  write(join(root, 'resources.json'), { status: 'ready' })
  expect(() => project(root)).toThrow()
  write(join(root, 'reset-request.json'), { allocation_id: 'test' })
  expect(() => project(root)).toThrow('Reset')
})
it('uses distinct deterministic run namespaces and retains microseconds', () => {
  const first = reading('a'.repeat(32), 0, 999), second = reading('b'.repeat(32), 0, 999)
  expect(first.celsius).toBe('25.24'); expect(first.humidity).toBe(49n)
  expect(first.at.toISOString()).toBe('2026-01-01T00:00:00.000999Z')
  expect(first.id).not.toBe(second.id); expect(first.station).not.toBe(second.station)
  expect(reading('a'.repeat(32), 0, 999)).toEqual(first)
})

it('matches the shared neutral model and exact cross-language generator fixture', () => {
  const sample = JSON.parse(readFileSync(new URL('../../examples/weather/generator.json', import.meta.url), 'utf8'))
  const declaration = JSON.parse(readFileSync(new URL('../../examples/weather/model.json', import.meta.url), 'utf8'))
  expect(neutralDeclaration(weatherModel())).toEqual(declaration)
  expect(weatherModel().version).toBe(sample.model_version)
  const row = reading(sample.run_id, sample.worker, sample.sequence)
  expect({ ...row, at: row.at.toISOString(), humidity: String(row.humidity) }).toEqual(sample.reading)
})

it('pins the generator descriptor and every supported sequence to the shared baseline', async () => {
  const { generatorId, generatorSpec } = await import('../src/demo/model.js')
  const fixture = JSON.parse(readFileSync(new URL('../../examples/weather/generator.json', import.meta.url), 'utf8'))
  expect(generatorId).toBe(fixture.generator_id)
  expect(generatorSpec).toEqual(fixture.generator_spec)
  const digest = createHash('sha256')
  for (const runId of fixture.domain.run_ids) {
    for (let sequence = fixture.domain.first_sequence; sequence <= fixture.domain.last_sequence; sequence++) {
      const row = reading(runId, fixture.domain.worker, sequence)
      digest.update(canonicalBytes({ ...row, at: row.at.toISOString(), humidity: String(row.humidity) }))
    }
  }
  expect(digest.digest('hex')).toBe(fixture.domain.sha256)
})

const fleet = new Map([['a'.repeat(32), 7], ['0'.repeat(32), 3], ['c'.repeat(31) + '1', 12]])
it('expects every run row in the fleet window, in key order (brute force)', () => {
  const through = 5, limit = 4
  const rows = [...fleet].flatMap(([identity, written]) =>
    Array.from({ length: Math.min(through, written) }, (_, index) => reading(identity, 0, index + 1)))
  rows.sort((left, right) => left.station < right.station ? -1 : left.station > right.station ? 1
    : left.at.epochMicroseconds < right.at.epochMicroseconds ? -1 : 1)
  const [page, total, celsius] = fleetExpected(fleet, through, limit)
  expect(page).toEqual(rows.slice(0, limit))
  expect(total).toBe(rows.length); expect(total).toBe(3 + 5 + 5)
  const cents = rows.reduce((sum, row) => sum + BigInt(row.celsius.replace('.', '')), 0n)
  expect(celsius).toBe(`${cents / 100n}.${String(cents % 100n).padStart(2, '0')}`)
  expect(fleetExpected(fleet, through, 100)[0]).toEqual(rows)
})
for (const [through, limit, count] of [[150, 7, 10], [150, 100, 10], [64, 100, 0], [65, 1, 1]] as const) {
  it(`expects every alert of the run in key order, ${through} rows by ${limit} (brute force)`, () => {
    const rows = Array.from({ length: through }, (_, index) => reading('a'.repeat(32), 0, index + 1))
      .filter(row => row.humidity >= BigInt(alertHumidity))
    const [page, total, celsius] = alertExpected('a'.repeat(32), through, limit)
    expect(page).toEqual(rows.slice(0, limit))
    expect(total).toBe(rows.length); expect(total).toBe(count)
    const cents = rows.reduce((sum, row) => sum + BigInt(row.celsius.replace('.', '')), 0n)
    expect(celsius).toBe(rows.length ? `${cents / 100n}.${String(cents % 100n).padStart(2, '0')}` : null)
  })
}
/** An in-memory session answering reads as an engine does; `defect` makes one answer wrong. */
function engine(defect?: string) {
  const rows: Row[] = []
  type Bounds = { field: string; low?: bigint; high?: bigint }
  const station = (row: Row) => row.station as string
  const micros = (row: Row) => (row.at as { epochMicroseconds: bigint }).epochMicroseconds
  function matching(where: Row = {}, bounds?: Bounds, ranged = true) {
    return [...rows].sort((left, right) => station(left) < station(right) ? -1 : station(left) > station(right) ? 1
      : micros(left) < micros(right) ? -1 : 1)
      .filter(row => Object.entries(where).every(([name, value]) => row[name] === value))
      .filter(row => {
        if (!bounds || !ranged) return true
        const value = row[bounds.field] as bigint
        return (bounds.low === undefined || value >= bounds.low) && (bounds.high === undefined || value < bounds.high)
      })
  }
  vi.spyOn(Session, 'connect').mockImplementation(async () => ({
    async close() {},
    async measureStorage() { return { sizes: [], unavailable: {} } },
    async saveMany(_entity: string, batch: Row[]) { rows.push(...batch) },
    async get(_entity: string, key: Row) {
      return rows.find(row => row.station === key.station && isDeepStrictEqual(row.at, key.at)) ?? null
    },
    async scan(_entity: string, options: { where?: Row; bounds?: Bounds; limit: number }) {
      return { rows: matching(options.where, options.bounds, defect !== 'page').slice(0, options.limit), nextAfter: null }
    },
    async count(_entity: string, options: { where?: Row; bounds?: Bounds }) {
      return BigInt(matching(options.where, options.bounds, defect !== 'count').length)
    },
    async summarize(_entity: string, _field: string, options: { where?: Row; bounds?: Bounds }) {
      const found = matching(options.where, options.bounds)
      let cents = found.reduce((total, row) => total + BigInt(String(row.celsius).replace('.', '')), 0n)
      if (defect === 'summary' && found.length) cents += 1n
      const total = found.length || defect === 'empty_total'
        ? `${cents / 100n}.${String(cents % 100n).padStart(2, '0')}` : null
      return { count: BigInt(found.length), total }
    },
  }) as unknown as Session)
  return rows
}
for (const [defect, refusal] of [[undefined, undefined], ['page', 'alert page'], ['count', 'alert count'],
  ['summary', 'alert summary'], ['empty_total', 'alert summary']] as const) {
  it(`checks every alerts answer exactly (${defect ?? 'faithful engine'})`, async () => {
    // Two iterations of 40: none alerts in the first, five in the second.
    const { root } = fixture(), rows = engine(defect)
    const run = runWeather(root, { iterations: 2, batchSize: 40, intervalMs: 0, workload: 'alerts' })
    if (refusal === undefined) {
      const report = await run
      expect(report.status).toBe('complete'); expect(report.verified_rows).toBe(80); expect(rows).toHaveLength(80)
    } else {
      await expect(run).rejects.toThrow(refusal)
    }
  })
}
function report(root: string, identity: string, fields: Record<string, unknown> = {}) {
  write(join(root, 'runs', identity, 'report.json'), { protocol: 2, run_id: identity, project_id: 'p'.repeat(32),
    status: 'complete', pending: null, generator_id: generatorId, verified_rows: 4, ...fields })
}
it('reads every completed run of this project for the fleet, and only those', () => {
  const { root } = fixture()
  report(root, 'a'.repeat(32)); report(root, 'b'.repeat(32), { verified_rows: 9 })
  report(root, 'c'.repeat(32), { project_id: 'q'.repeat(32), status: 'running' })
  expect(fleetRuns(root, 'p'.repeat(32))).toEqual(new Map([['a'.repeat(32), 4], ['b'.repeat(32), 9]]))
  expect(fleetRuns(join(root, 'empty'), 'p'.repeat(32))).toEqual(new Map())
})
for (const [name, fields] of Object.entries({
  running: { status: 'running' }, incomplete: { status: 'incomplete' }, pending: { pending: { first: 1, count: 2 } },
  generator: { generator_id: 'weather-v0:other' }, bool: { verified_rows: true }, negative: { verified_rows: -1 },
  'too-many': { verified_rows: 10001 }, moved: { run_id: 'b'.repeat(32) }, 'no-pending': { pending: undefined },
})) it(`refuses the fleet when an earlier run is not complete (${name})`, () => {
  const { root } = fixture()
  report(root, 'a'.repeat(32), fields)
  expect(() => fleetRuns(root, 'p'.repeat(32))).toThrow(/every earlier run/)
})
it('offers the fleet and alerts workloads and refuses anything else before reading setup', async () => {
  expect(workloads).toEqual(['mixed', 'point', 'analytics', 'fleet', 'alerts'])
  await expect(runWeather('/nonexistent', { workload: 'bogus' as never })).rejects.toThrow(/Invalid bounded workload/)
})

it('skips a run that failed before its first write, and still refuses one that may have written', () => {
  // Found on the installed Weather acceptance of 26 September: a run without the `pg` package failed
  // before writing, and its `incomplete` report stopped every later fleet run in that directory.
  const beforeWriting = { status: 'incomplete', failure: 'EngineError', acknowledged_rows: 0,
    verified_after_uncertain_rows: 0, verified_rows: 0 }
  const { root } = fixture()
  report(root, 'a'.repeat(32)); report(root, 'b'.repeat(32), beforeWriting)
  expect(fleetRuns(root, 'p'.repeat(32))).toEqual(new Map([['a'.repeat(32), 4]]))
  for (const change of [{ acknowledged_rows: 10 }, { verified_after_uncertain_rows: 10 }, { verified_rows: 3 },
    { pending: { first: 1, count: 10 } }, { status: 'running' }, { acknowledged_rows: null },
    { generator_id: 'weather-v0:other' }]) {
    const { root: other } = fixture()
    report(other, 'a'.repeat(32), { ...beforeWriting, ...change })
    expect(() => fleetRuns(other, 'p'.repeat(32)), JSON.stringify(change)).toThrow(/every earlier run/)
  }
})
it('checks the pg package only for a PostgreSQL binding, and names the install when it is missing', async () => {
  const loaded: string[] = []
  await requireDrivers(['clickhouse'], async name => { loaded.push(name) })
  expect(loaded).toEqual([])
  await requireDrivers(['clickhouse', 'postgres'], async name => { loaded.push(name) })
  expect(loaded).toEqual(['pg'])
  await expect(requireDrivers(['postgres'], async () => { throw new Error("Cannot find package 'pg'") }))
    .rejects.toThrow("needs the 'pg' package: npm install pg")
})
it('refuses a run without the pg package before its report exists', async () => {
  const { root } = fixture()
  vi.doMock('pg', () => { throw new Error("Cannot find package 'pg'") })
  const connect = vi.spyOn(Session, 'connect')
  try {
    await expect(runWeather(root, { iterations: 1, batchSize: 1, intervalMs: 0 }))
      .rejects.toThrow("needs the 'pg' package")
  } finally { vi.doUnmock('pg') }
  expect(connect).not.toHaveBeenCalled()
  expect(() => readdirSync(join(root, 'runs'))).toThrow()
})

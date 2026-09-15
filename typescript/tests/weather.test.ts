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
import { reading, weatherModel } from '../src/demo/model.js'
import { project, read, write } from '../src/demo/project.js'
import { runWeather } from '../src/demo/weather.js'

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
  let saves = 0
  vi.spyOn(Session, 'connect').mockImplementation(async () => {
    const client = { closed: false,
      async close() { client.closed = true },
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
  return { rows, sessions, saves: () => saves }
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

/** Native TLS qualification, importing either the build or an installed npm artifact. */
import { readFileSync } from 'node:fs'
import { randomUUID } from 'node:crypto'
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'
import { resolve } from 'node:path'

const packageRoot = process.env.SDE_TLS_PACKAGE_ROOT ?? resolve('dist')
const { PostgresEngine } = await import(pathToFileURL(resolve(packageRoot, 'engines/postgres.js')).href)
const { ClickHouseEngine } = await import(pathToFileURL(resolve(packageRoot, 'engines/clickhouse.js')).href)
const { Timestamp } = await import(pathToFileURL(resolve(packageRoot, 'timestamp.js')).href)
const config = JSON.parse(readFileSync(process.env.SDE_TLS_TEST ?? '', 'utf8'))
const table = 'tls_ts_' + randomUUID().replaceAll('-', '')
const row = { id: 'synthetic', big: 9007199254740993n, at: Timestamp.from('2026-09-15T10:00:00.123456Z'), label: 'Zażółć gęślą jaźń', money: '12.34' }
for (const [dialect, scheme, secureOption] of [['postgres', 'postgresql', false], ['clickhouse', 'https', false], ['clickhouse', 'clickhouse', true]]) {
  /** @param {string} ca */
  const dsn = (ca) => {
    const query = new URLSearchParams(dialect === 'postgres' ? { sslmode: 'verify-full', sslrootcert: ca } : { ca_cert: ca })
    if (secureOption) query.set('secure', 'true')
    const user = dialect === 'postgres' ? 'postgres' : 'default'
    return `${scheme}://${user}:${config.password}@127.0.0.1:${config[dialect + '_port']}/sde?${query}`
  }
  const Adapter = dialect === 'postgres' ? PostgresEngine : ClickHouseEngine
  const engine = new Adapter(dsn(config.material.ca))
  const currentTable = table + (secureOption ? '_secure' : '')
  try {
    await engine.connect()
    await engine.ensureSchema({ tables: { A: currentTable }, columns: { A: dialect === 'postgres' ? { id: 'text', big: 'bigint', at: 'timestamptz', label: 'text', money: 'numeric(12,2)' } : { id: 'String', big: 'Int64', at: "DateTime64(6, 'UTC')", label: 'String', money: 'Decimal(12, 2)' } }, indexes: [], partitionBy: {} }, { keys: { A: ['id'] } })
    await engine.insert(currentTable, row)
    await engine.insertMany(currentTable, [{ ...row, id: 'two' }, { ...row, id: 'three' }])
    for (const id of ['synthetic', 'two', 'three']) {
      assert.deepEqual(await engine.get(currentTable, { id }), { ...row, id })
    }
    assert.equal(await engine.count(currentTable), 3)
    console.log(JSON.stringify({ language: 'typescript', dialect, scheme, secure_option: secureOption, rows: 3, exact_values: true }))
  } finally { await engine.close() }
  const bad = new Adapter(dsn(config.material.other_ca))
  try {
    await assert.rejects(bad.connect())
    console.log(JSON.stringify({ language: 'typescript', dialect, scheme, wrong_ca_refused: true }))
  } finally { await bad.close() }
}

/** Driver-level outcomes cannot be made certain by attempting a rollback after a lost response. */
import { expect, it } from 'vitest'
import { PostgresEngine } from '../src/engines/postgres.js'

function driver(options: { connect?: boolean; commit?: boolean; rollback?: boolean; end?: boolean }) {
  const calls: string[] = []
  class Client {
    setTypeParser(): void {}
    on(): void {}
    async connect(): Promise<void> { calls.push('connect'); if (options.connect) throw new Error('connect failed') }
    async end(): Promise<void> { calls.push('end'); if (options.end) throw new Error('end failed') }
    async query(sql: string) {
      calls.push(sql)
      if (sql === 'COMMIT' && options.commit) throw new Error('commit response lost')
      if (sql === 'ROLLBACK' && options.rollback) throw new Error('rollback failed')
      return { rows: [], fields: [], rowCount: 0 }
    }
  }
  return { calls, Client }
}

it('a failed connect closes the client created before it rejected', async () => {
  const chosen = driver({ connect: true })
  const engine = new PostgresEngine('postgresql://localhost/db', { driver: chosen })
  await expect(engine.connect()).rejects.toThrow('connect failed')
  expect(chosen.calls).toEqual(['connect', 'end'])
})

it('an uncertain commit refuses further operations until explicit reconnect', async () => {
  const options = { commit: true }, chosen = driver(options)
  const engine = new PostgresEngine('postgresql://localhost/db', { driver: chosen })
  await engine.connect()
  await expect(engine.transaction(async () => {})).rejects.toThrow('uncertain')
  const before = chosen.calls.length
  await expect(engine.insert('events', { id: 1n })).rejects.toThrow('close() then connect()')
  await expect(engine.connect()).rejects.toThrow('close() then connect()')
  expect(chosen.calls.length).toBe(before)
  options.commit = false
  await engine.close(); await engine.connect()
  await engine.transaction(async () => {})
  await engine.close()
})

it('rollback failure preserves the body error and rejects connection reuse', async () => {
  const chosen = driver({ rollback: true })
  const engine = new PostgresEngine('postgresql://localhost/db', { driver: chosen })
  await engine.connect()
  const original = new Error('body failure')
  await expect(engine.transaction(async () => { throw original })).rejects.toBe(original)
  const before = chosen.calls.length
  await expect(engine.get('events', { id: 1n })).rejects.toThrow('close() then connect()')
  expect(chosen.calls.length).toBe(before)
  await engine.close()
})

it('a failed close keeps its client so cleanup can be retried', async () => {
  const options = { end: true }, chosen = driver(options)
  const engine = new PostgresEngine('postgresql://localhost/db', { driver: chosen })
  await engine.connect()
  await expect(engine.close()).rejects.toThrow('end failed')
  options.end = false
  await engine.close()
  expect(chosen.calls.filter(call => call === 'end')).toHaveLength(2)
})

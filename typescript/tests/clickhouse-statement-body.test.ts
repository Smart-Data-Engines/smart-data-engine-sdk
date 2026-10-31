/**
 * A ClickHouse statement travels as the request's body, never in its URL.
 *
 * A URL is what a proxy or a load balancer between the application and the server logs, and the
 * statement of a read holds the values it filters on. The reference's driver sends a statement as
 * the body. An INSERT's rows are its body, so its statement, which names the table and the columns
 * and holds no value, is the one that travels in the URL.
 */
import { createServer } from 'node:http'
import type { AddressInfo } from 'node:net'
import { afterEach, expect, it } from 'vitest'
import { ClickHouseEngine } from '../src/engines/clickhouse.js'
import { planRead } from '../src/query.js'

const CANARY = 'canary-value-7c1e'
const PASSWORD = 'fixture-secret'
const COLUMNS = [{ name: 'id', type: 'string' }, { name: 'label', type: 'string' }]
const INSERT = 'INSERT INTO `events` (`id`, `label`) FORMAT JSONEachRow'

interface Exchange { readonly method: string; readonly url: string; readonly body: string }

const closers: Array<() => Promise<void>> = []
afterEach(async () => { while (closers.length) await closers.pop()!() })

/** What ClickHouse would answer, judged by the statement as the server reassembles it. */
function answer(statement: string): string {
  if (statement.startsWith('SELECT version()')) {
    return JSON.stringify({ meta: [{ name: 'version()', type: 'String' }], data: [{ 'version()': '24.8.14.39' }] })
  }
  if (statement.startsWith('INSERT')) return ''
  if (statement.includes(' AS sde_count')) {
    return JSON.stringify({ meta: [{ name: 'sde_count', type: 'String' }], data: [{ sde_count: '1' }] })
  }
  return JSON.stringify({
    meta: [{ name: 'id', type: 'String' }, { name: 'label', type: 'String' }],
    data: [{ id: 'k-1', label: CANARY }],
  })
}

async function recorded(): Promise<{ engine: ClickHouseEngine, exchanges: Exchange[] }> {
  const exchanges: Exchange[] = []
  const server = createServer((request, response) => {
    const chunks: Buffer[] = []
    request.on('data', (chunk: Buffer) => chunks.push(chunk))
    request.on('end', () => {
      const body = Buffer.concat(chunks).toString('utf8')
      exchanges.push({ method: request.method!, url: request.url!, body })
      // The server reads a statement from the URL's `query` and continues it with the body.
      const start = new URL(request.url!, 'http://fixture.invalid').searchParams.get('query')
      response.writeHead(200, { 'Content-Type': 'application/json', Connection: 'close' })
      response.end(answer(start === null ? body : start + '\n' + body))
    })
  })
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  closers.push(async () => {
    server.closeAllConnections()
    await new Promise<void>(resolve => server.close(() => resolve()))
  })
  const port = (server.address() as AddressInfo).port
  return { engine: new ClickHouseEngine(`http://fixture:${PASSWORD}@127.0.0.1:${port}/db`), exchanges }
}

it('sends a statement as the body, and names only an INSERT, which holds no value, in the URL', async () => {
  const { engine, exchanges } = await recorded()
  await engine.connect()
  try {
    expect(await engine.get('events', { label: CANARY })).toEqual({ id: 'k-1', label: CANARY })
    const plan = planRead(COLUMNS, ['id'], { where: { label: CANARY } })
    expect(await engine.selectRows('events', plan)).toEqual([{ id: 'k-1', label: CANARY }])
    expect(await engine.countRows('events', plan)).toBe(1n)
    await engine.insert('events', { id: 'k-2', label: CANARY })
    await engine.insertMany('events', [{ id: 'k-3', label: CANARY }, { id: 'k-4', label: 'other' }])
  } finally {
    await engine.close()
  }

  expect(exchanges.map(exchange => exchange.method)).toEqual(Array(6).fill('POST'))
  for (const exchange of exchanges) {
    // Decoded too: a value a proxy logs percent-encoded is the same value.
    for (const url of [exchange.url, decodeURIComponent(exchange.url)]) {
      expect(url).not.toContain(CANARY)
      expect(url).not.toContain(PASSWORD)
    }
  }
  expect(exchanges.map(exchange => new URL(exchange.url, 'http://fixture.invalid').searchParams.get('query')))
    .toEqual([null, null, null, null, INSERT, INSERT])
  expect(exchanges[0]!.body).toBe('SELECT version() FORMAT JSON')
  expect(exchanges[1]!.body).toBe("SELECT * FROM `events` FINAL WHERE `label` = 'canary-value-7c1e' LIMIT 1 FORMAT JSON")
  // The control: the value this test looks for did travel, in the bodies.
  for (const exchange of exchanges.slice(1)) expect(exchange.body).toContain(CANARY)
  expect(exchanges[5]!.body.split('\n').map(line => JSON.parse(line) as unknown))
    .toEqual([{ id: 'k-3', label: CANARY }, { id: 'k-4', label: 'other' }])
})

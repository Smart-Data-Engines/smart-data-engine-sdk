/**
 * Every ClickHouse statement says that a limit is an error and that no limit or filter of the
 * login's profile applies.
 *
 * ClickHouse ends an answer early without an error under an overflow mode of `break`, and a
 * profile's `limit`, `offset` and `additional_result_filter` shorten one the same way. The library
 * takes an answer without an error for the whole answer, so it says otherwise with every request.
 * Against a server that records what it is sent; `clickhouse-whole-answers.live.test.ts` holds what
 * a real one then does.
 */
import { createServer } from 'node:http'
import type { AddressInfo } from 'node:net'
import { afterEach, expect, it } from 'vitest'
import { ClickHouseEngine, WHOLE_ANSWERS } from '../src/engines/clickhouse.js'
import { EngineError } from '../src/errors.js'

const closers: Array<() => Promise<void>> = []
afterEach(async () => { while (closers.length) await closers.pop()!() })

interface Fixture { readonly engine: ClickHouseEngine, readonly urls: string[] }

/** A server that answers each statement by `answer`, and keeps the URL of every request. */
async function server(answer: (statement: string) => { status?: number, body: string }): Promise<Fixture> {
  const urls: string[] = []
  const listening = createServer((request, response) => {
    const chunks: Buffer[] = []
    request.on('data', (chunk: Buffer) => chunks.push(chunk))
    request.on('end', () => {
      urls.push(request.url!)
      const start = new URL(request.url!, 'http://fixture.invalid').searchParams.get('query')
      const body = Buffer.concat(chunks).toString('utf8')
      const { status = 200, body: text } = answer(start === null ? body : start + '\n' + body)
      response.writeHead(status, { 'Content-Type': 'application/json', Connection: 'close' })
      response.end(text)
    })
  })
  await new Promise<void>(resolve => listening.listen(0, '127.0.0.1', resolve))
  closers.push(async () => {
    listening.closeAllConnections()
    await new Promise<void>(resolve => listening.close(() => resolve()))
  })
  const port = (listening.address() as AddressInfo).port
  return { engine: new ClickHouseEngine(`http://fixture:secret@127.0.0.1:${port}/db`), urls }
}

const json = (meta: string, data: unknown[]): string =>
  JSON.stringify({ meta: [{ name: meta, type: 'String' }], data })

function healthy(counted: unknown[]): (statement: string) => { body: string } {
  return (statement) => {
    if (statement.startsWith('SELECT version()')) return { body: json('version()', [{ 'version()': '24.8.14.39' }]) }
    if (statement.includes('count() AS n')) return { body: json('n', counted) }
    if (statement.startsWith('INSERT')) return { body: '' }
    return { body: json('id', [{ id: 'k-1' }]) }
  }
}

it('is what was measured: eleven overflow modes, and four settings that shorten an answer without one', () => {
  expect(Object.fromEntries(WHOLE_ANSWERS)).toEqual({
    distinct_overflow_mode: 'throw',
    group_by_overflow_mode: 'throw',
    join_overflow_mode: 'throw',
    read_overflow_mode: 'throw',
    read_overflow_mode_leaf: 'throw',
    result_overflow_mode: 'throw',
    set_overflow_mode: 'throw',
    sort_overflow_mode: 'throw',
    timeout_overflow_mode: 'throw',
    timeout_overflow_mode_leaf: 'throw',
    transfer_overflow_mode: 'throw',
    limit: '0',
    offset: '0',
    additional_table_filters: '{}',
    additional_result_filter: '',
  })
  expect(new Set(WHOLE_ANSWERS.map(([name]) => name)).size).toBe(WHOLE_ANSWERS.length)
})

it('travels with every request: the handshake, a read, a count and a write', async () => {
  const { engine, urls } = await server(healthy([{ n: '2' }]))
  await engine.connect()
  try {
    await engine.get('events', { id: 'k-1' })
    expect(await engine.count('events')).toBe(2)
    await engine.insert('events', { id: 'k-2' })
  } finally {
    await engine.close()
  }
  expect(urls).toHaveLength(4)
  for (const url of urls) {
    const sent = new URL(url, 'http://fixture.invalid').searchParams
    for (const [name, value] of WHOLE_ANSWERS) expect(sent.getAll(name), `${name} in ${url}`).toEqual([value])
  }
})

it('takes a count answered no row at all for an error, and an empty table for zero', async () => {
  const none = await server(healthy([]))
  await none.engine.connect()
  await expect(none.engine.count('events')).rejects.toThrow(/count on events failed: it was answered no row at all/)
  const empty = await server(healthy([{ n: '0' }]))
  await empty.engine.connect()
  expect(await empty.engine.count('events')).toBe(0)
})

it.each([
  ["Code: 164. DB::Exception: Cannot modify 'result_overflow_mode' setting in readonly mode. (READONLY) (version 24.8.14.39 (official build))",
    'result_overflow_mode'],
  ['Code: 452. DB::Exception: Setting limit should not be changed. (SETTING_CONSTRAINT_VIOLATION) (version 26.9.27.3 (official build))',
    'limit'],
])('names the setting a login may not send, and none of the server\'s text: %s', async (text, name) => {
  const { engine } = await server(() => ({ status: 500, body: text }))
  const refused = await engine.connect().then(() => undefined, (error: unknown) => error)
  expect(refused).toBeInstanceOf(EngineError)
  const said = (refused as Error).message
  expect(said).toContain(`this ClickHouse login's ${name} is not `)
  expect(said).toContain('may not change it')
  expect(said).not.toContain('DB::Exception')
})

it('leaves every other refusal of a connection as the server said it', async () => {
  // The controls: a setting this adapter does not send, and a wrong password.
  for (const text of [
    "Code: 164. DB::Exception: Cannot modify 'max_threads' setting in readonly mode. (READONLY)",
    'Code: 516. DB::Exception: fixture: Authentication failed. (AUTHENTICATION_FAILED)',
  ]) {
    const { engine } = await server(() => ({ status: 500, body: text }))
    await expect(engine.connect()).rejects.toThrow(/could not connect to ClickHouse: /)
  }
})

const UNFINISHED = 'Code: 396. DB::Exception: Limit for result exceeded, max rows: 5.00 thousand, ' +
  'current rows: 5.10 thousand. (TOO_MANY_ROWS_OR_BYTES) (version 24.8.14.39 (official build))'

it.each([
  ['a document with what it had and an exception beside it',
    JSON.stringify({ meta: [{ name: 'id', type: 'String' }], data: [{ id: 'k-1' }], rows: 1, exception: UNFINISHED })],
  ['an unfinished document and the exception after it as text',
    `{"meta":[{"name":"id","type":"String"}],"data":[{"id":"k-1"},\n${UNFINISHED}\n`],
])('takes an answer the server ended with an error, under HTTP 200, for that error: %s', async (_label, body) => {
  // The status was sent before the server knew: what it had written so far is the first rows of
  // the table, and read for its rows alone it was returned as the table.
  const { engine } = await server((statement) =>
    statement.startsWith('SELECT version()') ? healthy([])(statement) : { status: 200, body })
  await engine.connect()
  for (const read of [
    () => engine.keyRange('events', ['id']),
    () => engine.get('events', { id: 'k-1' }),
    () => engine.count('events'),
  ]) {
    const refused = await read().then(() => undefined, (error: unknown) => error)
    expect(refused).toBeInstanceOf(EngineError)
    expect((refused as Error).message).toContain('ClickHouse ended its answer with an error: Code: 396.')
    expect((refused as Error).message).toContain('Limit for result exceeded')
  }
})

it('still says where an answer that is not JSON stopped parsing, when the server said nothing', async () => {
  const { engine } = await server((statement) =>
    statement.startsWith('SELECT version()') ? healthy([])(statement) : { body: '{"meta":[],"data":[{"id":"k-1"},' })
  await engine.connect()
  await expect(engine.keyRange('events', ['id'])).rejects.toThrow(/JSON/)
})

it('takes a body the server cut after it had begun for a failure, never for the rows it holds', async () => {
  // What 26.9 does with an error raised in the middle of an answer: the status and the first rows
  // are on their way, and the body ends without its last chunk.
  const listening = createServer((request, response) => {
    request.resume()
    request.on('end', () => {
      const statement = new URL(request.url!, 'http://fixture.invalid').searchParams.get('query')
      if (statement === null && request.headers['content-length'] === '28') {
        response.writeHead(200, { 'Content-Type': 'application/json', Connection: 'close' })
        response.end(json('version()', [{ 'version()': '26.9.27.3' }]))
        return
      }
      response.writeHead(200, { 'Content-Type': 'application/json', 'Transfer-Encoding': 'chunked' })
      response.write('{"meta":[{"name":"id","type":"String"}],"data":[{"id":"k-1"},{"id":"k-2"}')
      setTimeout(() => response.socket?.destroy(), 20)
    })
  })
  await new Promise<void>(resolve => listening.listen(0, '127.0.0.1', resolve))
  closers.push(async () => {
    listening.closeAllConnections()
    await new Promise<void>(resolve => listening.close(() => resolve()))
  })
  const port = (listening.address() as AddressInfo).port
  const engine = new ClickHouseEngine(`http://fixture:secret@127.0.0.1:${port}/db`)
  await engine.connect()
  await expect(engine.keyRange('events', ['id'])).rejects.toBeInstanceOf(EngineError)
})

/**
 * The account-free mode against live engines (requirement 12.6): from the moment the library is
 * imported through the full operation set on two engines, every connection it attempts is to one of
 * the client's own engines. The reference's `test_no_account_live.py` holds the same claim with an
 * interpreter audit hook; `boundary.test.ts` is the static half here, over the import closure.
 *
 * **The instrument has to be shown to see a connection.** An empty list is what a blind instrument
 * produces, so the claim is not "nothing was attempted" but "the list holds the engines, and only
 * them", with a connection this test makes itself as the control. The instrument wraps
 * `net.Socket.prototype.connect`, which every TCP and TLS client in Node goes through - the
 * PostgreSQL driver's socket and the ClickHouse adapter's HTTP agent alike - and it is installed
 * before the library is imported, so a library that called home on import would be seen too.
 */

import net from 'node:net'

import { expect, it } from 'vitest'

const PG = process.env['SDE_POSTGRES_DSN']
const CH = process.env['SDE_CLICKHOUSE_DSN']

/** `host:port` of an engine's DSN, as the socket is asked to connect to it. */
function endpoint(dsn: string, defaultPort: number): string {
  const url = new URL(dsn.replace(/^clickhouse:/, 'http:'))
  return `${url.hostname}:${url.port === '' ? defaultPort : Number(url.port)}`
}

/**
 * What one `Socket.connect` call asks for: `host:port`, or the path of a pipe. `net.connect` hands
 * the socket its arguments already normalized, as one array of options and callback.
 */
function target(args: unknown[]): string {
  const [first, second] = Array.isArray(args[0]) ? (args[0] as unknown[]) : args
  if (typeof first === 'object' && first !== null) {
    const options = first as { host?: string; port?: number | string; path?: string | null }
    // An HTTP agent hands a TCP connection `path: null`; only a path given is a pipe.
    if (options.path) return `pipe:${options.path}`
    return `${options.host ?? 'localhost'}:${Number(options.port)}`
  }
  if (typeof first === 'string' && Number.isNaN(Number(first))) return `pipe:${first}`
  return `${typeof second === 'string' ? second : 'localhost'}:${Number(first)}`
}

it.skipIf(!PG || !CH)('attempts no connection to anything but the engines it was given', async () => {
  const attempted: string[] = []
  const original = net.Socket.prototype.connect
  net.Socket.prototype.connect = function (this: net.Socket, ...args: unknown[]) {
    attempted.push(target(args))
    return (original as (...a: unknown[]) => net.Socket).apply(this, args)
  } as typeof net.Socket.prototype.connect
  const tables = { pg: `no_account_${process.pid}`, ch: `no_account_${process.pid}` }
  try {
    // The control: a connection the test makes itself has to be seen, or the list proves nothing.
    const control = net.connect(Number(new URL(PG!).port), new URL(PG!).hostname)
    await new Promise<void>((resolve, reject) => control.once('connect', resolve).once('error', reject))
    control.destroy()
    expect(attempted).toEqual([endpoint(PG!, 5432)])
    attempted.length = 0

    // Imported only now, under the instrument: a library that called home on import would be seen.
    const { buildModel, entity, loadMap, Session, T } = await import('../src/index.js')
    const { PostgresEngine } = await import('../src/engines/postgres.js')
    const { ClickHouseEngine } = await import('../src/engines/clickhouse.js')
    expect(attempted, 'importing the library connected to something').toEqual([])

    const model = buildModel([
      entity('Reading', { fields: { id: T.int64, station: T.string }, key: ['id'] }),
      entity('Tick', { fields: { id: T.int64, venue: T.string }, key: ['id'] }),
    ])
    // A map written by hand and unsigned: no key, no account, nothing that came from us.
    const placement = loadMap(
      {
        contract: 2,
        model_version: model.version,
        map_version: 1,
        groups: {
          Reading: {
            source: {
              id: 'source@pg', engine: 'pg',
              layout: { tables: { Reading: tables.pg }, columns: { Reading: { id: 'bigint', station: 'text' } } },
            },
          },
          Tick: {
            source: {
              id: 'source@ch', engine: 'ch',
              layout: { tables: { Tick: tables.ch }, columns: { Tick: { id: 'Int64', venue: 'String' } } },
            },
          },
        },
        routing: {},
      },
      { model },
    )
    const pg = new PostgresEngine(PG!)
    const ch = new ClickHouseEngine(CH!)
    await pg.connect()
    await ch.connect()
    try {
      const session = await Session.open(model, placement, { pg, ch })
      expect(session.rollbackProtection.protection).toBe('not_applicable')
      await session.ensureSchema()
      await session.save('Reading', { id: 1n, station: 'WAW' })
      await session.saveMany('Tick', [{ id: 1n, venue: 'XWAR' }, { id: 2n, venue: 'XNYS' }])
      expect(await session.get('Reading', { id: 1n })).toEqual({ id: 1n, station: 'WAW' })
      expect(await session.get('Tick', { id: 2n })).toEqual({ id: 2n, venue: 'XNYS' })
      expect(await session.count('Tick')).toBe(2n)
    } finally {
      await pg.close()
      await ch.close()
    }
  } finally {
    net.Socket.prototype.connect = original
    await drop(tables)
  }
  // Every connection the library attempted is to one of the two engines, and it reached both.
  const engines = new Set([endpoint(PG!, 5432), endpoint(CH!, 8123)])
  expect(attempted.filter((where) => !engines.has(where))).toEqual([])
  expect(new Set(attempted)).toEqual(engines)
}, 60000)

/** The tables this test made, dropped after the instrument is gone. */
async function drop(tables: { pg: string; ch: string }): Promise<void> {
  const { Client } = await import('pg')
  const admin = new Client({ connectionString: PG })
  await admin.connect()
  try {
    await admin.query(`DROP TABLE IF EXISTS "${tables.pg}" CASCADE`)
  } finally {
    await admin.end()
  }
  const url = new URL(CH!.replace(/^clickhouse:/, 'http:'))
  const database = url.pathname.replace(/^\//, '') || 'default'
  const auth = Buffer.from(`${decodeURIComponent(url.username)}:${decodeURIComponent(url.password)}`).toString('base64')
  await fetch(`http://${url.host}/`, {
    method: 'POST',
    headers: { Authorization: `Basic ${auth}` },
    body: `DROP TABLE IF EXISTS ${database}.${tables.ch}`,
  })
}

/**
 * Plain transport to a host other than the loopback only when the connection string says so
 * (`specs/secure-connections`), case for case as the reference's `test_plaintext_only_when_said.py`.
 * Every case is decided on the text, before anything connects: adapters are constructed, never
 * connected.
 */

import { describe, expect, it } from 'vitest'

import { EngineError } from '../src/errors.js'
import { parseDsn as clickhouseDsn } from '../src/engines/_clickhouse-connection.js'
import { PostgresEngine, refusePlaintextElsewhere } from '../src/engines/postgres.js'
import { loopback } from '../src/engines/_transport.js'
import { OrderbookEngine } from '../src/engines/orderbook.js'
import { parseOrderbookDsn } from '../src/engines/_orderbook-dsn.js'

describe('the loopback, decided on the text', () => {
  it.each([
    ['localhost', true], ['LOCALHOST', true], ['127.0.0.1', true], ['127.8.9.10', true],
    ['::1', true], ['[::1]', true], ['0:0:0:0:0:0:0:1', true], ['::ffff:127.0.0.1', true],
    ['::ffff:7f00:1', true], ['::ffff:10.0.0.5', false], ['db.example.com', false],
    ['10.0.0.5', false], ['localhost.example.com', false], ['0.0.0.0', false],
    ['0:0:0:0:0:ffff:127.0.0.1', true], ['::FFFF:127.1.2.3', true], ['::127.0.0.1', false],
    ['0000:0000:0000:0000:0000:0000:0000:0001', true], ['::1%lo', false], ['[::1%lo]', false],
  ])('%s is %s', (host, expected) => {
    expect(loopback(host)).toBe(expected)
  })
})

describe('PostgreSQL', () => {
  const empty: NodeJS.ProcessEnv = {}

  it.each([
    'postgresql://app:secret@db.example.com:5432/sde',
    'postgres://app:secret@10.0.0.5/sde',
    'postgresql://app:secret@127.0.0.1/sde?host=db.example.com',
  ])('refuses %s with no TLS policy, and does not echo the secret', (dsn) => {
    let error: unknown
    try {
      refusePlaintextElsewhere(dsn, empty)
    } catch (caught) {
      error = caught
    }
    expect(error).toBeInstanceOf(EngineError)
    expect(String(error)).toContain('sslmode=verify-full')
    expect(String(error)).not.toContain('secret')
  })

  it.each([
    'postgresql://app:secret@127.0.0.1:55432/sde',
    'postgresql://app:secret@localhost/sde',
    'postgresql://app:secret@[::1]/sde',
    'postgresql://app@/sde?host=/var/run/postgresql',
    '/var/run/postgresql sde',
    'postgresql://app:secret@db.example.com/sde?sslmode=verify-full',
    'postgresql://app:secret@db.example.com/sde?sslmode=disable',
    'postgresql://app:secret@db.example.com/sde?ssl=true',
  ])('accepts %s', (dsn) => {
    expect(() => refusePlaintextElsewhere(dsn, empty)).not.toThrow()
  })

  it.skipIf(Boolean(process.env['PGSSLMODE']))('is the adapter constructor\'s rule, not only a helper\'s', () => {
    expect(() => new PostgresEngine('postgresql://app:secret@db.example.com/sde')).toThrow(/TLS policy/)
    expect(() => new PostgresEngine('postgresql://app:secret@127.0.0.1/sde')).not.toThrow()
  })

  it('reads the environment as pg does', () => {
    expect(() => refusePlaintextElsewhere('postgresql:///sde', { PGHOST: 'db.example.com' })).toThrow(/TLS policy/)
    expect(() => refusePlaintextElsewhere('postgresql:///sde', { PGHOST: 'db.example.com', PGSSLMODE: 'verify-full' })).not.toThrow()
  })
})

describe('ClickHouse', () => {
  it.each([
    'clickhouse://app:secret@ch.example.com:8123/sde',
    'http://app:secret@ch.example.com/sde',
    'clickhouse://app:secret@10.0.0.5/sde',
  ])('refuses plain HTTP to %s unless secure=false is stated', (dsn) => {
    expect(() => clickhouseDsn(dsn)).toThrow(/explicit secure=false/)
    expect(() => clickhouseDsn(dsn)).not.toThrow(/secret/)
    expect(clickhouseDsn(`${dsn}?secure=false`).secure).toBe(false)
  })

  it.each([
    'clickhouse://default:sde@127.0.0.1:58123/sde',
    'http://default@localhost:8123/sde',
    'clickhouse://default@[::1]:8123/sde',
    'https://app:secret@ch.example.com/sde',
    'clickhouses://app:secret@ch.example.com/sde',
    'clickhouse://app:secret@ch.example.com/sde?secure=true',
  ])('accepts %s', (dsn) => {
    expect(() => clickhouseDsn(dsn)).not.toThrow()
  })
})

describe('the orderbook engine, whose rule is its adapter\'s and not its grammar\'s', () => {
  it.each([
    'orderbook://ob.example.com:9090',
    'orderbook://app:secret@10.0.0.5:9090',
    'orderbook://[2001:db8::1]:9090?timeout=5',
  ])('refuses %s, which states no TLS', (dsn) => {
    let message = ''
    try {
      OrderbookEngine.fromDsn(dsn)
    } catch (error) {
      message = (error as Error).message
    }
    expect(message).toContain('needs its TLS stated')
    expect(message).not.toContain('secret')
  })

  it.each([
    'orderbook://127.0.0.1:9090',
    'orderbook://localhost:9090',
    'orderbook://[::1]:9090',
    'orderbook://ob.example.com:9090?tls=on',
    'orderbook://app:secret@ob.example.com:9090?tls=off',
  ])('accepts %s', (dsn) => {
    expect(() => OrderbookEngine.fromDsn(dsn)).not.toThrow()
  })

  it('holds fields to the same rule: tls left out is the unstated default', () => {
    expect(() => new OrderbookEngine({ host: 'ob.example.com', port: 9090 })).toThrow('needs its TLS stated')
    expect(() => new OrderbookEngine({ host: 'ob.example.com', port: 9090, tls: false })).not.toThrow()
    expect(() => new OrderbookEngine({ host: '127.0.0.1', port: 9090 })).not.toThrow()
  })

  it('leaves the grammar reading what a DSN says', () => {
    expect(parseOrderbookDsn('orderbook://ob.example.com:9090')).toMatchObject({ tls: false, tlsGiven: false })
    expect(parseOrderbookDsn('orderbook://ob.example.com:9090?tls=off').tlsGiven).toBe(true)
  })
})

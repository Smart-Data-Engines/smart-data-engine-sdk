/**
 * What each adapter does when the engine is not there, measured rather than described.
 *
 * `docs/failure-semantics.md` is a public document a client reads before buying, and requirement
 * 6.6 asks for it before the sale rather than after the first incident. Every row of it is either
 * measured by a test like this one or paired with a mechanism by one - a document whose evidence
 * skipped is a document with no evidence.
 *
 * Two of the three connection failures in that document were **defects** when it was written, in
 * the reference implementation: no adapter bounded opening a connection, and which layer bounds it
 * turned out to depend on what succeeded. Measured against a socket that accepts TCP and then says
 * nothing, the call did not return within 45 seconds - inside a caller's request path, with nothing
 * to time out. This file is the same measurement in this language, where the bounds are the
 * adapters' own rather than a driver's.
 *
 * **Nothing here needs a server**, which is why the file is not a live slice: every socket in
 * it is one this file starts. That is a property worth having - the bound a client depends on
 * is measured on every machine that runs the suite, not only on one with two databases.
 *
 * **Every test that waits on a silent socket has a deadline of its own.** A test that loses the
 * thing it guards must fail rather than hang: without one, removing the bound turns a red suite
 * into a suite that never finishes, and CI reports that as a timeout on the job rather than as this
 * assertion.
 */

import { createServer } from 'node:net'
import type { AddressInfo, Server, Socket } from 'node:net'

import { afterEach, describe, expect, it } from 'vitest'

import {
  CONNECT_TIMEOUT_MS as CH_CONNECT_MS,
  ClickHouseEngine,
  HANDSHAKE_TIMEOUT_MS,
} from '../src/engines/clickhouse.js'
import { CONNECT_TIMEOUT_MS as PG_CONNECT_MS, PostgresEngine } from '../src/engines/postgres.js'



let listening: Server | null = null

afterEach(async () => {
  if (listening === null) return
  const server = listening
  listening = null
  server.emit('close')
  await new Promise<void>((resolve) => server.close(() => resolve()))
})

/** A port nothing is listening on. Bound and released, so it is free and was not guessed. */
async function closedPort(): Promise<number> {
  const server = createServer()
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  const port = (server.address() as AddressInfo).port
  await new Promise<void>((resolve) => server.close(() => resolve()))
  return port
}

/**
 * A socket that accepts the connection and then says nothing.
 *
 * The case that matters, and the one a connect timeout does not cover: the TCP handshake succeeds,
 * so a driver bounding *connect* has already finished its job. What is left is the wait for an
 * answer that never comes - a firewall that accepts, a load balancer with no healthy backend, a
 * server mid-restart.
 */
async function silentPort(): Promise<number> {
  const open: Socket[] = []
  const server = createServer((socket) => {
    // Held open deliberately. Closing it would produce a different failure - one the adapters
    // already report correctly - and this test would then be measuring the wrong thing.
    open.push(socket)
    socket.on('error', () => undefined)
  })
  // Destroyed on the way out, and that is not tidiness: `server.close()` waits for open
  // connections, and this server's whole purpose is to hold one open. The first version left the
  // teardown hook to time out, which reads as four failing tests rather than as one unclosed
  // socket.
  server.on('close', () => {
    for (const socket of open) socket.destroy()
  })
  listening = server
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  return (server.address() as AddressInfo).port
}

/** Run something with a hard deadline, so a lost bound is a failure rather than a hang. */
async function within<T>(ms: number, what: string, body: () => Promise<T>): Promise<T> {
  let timer: NodeJS.Timeout | undefined
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(
      () =>
        reject(
          new Error(
            `${what} did not finish within ${ms} ms. The bound this test exists for is gone, and ` +
              'without this deadline the suite would hang instead of failing.',
          ),
        ),
      ms,
    )
  })
  try {
    return await Promise.race([body(), deadline])
  } finally {
    if (timer !== undefined) clearTimeout(timer)
  }
}

describe('a port with nothing on it', () => {
  it('is reported by the PostgreSQL adapter, not waited on', async () => {
    const port = await closedPort()
    const engine = new PostgresEngine(`postgresql://postgres:sde@127.0.0.1:${port}/sde`)
    await within(5_000, 'connecting to a closed port', async () => {
      await expect(engine.connect()).rejects.toThrow('could not connect to PostgreSQL')
    })
  })

  it('is reported by the ClickHouse adapter, not waited on', async () => {
    const port = await closedPort()
    const engine = new ClickHouseEngine(`clickhouse://default:sde@127.0.0.1:${port}/sde`)
    await within(5_000, 'connecting to a closed port', async () => {
      await expect(engine.connect()).rejects.toThrow('could not connect to ClickHouse')
    })
  })
})

describe('a socket that accepts and says nothing', () => {
  it('bounds the PostgreSQL adapter, and the bound is the one it publishes', async () => {
    const port = await silentPort()
    const engine = new PostgresEngine(`postgresql://postgres:sde@127.0.0.1:${port}/sde`)
    const started = Date.now()
    // Two-sided: the call has to return, and it has to take roughly as long as the constant says.
    // Only the upper half would pass for an adapter that gave up instantly for some other reason,
    // and only the lower half would pass for one that never returned.
    await within(PG_CONNECT_MS * 3, 'connecting to a silent socket', async () => {
      await expect(engine.connect()).rejects.toThrow('could not connect to PostgreSQL')
    })
    const elapsed = Date.now() - started
    expect(elapsed).toBeGreaterThan(PG_CONNECT_MS * 0.5)
    expect(elapsed).toBeLessThan(PG_CONNECT_MS * 2)
  }, 60_000)

  it('bounds the ClickHouse adapter at its handshake, not at its connect', async () => {
    // The distinction the reference implementation had to discover: the TCP connect **succeeds**
    // here, so a connect bound never fires. What bounds this is the wait for an answer, and that is
    // a different constant on purpose - opening is bounded tightly, reading past the handshake is
    // not bounded at all, because an analytical query legitimately takes minutes.
    expect(HANDSHAKE_TIMEOUT_MS).toBeGreaterThan(CH_CONNECT_MS)
    const port = await silentPort()
    const engine = new ClickHouseEngine(`clickhouse://default:sde@127.0.0.1:${port}/sde`)
    const started = Date.now()
    await within(HANDSHAKE_TIMEOUT_MS * 3, 'the handshake against a silent socket', async () => {
      await expect(engine.connect()).rejects.toThrow('no answer within')
    })
    const elapsed = Date.now() - started
    expect(elapsed).toBeGreaterThan(HANDSHAKE_TIMEOUT_MS * 0.5)
    expect(elapsed).toBeLessThan(HANDSHAKE_TIMEOUT_MS * 2)
  }, 90_000)
})

describe('the caller decides about their own network', () => {
  it('lets a connect_timeout in the DSN win over the default', async () => {
    // The default is a default, not a rule. A caller who wrote a bound into their DSN has made a
    // decision about their network and this library must not overrule it - which is checked by
    // measuring, because the code path that applies the default is a string test on the DSN and a
    // string test is exactly the kind of thing that reads correctly and matches nothing.
    const port = await silentPort()
    const engine = new PostgresEngine(
      `postgresql://postgres:sde@127.0.0.1:${port}/sde?connect_timeout=2`,
    )
    const started = Date.now()
    await within(PG_CONNECT_MS, "the caller's own two-second bound", async () => {
      await expect(engine.connect()).rejects.toThrow('could not connect to PostgreSQL')
    })
    // Well inside the library's own ten seconds, which is the whole assertion.
    expect(Date.now() - started).toBeLessThan(PG_CONNECT_MS * 0.6)
  }, 60_000)
})

describe('an operation on an engine that was never connected', () => {
  it('says so rather than reaching for a connection', async () => {
    // No implicit connect, for the same reason there is no implicit reconnect: a library that
    // opened a connection on demand would also be deciding when to reopen one, and rule 3 of the
    // failure semantics allows a retry only for an operation known to be idempotent.
    const engine = new ClickHouseEngine('clickhouse://default:sde@127.0.0.1:1/sde')
    await expect(engine.count('anything')).rejects.toThrow('not connected')
    const other = new PostgresEngine('postgresql://postgres:sde@127.0.0.1:1/sde')
    await expect(other.count('anything')).rejects.toThrow('not connected')
  })
})

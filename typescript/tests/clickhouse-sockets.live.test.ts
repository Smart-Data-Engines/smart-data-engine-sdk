/**
 * A keep-alive socket outlives the request that used it: Node's global agent keeps sockets alive from
 * Node 19, and this adapter's requests go through it. Whatever a request puts on the socket has to
 * come off with the request. Before 31 October 2026 one `error` listener per request stayed - 52
 * after 50 requests on one engine, measured - and Node warned of a leak after eleven.
 */
import http from 'node:http'

import { describe, expect, it } from 'vitest'

import { ClickHouseEngine } from '../src/engines/clickhouse.js'

const DSN = process.env['SDE_CLICKHOUSE_DSN']

describe.skipIf(DSN === undefined)('a ClickHouse engine on a keep-alive socket', () => {
  it('leaves no listener behind from one request to the next', async () => {
    const warnings: string[] = []
    const record = (warning: Error): void => {
      warnings.push(warning.name)
    }
    process.on('warning', record)
    const engine = new ClickHouseEngine(DSN!)
    const listeners = (): number[] =>
      Object.values(http.globalAgent.freeSockets)
        .flat()
        .map((socket) => socket!.listenerCount('error'))
    try {
      await engine.connect()
      await engine.mapWatermark()
      const first = listeners()
      for (let request = 0; request < 30; request++) await engine.mapWatermark()
      // The control: the requests did reuse a socket, or nothing here could have piled up.
      expect(first.length).toBeGreaterThan(0)
      expect(listeners()).toEqual(first)
      await new Promise((resolve) => setImmediate(resolve))
      expect(warnings.filter((name) => name === 'MaxListenersExceededWarning')).toEqual([])
    } finally {
      process.off('warning', record)
      await engine.close()
    }
  })
})

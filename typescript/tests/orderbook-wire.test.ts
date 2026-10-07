/**
 * What an answer can begin with decides when the reader waits and when it refuses.
 *
 * The reader waited for 16 bytes before it judged them. So a pushed row longer than that, still
 * arriving, was refused as an answer it could not read. And bytes that begin no answer, fewer
 * than 16, were waited on until the timeout. Finding 35 of the C++ port, whose reader refuses as
 * soon as the bytes cannot begin an answer and waits for a pushed row to finish.
 */
import { createServer, type Server, type Socket } from 'node:net'
import type { AddressInfo } from 'node:net'
import { afterEach, expect, it } from 'vitest'
import { WireConnection } from '../src/engines/_orderbook-wire.js'

const servers: Server[] = []
const sockets: Socket[] = []
afterEach(async () => {
  for (const socket of sockets.splice(0)) socket.destroy()
  while (servers.length > 0) {
    const server = servers.pop()!
    await new Promise<void>((resolve) => server.close(() => resolve()))
  }
})

/** A server that greets, then answers the first command with these pieces, a pause between them. */
async function scripted(pieces: readonly string[]): Promise<number> {
  const server = createServer((socket: Socket) => {
    sockets.push(socket)
    socket.write('OK ob_tcp_server v0.1.0\n\n')
    socket.once('data', async () => {
      for (const [index, piece] of pieces.entries()) {
        if (index > 0) await new Promise((resolve) => setTimeout(resolve, 100))
        socket.write(piece)
      }
    })
  })
  servers.push(server)
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  return (server.address() as AddressInfo).port
}

it('waits for a pushed row longer than sixteen bytes to finish, and reads the answer behind it', async () => {
  const port = await scripted(['PUSH 7 BTCUSDT binance bid 0 5000000 3 1 10', '00\nPONG\n'])
  const wire = await WireConnection.open({ host: '127.0.0.1', port, timeoutMs: 5000 })
  try {
    expect(await wire.execute('PING')).toBe('PONG\n')
  } finally {
    await wire.close()
  }
})

it('refuses bytes that begin no answer at once, not when the timeout runs out', async () => {
  const port = await scripted(['XYZ\n'])
  const wire = await WireConnection.open({ host: '127.0.0.1', port, timeoutMs: 5000 })
  const started = Date.now()
  try {
    await expect(wire.execute('PING')).rejects.toThrow('this client cannot read')
    expect(Date.now() - started).toBeLessThan(1000)
  } finally {
    await wire.close()
  }
})

it('waits on the first bytes of an answer that has not finished arriving', async () => {
  const port = await scripted(['P', 'ONG\n'])
  const wire = await WireConnection.open({ host: '127.0.0.1', port, timeoutMs: 5000 })
  try {
    expect(await wire.execute('PING')).toBe('PONG\n')
  } finally {
    await wire.close()
  }
})

/**
 * The orderbook engine's text protocol, over TCP or TLS, with nothing else in between.
 *
 * The engine ships a Python client and no Node one, so this is the protocol read from that client
 * and from the server's response formatter at the commit `.github/orderbook-engine.txt` pins, and
 * measured against a running `ob_tcp_server` before it was written down:
 *
 * - the server greets with `OK ob_tcp_server v<version>\n\n` once TLS, if any, has finished;
 * - a command is one line, and `MINSERT` carries one more line per level;
 * - an answer is `ERR <message>\n`, `PONG\n`, or `OK` followed by a body and an empty line -
 *   `OK\n\n` when there is no body;
 * - a server with client authentication answers `AUTH` with `OK CHALLENGE <nonce>`, and the second
 *   `AUTH <identity> <hmac>` with `OK AUTH`; the HMAC is SHA-256, keyed by the secret, over five
 *   NUL-separated fields;
 * - answers come back in the order the commands went, so commands can be pipelined;
 * - a subscription's `PUSH ` lines may arrive in front of an answer. Nothing here subscribes, and
 *   they are skipped anyway, because a parser that matches only the front of the buffer hangs on
 *   one.
 *
 * **One exchange that does not finish closes the connection.** The rest of its answer may still be
 * on the way, and the next command would read it as its own reply: the engine session measured
 * exactly that in its own client (#171) - the query after a timed-out one got another symbol's
 * rows, with no error, for the life of the connection. So a timeout, a reset or an answer this
 * code cannot read ends the connection, and every later call is refused with the reason.
 */

import { createHmac } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { connect as netConnect, type Socket } from 'node:net'
import { connect as tlsConnect, type ConnectionOptions } from 'node:tls'

import { EngineError } from '../errors.js'

export interface WireOptions {
  readonly host: string
  readonly port: number
  readonly auth?: { readonly identity: string; readonly secret: string }
  readonly tls?: boolean
  readonly tlsCaFile?: string
  readonly tlsVerify?: boolean
  readonly timeoutMs: number
}

/** One parsed answer: an error message, or a header and its rows, tab-separated as sent. */
export interface Answer {
  readonly error: string | null
  readonly header: readonly string[]
  readonly rows: readonly (readonly string[])[]
  readonly raw: string
}

export function parseAnswer(raw: string): Answer {
  if (raw.startsWith('ERR ')) {
    return { error: raw.slice(4).replace(/\n+$/, ''), header: [], rows: [], raw }
  }
  if (raw.startsWith('PONG')) return { error: null, header: [], rows: [], raw }
  if (raw.startsWith('OK\n') || raw.trim() === 'OK') {
    const lines = raw.slice(3).split('\n')
    let index = 0
    while (index < lines.length && lines[index] === '') index += 1
    if (index >= lines.length) return { error: null, header: [], rows: [], raw }
    const header = lines[index]!.split('\t')
    index += 1
    const rows: string[][] = []
    while (index < lines.length && lines[index] !== '') {
      rows.push(lines[index]!.split('\t'))
      index += 1
    }
    return { error: null, header, rows, raw }
  }
  if (raw.startsWith('OK')) return { error: null, header: [], rows: [], raw }
  return { error: `unexpected answer: ${raw.slice(0, 80)}`, header: [], rows: [], raw }
}

/** The `key: value` lines of a STATUS answer, before its first `[section]`. */
export function statusFields(raw: string): Map<string, string> {
  const fields = new Map<string, string>()
  for (const untrimmed of raw.split('\n')) {
    const line = untrimmed.trim()
    if (line === '') continue
    if (line.startsWith('[')) break
    const colon = line.indexOf(':')
    if (colon <= 0) continue
    const key = line.slice(0, colon).trim()
    if (key === '' || key.includes(' ')) continue
    fields.set(key, line.slice(colon + 1).trim())
  }
  return fields
}

/** The response to the server's challenge: HMAC-SHA256 of five NUL-separated fields. */
export function authDigest(identity: string, secret: string, nonce: string): string {
  const message = Buffer.concat([
    Buffer.from('ob-auth-v1'), Buffer.from([0]),
    Buffer.from('client'), Buffer.from([0]),
    Buffer.from('initiator'), Buffer.from([0]),
    Buffer.from(identity, 'utf8'), Buffer.from([0]),
    Buffer.from(nonce, 'ascii'),
  ])
  return createHmac('sha256', Buffer.from(secret, 'utf8')).update(message).digest('hex')
}

/** The answers that are one line, and the prefix of a line a subscription pushes. */
const LINE_ANSWERS = ['ERR ', 'PONG', 'PRIMARY', 'REPLICA', 'STANDALONE', 'MULTI_MASTER'] as const
const PUSH = 'PUSH '

export class WireConnection {
  private socket: Socket | null = null
  private buffer: Buffer = Buffer.alloc(0)
  private closedBecause: string | null = null
  private waiting: (() => void) | null = null
  private failure: Error | null = null
  private ended = false
  private busy = false

  private constructor(private readonly options: WireOptions) {}

  /** Open, read the greeting and authenticate; or refuse with what went wrong, never the secret. */
  static async open(options: WireOptions): Promise<WireConnection> {
    const connection = new WireConnection(options)
    await connection.exchange('the handshake', async () => {
      await connection.connectSocket()
      await connection.readGreeting()
      if (options.auth !== undefined) await connection.authenticate(options.auth)
    })
    return connection
  }

  private where(): string {
    return `${this.options.host}:${this.options.port}`
  }

  private async connectSocket(): Promise<void> {
    const { host, port, timeoutMs } = this.options
    const socket = await new Promise<Socket>((resolve, reject) => {
      let settled = false
      const fail = (error: Error): void => {
        if (settled) return
        settled = true
        candidate.destroy()
        reject(error)
      }
      let candidate: Socket
      if (this.options.tls === true) {
        const tls: ConnectionOptions = {
          host,
          port,
          servername: /^[\d.]+$|:/.test(host) ? undefined : host,
          minVersion: 'TLSv1.3',
          rejectUnauthorized: this.options.tlsVerify !== false,
        }
        if (this.options.tlsCaFile !== undefined) {
          try {
            tls.ca = readFileSync(this.options.tlsCaFile)
          } catch (error) {
            reject(new EngineError(`tls_ca_file ${JSON.stringify(this.options.tlsCaFile)} is not readable: ${text(error)}`))
            return
          }
        }
        candidate = tlsConnect(tls, () => {
          if (settled) return
          settled = true
          resolve(candidate)
        })
      } else {
        candidate = netConnect({ host, port }, () => {
          if (settled) return
          settled = true
          resolve(candidate)
        })
      }
      candidate.setTimeout(timeoutMs, () => fail(new EngineError(`connecting to ${this.where()} timed out`)))
      candidate.once('error', (error) => {
        fail(
          this.options.tls === true
            ? new EngineError(`TLS handshake with ${this.where()} failed: ${text(error)}`)
            : new EngineError(`could not connect to ${this.where()}: ${text(error)}`),
        )
      })
    })
    socket.setNoDelay(true)
    socket.setTimeout(0)
    socket.on('data', (chunk: Buffer) => {
      this.buffer = this.buffer.length === 0 ? chunk : Buffer.concat([this.buffer, chunk])
      this.wake()
    })
    socket.on('error', (error) => {
      this.failure = error
      this.wake()
    })
    socket.on('close', () => {
      this.ended = true
      this.wake()
    })
    this.socket = socket
  }

  private wake(): void {
    const waiting = this.waiting
    this.waiting = null
    waiting?.()
  }

  /** Wait for more bytes, or the end of the connection, or the deadline. */
  private async more(deadline: number): Promise<void> {
    if (this.failure !== null) throw new EngineError(`the connection to ${this.where()} failed: ${text(this.failure)}`)
    if (this.ended) throw new EngineError(`the connection to ${this.where()} was closed by the server`)
    const remaining = deadline - Date.now()
    if (remaining <= 0) throw new EngineError(`no answer from ${this.where()} within ${this.options.timeoutMs} ms`)
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        this.waiting = null
        resolve()
      }, remaining)
      this.waiting = () => {
        clearTimeout(timer)
        resolve()
      }
    })
  }

  private async readGreeting(): Promise<void> {
    const deadline = Date.now() + this.options.timeoutMs
    for (;;) {
      const end = this.buffer.indexOf('\n\n')
      if (end !== -1) {
        const greeting = this.buffer.subarray(0, end).toString('utf8')
        this.buffer = this.buffer.subarray(end + 2)
        if (!greeting.startsWith('OK')) throw new EngineError(`unexpected greeting from ${this.where()}: ${greeting.slice(0, 80)}`)
        return
      }
      await this.more(deadline)
    }
  }

  private async authenticate(auth: { readonly identity: string; readonly secret: string }): Promise<void> {
    this.write('AUTH\n')
    const challenge = (await this.answer()).trim()
    if (!challenge.startsWith('OK CHALLENGE ')) {
      throw new EngineError(`the server refused the authentication request: ${challenge}`)
    }
    const nonce = challenge.slice('OK CHALLENGE '.length).trim()
    this.write(`AUTH ${auth.identity} ${authDigest(auth.identity, auth.secret, nonce)}\n`)
    const verdict = (await this.answer()).trim()
    if (!verdict.startsWith('OK AUTH')) throw new EngineError(`Authentication failed: ${verdict}`)
  }

  private write(data: string): void {
    this.socket!.write(data, 'utf8')
  }

  /** Whether the buffer begins with this ASCII text. */
  private starts(text: string): boolean {
    return this.buffer.length >= text.length && this.buffer.subarray(0, text.length).toString('latin1') === text
  }

  /** Skip the whole `PUSH ` lines a subscription would put in front of an answer. */
  private skipPushes(): void {
    while (this.starts(PUSH)) {
      const newline = this.buffer.indexOf('\n')
      if (newline === -1) return
      this.buffer = this.buffer.subarray(newline + 1)
    }
  }

  /**
   * Where the answer at the front of the buffer ends; -1 while it, or a pushed line in front of it,
   * is still arriving.
   *
   * Bytes that can begin neither are refused at once. This reader used to wait for sixteen bytes
   * before judging them. A pushed row longer than that, still arriving, was then refused as an
   * answer it could not read, and a few bytes that begin no answer were waited on until the
   * timeout (finding 35 of the C++ port).
   */
  private answerEnd(): number {
    if (this.starts(PUSH)) return -1
    if (LINE_ANSWERS.some((start) => this.starts(start))) {
      const newline = this.buffer.indexOf('\n')
      return newline === -1 ? -1 : newline + 1
    }
    if (this.starts('OK')) {
      const blank = this.buffer.indexOf('\n\n')
      return blank === -1 ? -1 : blank + 2
    }
    const head = this.buffer.subarray(0, 16).toString('latin1')
    if ([...LINE_ANSWERS, 'OK', PUSH].some((start) => start.startsWith(head))) return -1
    throw new EngineError(
      `an answer from ${this.where()} this client cannot read: ${this.buffer.subarray(0, 16).toString('utf8')}`,
    )
  }

  /** One whole answer off the front of the buffer, waiting for the rest of it if it has not come. */
  private async answer(): Promise<string> {
    const deadline = Date.now() + this.options.timeoutMs
    for (;;) {
      this.skipPushes()
      const end = this.answerEnd()
      if (end !== -1) {
        const raw = this.buffer.subarray(0, end).toString('utf8')
        this.buffer = this.buffer.subarray(end)
        return raw
      }
      await this.more(deadline)
    }
  }

  private async exchange<T>(label: string, body: () => Promise<T>): Promise<T> {
    if (this.busy) {
      throw new EngineError('one exchange at a time on an orderbook connection; this one is still in flight')
    }
    this.busy = true
    try {
      return await body()
    } catch (error) {
      this.abandon(`an exchange (${label}) did not finish: ${text(error)}`)
      throw error
    } finally {
      this.busy = false
    }
  }

  private checkOpen(): void {
    if (this.socket !== null) return
    if (this.closedBecause === null) throw new EngineError(`the connection to ${this.where()} is closed`)
    throw new EngineError(
      `the connection to ${this.where()} was closed because ${this.closedBecause}. The rest of that ` +
        'exchange may still have been on its way, and the next command would have read it as its own ' +
        'reply, so nothing more is sent on it. Connect again.',
    )
  }

  private abandon(reason: string): void {
    if (this.closedBecause === null) this.closedBecause = reason
    this.socket?.destroy()
    this.socket = null
    this.buffer = Buffer.alloc(0)
  }

  /** Send one command and read its answer. */
  async execute(command: string): Promise<string> {
    this.checkOpen()
    const verb = command.trim().split(/\s+/, 1)[0] || 'an empty command'
    return this.exchange(verb, async () => {
      this.write(command.endsWith('\n') ? command : command + '\n')
      return this.answer()
    })
  }

  /** Send every command in one write, then read one answer per command, in order. */
  async executePipelined(commands: readonly string[]): Promise<string[]> {
    this.checkOpen()
    if (commands.length === 0) return []
    return this.exchange(`a pipelined batch of ${commands.length} command(s)`, async () => {
      this.write(commands.map((command) => (command.endsWith('\n') ? command : command + '\n')).join(''))
      const answers: string[] = []
      for (let index = 0; index < commands.length; index += 1) answers.push(await this.answer())
      return answers
    })
  }

  async close(): Promise<void> {
    const socket = this.socket
    this.socket = null
    if (socket === null) return
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        socket.destroy()
        resolve()
      }, 1000)
      socket.once('close', () => {
        clearTimeout(timer)
        resolve()
      })
      socket.end('QUIT\n')
    })
  }

  get isOpen(): boolean {
    return this.socket !== null
  }
}

function text(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

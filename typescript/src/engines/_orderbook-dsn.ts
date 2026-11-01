/**
 * The orderbook engine's DSN, read by format contract 7n: one grammar, the same in every library.
 *
 * No URL parser reads it. WHATWG's and Python's disagree exactly where a DSN is written by hand -
 * WHATWG's turned the host `0x7f.1` into `127.0.0.1`, which a TLS check then compared, and `Number()`
 * read `timeout=0x10` as sixteen seconds (findings 29 and 32 of the C++ port) - so every rule is
 * written out here in the contract's order, each refusal in the contract's words. A refusal names the
 * part that is wrong and never repeats the DSN, which carries the secret: it quotes a parameter's
 * name as written, never a value.
 */

import { TextDecoder } from 'node:util'

import { EngineError } from '../errors.js'

export const DEFAULT_TIMEOUT_SECONDS = 10
/** A millisecond: a Node.js socket waits no less, so no library promises less. */
export const SHORTEST_TIMEOUT_SECONDS = 0.001
/** 2^31 - 1 milliseconds: the longest a Node.js timer waits as asked, so no library promises more. */
export const LONGEST_TIMEOUT_SECONDS = 2147483.647
/**
 * The identities the server's `--auth-secret-file` takes (the engine's `valid_identity`). One outside
 * them can never authenticate, and it is sent on the wire, where a space would end it.
 */
export const IDENTITY = /^[A-Za-z0-9_.:-]{1,64}$/

/** What a DSN says: where to connect, and how. */
export interface OrderbookDsn {
  /** A name in lower case, or an IPv6 address in lower case without its brackets. */
  readonly host: string
  readonly port: number
  readonly identity: string | null
  readonly secret: string | null
  readonly tls: boolean
  readonly ca: string | null
  readonly verify: boolean
  /** Seconds, from `SHORTEST_TIMEOUT_SECONDS` to `LONGEST_TIMEOUT_SECONDS`. */
  readonly timeout: number
}

const PREFIX = 'orderbook://'
const PARAMETERS = new Set(['ca', 'timeout', 'tls', 'verify'])
const SWITCH: Readonly<Record<string, boolean>> = { on: true, off: false }
const NAME = /^[A-Za-z0-9._-]+$/
const DIGITS = /^[0-9]+$/
const GROUP = /^[0-9A-Fa-f]{1,4}$/
const OCTET = /^(0|[1-9][0-9]{0,2})$/
const SECONDS = /^[0-9]+(\.[0-9]+)?$/
const HEX_PAIR = /^[0-9A-Fa-f]{2}$/
// Strict, and keeping a byte order mark: Python's `utf-8` codec keeps one, and without `ignoreBOM`
// this decoder would drop a secret's first character.
const UTF8 = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true })

const PRINTABLE =
  'an orderbook DSN is printable ASCII: a space, a control character or a character beyond ASCII ' +
  'is written percent-encoded'
const SCHEME = 'an orderbook DSN starts with orderbook://'
const FRAGMENT = "an orderbook DSN has no fragment: a '#' in a secret is written %23"
const CREDENTIALS = "an orderbook DSN's credentials are identity:secret, and neither is empty"
const IDENTITY_REFUSAL =
  "an orderbook DSN's identity is 1 to 64 characters of [A-Za-z0-9_.:-], as the server's " +
  '--auth-secret-file takes them'
const SECRET = "an orderbook DSN's secret is percent-encoded UTF-8"
const HOST_AND_PORT = 'an orderbook DSN names a host and a port: orderbook://host:port'
const HOST =
  "an orderbook DSN's host is a name of letters, digits, '.', '-' and '_', or an IPv6 address in " +
  'brackets'
const PORT = "an orderbook DSN's port is a number from 1 to 65535"
const PATH = 'an orderbook DSN has no path; local mode takes a data directory'
const CA = "the orderbook DSN parameter 'ca' names a file: it is not empty and holds no NUL"
const TIMEOUT =
  "the orderbook DSN parameter 'timeout' is seconds in decimal digits, from 0.001 to 2147483.647"
const CA_WITHOUT_TLS = "the orderbook DSN's ca needs tls=on: without TLS it verifies nothing"
const VERIFY_WITHOUT_TLS =
  "the orderbook DSN's verify=off needs tls=on: without TLS there is no certificate to decline to check"
const CA_WITHOUT_VERIFY = "the orderbook DSN's ca with verify=off is a trust anchor nothing consults"

/**
 * Reads `orderbook://[identity:secret@]host:port[/][?name=value[&...]]` by contract 7n. Throws
 * `EngineError` at the first rule the DSN breaks, in the rules' order.
 */
export function parseOrderbookDsn(dsn: unknown): OrderbookDsn {
  if (typeof dsn !== 'string') throw new EngineError('an orderbook DSN is a string')
  // Rule 1, by code unit: a character beyond ASCII is one or two units, each above U+007E.
  for (let index = 0; index < dsn.length; index++) {
    const unit = dsn.charCodeAt(index)
    if (unit < 0x21 || unit > 0x7e) throw new EngineError(PRINTABLE)
  }
  // Rules 2 and 3. Every character is ASCII now, so toLowerCase() is ASCII's.
  if (dsn.slice(0, PREFIX.length).toLowerCase() !== PREFIX) throw new EngineError(SCHEME)
  let rest = dsn.slice(PREFIX.length)
  if (rest.includes('#')) throw new EngineError(FRAGMENT)
  // Rule 4.
  const stops = [rest.indexOf('/'), rest.indexOf('?')].filter(found => found !== -1)
  const end = stops.length === 0 ? rest.length : Math.min(...stops)
  const authority = rest.slice(0, end)
  rest = rest.slice(end)
  const mark = rest.indexOf('?')
  const path = mark === -1 ? rest : rest.slice(0, mark)
  const query = mark === -1 ? '' : rest.slice(mark + 1)
  // Rule 5: what comes before the last @ is identity:secret, split at the first :.
  let identity: string | null = null
  let secret: string | null = null
  const at = authority.lastIndexOf('@')
  const hostport = at === -1 ? authority : authority.slice(at + 1)
  if (at !== -1) {
    const userinfo = authority.slice(0, at)
    const colon = userinfo.indexOf(':')
    const writtenIdentity = colon === -1 ? userinfo : userinfo.slice(0, colon)
    const writtenSecret = colon === -1 ? '' : userinfo.slice(colon + 1)
    if (colon === -1 || writtenIdentity === '' || writtenSecret === '') throw new EngineError(CREDENTIALS)
    identity = decoded(writtenIdentity, false)
    if (identity === null || !IDENTITY.test(identity)) throw new EngineError(IDENTITY_REFUSAL)
    secret = decoded(writtenSecret, false)
    if (secret === null) throw new EngineError(SECRET)
  }
  // Rules 6 and 7.
  const [host, writtenPort] = hostAndPort(hostport)
  const port = portOf(writtenPort)
  // Rule 8.
  if (path !== '' && path !== '/') throw new EngineError(PATH)
  // Rule 9, piece by piece, left to right.
  const options = new Map<string, string>()
  for (const piece of query.split('&')) {
    if (piece === '') continue
    const equals = piece.indexOf('=')
    const writtenName = equals === -1 ? piece : piece.slice(0, equals)
    const writtenValue = equals === -1 ? '' : piece.slice(equals + 1)
    const name = decoded(writtenName, true)
    if (name === null) {
      throw new EngineError(`the orderbook DSN parameter '${writtenName}' is not percent-encoded UTF-8`)
    }
    if (!PARAMETERS.has(name)) {
      throw new EngineError(
        `the orderbook DSN parameter '${writtenName}' is unknown; it takes ca, timeout, tls and verify`,
      )
    }
    if (options.has(name)) throw new EngineError(`the orderbook DSN gives '${name}' twice`)
    const value = decoded(writtenValue, true)
    if (value === null) throw new EngineError(`the orderbook DSN parameter '${name}' is not percent-encoded UTF-8`)
    if ((name === 'tls' || name === 'verify') && !Object.hasOwn(SWITCH, value)) {
      throw new EngineError(`the orderbook DSN parameter '${name}' is on or off`)
    }
    if (name === 'ca' && (value === '' || value.includes('\u0000'))) throw new EngineError(CA)
    if (name === 'timeout' && secondsOf(value) === null) throw new EngineError(TIMEOUT)
    options.set(name, value)
  }
  // Rule 10.
  const tls = SWITCH[options.get('tls') ?? 'off']!
  const verify = SWITCH[options.get('verify') ?? 'on']!
  const ca = options.get('ca') ?? null
  if (ca !== null && !tls) throw new EngineError(CA_WITHOUT_TLS)
  if (!verify && !tls) throw new EngineError(VERIFY_WITHOUT_TLS)
  if (ca !== null && !verify) throw new EngineError(CA_WITHOUT_VERIFY)
  const written = options.get('timeout')
  const timeout = written === undefined ? DEFAULT_TIMEOUT_SECONDS : secondsOf(written)!
  return { host, port, identity, secret, tls, ca, verify, timeout }
}

/** A timeout's seconds in decimal digits, or null: its nearest double, within the range. */
export function secondsOf(value: string): number | null {
  if (!SECONDS.test(value)) return null
  // Number() of digits alone, which the pattern has made sure of, is the nearest double at any
  // length: past the double range it is Infinity, below it zero, and the range refuses both.
  const seconds = Number(value)
  return seconds >= SHORTEST_TIMEOUT_SECONDS && seconds <= LONGEST_TIMEOUT_SECONDS ? seconds : null
}

/**
 * Percent-decoded UTF-8, or null: a `%` without two hexadecimal digits, or bytes that are not UTF-8.
 * In a form - a parameter - a `+` is a space first.
 */
function decoded(written: string, form: boolean): string | null {
  const text = form ? written.replace(/\+/g, ' ') : written
  const bytes: number[] = []
  for (let index = 0; index < text.length; ) {
    const unit = text.charCodeAt(index)
    if (unit !== 0x25) {
      bytes.push(unit) // rule 1 made every character ASCII
      index += 1
      continue
    }
    const pair = text.slice(index + 1, index + 3)
    if (!HEX_PAIR.test(pair)) return null
    bytes.push(Number.parseInt(pair, 16))
    index += 3
  }
  try {
    // Fatal: an overlong form, an encoded surrogate and a truncated sequence are refused.
    return UTF8.decode(Uint8Array.from(bytes))
  } catch {
    return null
  }
}

/** The host, in lower case, and the port as written. */
function hostAndPort(hostport: string): [string, string] {
  if (hostport.startsWith('[')) {
    const close = hostport.indexOf(']')
    if (close === -1 || !ipv6(hostport.slice(1, close))) throw new EngineError(HOST)
    const after = hostport.slice(close + 1)
    if (after === '') throw new EngineError(HOST_AND_PORT)
    if (!after.startsWith(':')) throw new EngineError(HOST)
    return [hostport.slice(1, close).toLowerCase(), after.slice(1)]
  }
  const colon = hostport.indexOf(':')
  const name = colon === -1 ? hostport : hostport.slice(0, colon)
  if (name === '') throw new EngineError(HOST_AND_PORT)
  if (!NAME.test(name)) throw new EngineError(HOST)
  return [name.toLowerCase(), colon === -1 ? '' : hostport.slice(colon + 1)]
}

function portOf(written: string): number {
  if (written === '') throw new EngineError(HOST_AND_PORT)
  if (!DIGITS.test(written)) throw new EngineError(PORT)
  // Without the zeros that lead, five digits at most: no port has more, and Number() of many more
  // is no longer the integer written.
  const digits = written.replace(/^0+/, '')
  const port = digits.length > 5 ? 0 : Number(digits === '' ? '0' : digits)
  if (port < 1 || port > 65535) throw new EngineError(PORT)
  return port
}

/**
 * RFC 3986's `IPv6address`: groups of one to four hexadecimal digits, at most one `::` standing for
 * at least one group, and an IPv4 address allowed as the last 32 bits.
 */
function ipv6(text: string): boolean {
  const at = text.indexOf('::')
  const double = at !== -1
  const head = double ? text.slice(0, at) : text
  const tail = double ? text.slice(at + 2) : ''
  if (double && tail.includes('::')) return false
  const left = head === '' ? [] : head.split(':')
  const right = double && tail !== '' ? tail.split(':') : []
  const groups = [...left, ...right]
  if (groups.length === 0 && !double) return false
  let count = 0
  for (let index = 0; index < groups.length; index++) {
    const group = groups[index]!
    // Only the address's last group may be an IPv4 address: not one before a closing "::".
    const last = index === groups.length - 1 && (!double || right.length > 0)
    if (last && group.includes('.')) {
      if (!ipv4(group)) return false
      count += 2
    } else if (GROUP.test(group)) {
      count += 1
    } else {
      return false
    }
  }
  return double ? count <= 7 : count === 8
}

function ipv4(text: string): boolean {
  const octets = text.split('.')
  return octets.length === 4 && octets.every(octet => OCTET.test(octet) && Number(octet) <= 255)
}

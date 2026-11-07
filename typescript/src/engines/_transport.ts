/**
 * Where a connection may go without TLS: the one rule the engine adapters share
 * (`specs/secure-connections`), the same as the reference's `sde/engines/_transport.py`.
 *
 * Plain transport to a host other than the loopback is accepted only when the connection string
 * says so - an explicit decision a person wrote, never a default. The decision is made on the text,
 * before connecting, and names are not resolved.
 */

import { isIP } from 'node:net'

/**
 * `localhost` in any case, an IPv4 address in 127.0.0.0/8, `::1` in any spelling, or 127.0.0.0/8
 * mapped into IPv6 (`::ffff:127.0.0.1`, which the WHATWG parser writes `::ffff:7f00:1`).
 */
export function loopback(host: string): boolean {
  const name = host.startsWith('[') && host.endsWith(']') ? host.slice(1, -1) : host
  if (name.toLowerCase() === 'localhost') return true
  // A zone (`::1%lo`) is not the loopback here. C's inet_pton refuses one, so the C++ library
  // cannot accept it, and a rule the three libraries answered differently would not be one rule.
  if (name.includes('%')) return false
  const kind = isIP(name)
  if (kind === 4) return name.split('.')[0] === '127'
  if (kind !== 6) return false
  const groups = ipv6Groups(name)
  if (groups.every((group, index) => group === (index === 7 ? 1 : 0))) return true
  return groups.slice(0, 5).every((group) => group === 0) && groups[5] === 0xffff && groups[6]! >> 8 === 127
}

/** The eight 16-bit groups of an address `isIP` has already accepted as IPv6. */
function ipv6Groups(address: string): number[] {
  let text = address.toLowerCase()
  // An IPv4 tail (`::ffff:127.0.0.1`) is the last two groups in dotted decimal, and in an address
  // `isIP` accepted a dot can only be after the last colon. Split there rather than match a
  // pattern: a backtracking pattern over a host is a cost the caller's input decides.
  const colon = text.lastIndexOf(':')
  const last = text.slice(colon + 1)
  if (last.includes('.')) {
    const [a, b, c, d] = last.split('.').map(Number) as [number, number, number, number]
    text = `${text.slice(0, colon + 1)}${((a << 8) | b).toString(16)}:${((c << 8) | d).toString(16)}`
  }
  const [head, tail] = text.split('::') as [string, string | undefined]
  const front = head === '' ? [] : head.split(':')
  const back = tail === undefined || tail === '' ? [] : tail.split(':')
  const middle = tail === undefined ? [] : new Array<string>(8 - front.length - back.length).fill('0')
  return [...front, ...middle, ...back].map((group) => parseInt(group, 16))
}

/** Name verification must not depend on Node's IDNA handling of bare IPv6 addresses. */
import { execFileSync } from 'node:child_process'
import { X509Certificate } from 'node:crypto'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { checkServerIdentity, type PeerCertificate } from 'node:tls'
import { afterAll, beforeAll, expect, it } from 'vitest'

import { verifyPeerIdentity } from '../src/engines/_tls-peer-identity.js'

let directory: string, material: Record<string, string>
beforeAll(() => {
  directory = mkdtempSync(join(tmpdir(), 'sde-peer-identity-'))
  const python = process.env.SDE_TEST_PYTHON ?? resolve('../python/.venv/bin/python')
  material = JSON.parse(execFileSync(python, [resolve('../python/tests/tls_certificates.py'), directory],
    { encoding: 'utf8', timeout: 30000 })) as Record<string, string>
})
afterAll(() => { if (directory) rmSync(directory, { recursive: true, force: true }) })
function peer(name: string): PeerCertificate {
  return new X509Certificate(readFileSync(material[name]!)).toLegacyObject()
}

it.each([
  ['127.0.0.1', 'ip_only_cert', true],
  ['127.0.0.2', 'ip_only_cert', false],
  ['::1', 'ip_only_cert', true],
  ['0:0:0:0:0:0:0:1', 'ip_only_cert', true],
  ['::2', 'ip_only_cert', false],
  ['127.0.0.1', 'dns_only_cert', false],
  ['::1', 'dns_only_cert', false],
  ['localhost', 'dns_only_cert', true],
  ['localhost', 'ip_only_cert', false],
] as const)('checks native certificate identity for %s / %s, accepted=%s', (host, certificate, accepted) => {
  const result = verifyPeerIdentity(host, peer(certificate))
  if (accepted) expect(result).toBeUndefined()
  else expect(result).toBeInstanceOf(Error)
})

it.each([undefined, null, '', Buffer.alloc(0), Buffer.from('not a DER certificate')])(
  'refuses missing or malformed raw IP certificate %s', (raw) => {
    const certificate = { ...peer('ip_only_cert'), raw } as unknown as PeerCertificate
    expect(verifyPeerIdentity('::1', certificate)).toMatchObject({ code: 'ERR_TLS_CERT_ALTNAME_INVALID' })
  })

it('does not substitute the displayed SAN string for the actual IP certificate', () => {
  const certificate = { ...peer('dns_only_cert'), subjectaltname: 'IP Address:0:0:0:0:0:0:0:1' }
  expect(verifyPeerIdentity('::1', certificate)).toBeInstanceOf(Error)
  const ip = { ...peer('ip_only_cert'), subjectaltname: 'DNS:other.invalid' }
  expect(verifyPeerIdentity('::1', ip)).toBeUndefined()
})

it.each(['localhost', 'other.invalid'])('retains the native DNS checker for %s', (host) => {
  const certificate = peer('dns_only_cert')
  const expected = checkServerIdentity(host, certificate)
  const actual = verifyPeerIdentity(host, certificate)
  expect(actual?.message).toBe(expected?.message)
  expect(actual === undefined).toBe(expected === undefined)
})

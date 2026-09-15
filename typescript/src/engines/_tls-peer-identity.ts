/** Native peer-name checks, separate from TLS's certificate-chain verification. */
import { X509Certificate } from 'node:crypto'
import { isIP } from 'node:net'
import { checkServerIdentity, type PeerCertificate } from 'node:tls'

function mismatch(): Error {
  return Object.assign(new Error('TLS peer certificate does not match the configured host.'),
    { code: 'ERR_TLS_CERT_ALTNAME_INVALID' })
}

export function verifyPeerIdentity(host: string, certificate: PeerCertificate): Error | undefined {
  try {
    if (isIP(host) === 0) return checkServerIdentity(host, certificate)
    // Node 22.23.2 applies domainToASCII before recognizing an IP, losing bare IPv6 addresses.
    // OpenSSL's X509 IP check uses the actual DER certificate without a textual SAN parser.
    if (!Buffer.isBuffer(certificate.raw) || certificate.raw.length === 0) return mismatch()
    return new X509Certificate(certificate.raw).checkIP(host) === undefined ? mismatch() : undefined
  } catch {
    // Missing/malformed peer material must never turn a verification error into acceptance.
    return mismatch()
  }
}

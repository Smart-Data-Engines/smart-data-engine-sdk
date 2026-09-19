# Engine connections and verified TLS

Connection URIs and CA files stay in the customer's application/operator environment. They are
not placement-map fields and must not be uploaded to the control plane. A signed map authenticates
placement instructions; it does not authenticate the database server. Configure both boundaries.

## ClickHouse: the same connection intent in Python and TypeScript

Both SDKs parse this explicit URI profile before opening a connection:

```text
https://USER:PASSWORD@db.example.com:8443/DATABASE?ca_cert=%2Fetc%2Fsde%2Froot-ca.pem
clickhouses://USER:PASSWORD@db.example.com/DATABASE
clickhouse://USER:PASSWORD@db.example.com:8443/DATABASE?secure=true
```

Percent-encode each username, password and database component separately. Encode query values
with `urllib.parse.urlencode` / `URLSearchParams`; do not concatenate an unescaped CA path.
Credentials are UTF-8. A username must be nonempty and contain no colon; passwords may contain one.
An absent user is `default`, with an empty password unless supplied. Use an ASCII DNS name
(punycode for international names), canonical IPv4, or bracketed IPv6. A single explicit database
segment is required. Spaces in that name must be percent-encoded; encode non-ASCII components as UTF-8.

| Scheme | Default port | Transport |
|---|---:|---|
| `https` | 443 | TLS with certificate chain and host/IP verification |
| `clickhouses` | 8443 | TLS with certificate chain and host/IP verification |
| `http` | 80 | Plain HTTP |
| `clickhouse` | 8123 | Plain HTTP; `secure=true` selects TLS and default port 8443 |

An explicit port is preserved, including 80 and 443. Plain `clickhouse` on 443/8443 without an
explicit `secure` choice is refused: an older Python driver inferred encryption from those ports,
and silently interpreting that configuration as plaintext would be unsafe. Use a secure scheme
or explicit `secure=true`; use `secure=false` only when plaintext is intended.

| Query option | Accepted meaning |
|---|---|
| `secure` | Literal `true` or `false`; must agree with `http`, `https` or `clickhouses`. |
| `verify` | Omit or literal `true`, on a TLS connection. Disabling verification is refused. |
| `ca_cert` | Absolute path to a readable PEM CA file, on TLS. Nonempty regular file, at most 1 MiB. |
| `connect_timeout` | Positive finite seconds from 0.001 through 2147483.647; default 10. |
| `send_receive_timeout` | The same range in seconds; socket inactivity, not a total query deadline. |

Unknown, duplicated, malformed and conflicting options are refused before a socket opens.
There is no arbitrary driver-kwargs channel, `verify=false`, proxy override, client certificate
or server-name override in this profile. URI fragments, malformed UTF-8, raw whitespace/backslash,
legacy numeric/octal IP forms, IPv6 zones and multi-segment database paths are refused. Encoded
slashes in a database name are refused too. These refusals prevent two languages from interpreting
one local configuration differently; they do not change placement-map bytes.

A supplied CA replaces the default trust roots for that connection. The SDK reads it before the
first request and pins the trust material for the connection's lifetime. After updating the file,
close and reconnect explicitly to use the new CA. Without `ca_cert`, the runtime/driver's default
trust store applies. Python and Node may have different default trust stores; qualify both in the
actual application environment. Neither SDK changes process-wide trust configuration. The
TypeScript adapter explicitly retains verification even if `NODE_TLS_REJECT_UNAUTHORIZED=0` is
set elsewhere in the process.

The initial ClickHouse handshake has a 15-second receive-inactivity default. Python retains this
transport inactivity bound for later operations. TypeScript retains its existing unbounded query
receive default; specifying `send_receive_timeout` bounds later request inactivity too. This
explicit difference is about the native transports, not a guarantee on analytical query duration.
The connect timeout covers establishment of a new connection. Progress can extend a receive
inactivity timer. Configure query execution limits at the server when the workload needs them.

No request follows an HTTP redirect or automatically replays a transport-ambiguous operation,
including the first version/settings request. After a lost response, inspect the uncertain outcome;
reconnecting does not mean that replaying the write is safe. See [failure semantics](failure-semantics.md).

## PostgreSQL

Use an explicit verified profile for a customer connection:

```text
postgresql://USER:PASSWORD@db.example.com:5432/DATABASE?sslmode=verify-full&sslrootcert=%2Fetc%2Fsde%2Froot-ca.pem
```

`verify-full` means both certificate-chain validation and server identity validation. Configure
`sslrootcert` for a private CA. A DSN without an explicit TLS policy is retained for existing local
development; the SDK does not label that as a verified production connection. PostgreSQL driver
options outside the qualified profile retain their native interpretation. In particular, encryption
without server identity verification is a weaker property. Do not infer it from a successful
connection or from a `require` / `prefer` spelling across different drivers.

The TypeScript adapter binds a literal IP from the driver's effective connection configuration
to TLS certificate verification, including IPv6. The native driver otherwise supplied a connected
socket without this identity, causing Node to check its `localhost` fallback: a DNS-only localhost
certificate was accepted for an IP connection, while a correct IP-only certificate was refused.
The fix preserves CA material, callbacks and existing SSL modes, and clones SSL option descriptors
so another client or `pg.defaults.ssl` is not changed. Active TLS also supplies
`rejectUnauthorized=true` unless the native configuration explicitly requests `false`, preventing
`NODE_TLS_REJECT_UNAUTHORIZED=0` from weakening `verify-full`. Explicit legacy `no-verify` and
`disable` keep their native meaning; they are outside the verified profile. URI IPv6 brackets are removed only after
identifying a valid literal IP. Native CI uses an IP-only PostgreSQL certificate to cover this case.

The [PostgreSQL TLS documentation](https://www.postgresql.org/docs/current/libpq-ssl.html)
explains libpq's modes. The [Node driver documentation](https://node-postgres.com/features/ssl)
also warns that SSL URI options can replace a separately supplied SSL object. Qualify the exact
installed runtime/driver and credential policy before using a customer endpoint.

## Compatibility and evidence

This fixes measured transport errors: Python previously sent HTTP for an `https` URI on a custom
port; TypeScript ignored `secure=true`; unknown schemes/options could be silently accepted; and
Python's driver could disable verification for a mistyped `verify` value. HTTPS now means TLS
regardless of port. HTTP/HTTPS use their standard default ports. Configurations relying on old
silent defaults or arbitrary ClickHouse driver query arguments must be made explicit.

`testdata/clickhouse-dsns.json` is a shared input profile, separate from the placement conformance
vectors. Local certificate tests check the actual wire, CA, DNS/IP, expiration, first-request replay,
redirects, credential encoding and CA isolation. Their negative controls observe a TCP/TLS attempt
and the absence of application requests; merely catching an `EngineError` is not sufficient.

Native qualification uses ephemeral test certificates, isolated PostgreSQL 15 and ClickHouse 24.8,
custom ports, exact integer/decimal/microsecond values, single and batch writes, and unrelated-CA
refusals in both languages. It creates and removes only its own containers:

```bash
make tls-check TLS_SCRATCH=/absolute/new/private/test-directory TLS_DOCKER_FLAGS=--sudo
```

Omit `TLS_DOCKER_FLAGS` where Docker is already accessible. Install the Python test extras and
TypeScript dependencies first. `TLS_SCRATCH` must not exist. Native TLS runs in every Python/Node
CI matrix job, and also against the supported Python ClickHouse driver floor. This is automated
engineering evidence; the customer's certificates, network path, effective grants and workload
remain part of pilot qualification.


### IPv6 across Node versions

Node 22.23.2 applies DNS ASCII conversion before classifying an IP inside its default identity
checker; a bare IPv6 address becomes empty and a matching IP certificate is rejected. This was
reproduced both with the native function alone and with actual ClickHouse/PostgreSQL TLS sockets.
The SDK uses native [`X509Certificate.checkIP`](https://nodejs.org/download/release/v22.23.2/docs/api/crypto.html#x509checkipip)
for literal IP identities and the standard DNS checker for names. `checkIP` checks the signed DER
certificate's IP SANs; no textual SAN parser or Common Name fallback is added. TLS still validates
the certificate chain before the identity callback. Explicit PostgreSQL callbacks retain their
meaning. Missing or malformed peer certificate bytes cause refusal.


## Exact TypeScript numbers over ClickHouse JSON

Every TypeScript JSON request explicitly selects quoted wide integers, quoted decimals and
preserved decimal scale. Server defaults changed for integers in newer ClickHouse versions;
converting a JSON number back to text after `JSON.parse` cannot restore lost digits.
The decoder refuses unquoted Int64/UInt64/wider integers and decimals rather than returning
rounded data. The setting applies to this request, without changing the server or other clients.

The account must permit these output-format settings or already have all three set to `1`.
A `readonly=1` profile locking incompatible defaults is refused. The owner can configure these
exact defaults while keeping `readonly=1`, or use `readonly=2` with SELECT-only grants, permitting
output settings while refusing modifying queries. Do not grant write privileges just to address
a format refusal. The existing read-only EXPLAIN scope is checked
separately. Runtime roles that need signed-map bookkeeping retain their documented grants.

The change covers ordinary point/range reads, migration key reads and numeric metadata;
logical scan and summarize already use explicit text projections. Stored values, placement-map
bytes and signatures are unchanged. Before this correction, an independently verified
`12345678901234567890.123456789012345678` was returned by TypeScript as
`12345678901234567000`; do not use results from that path as a benchmark correctness oracle.

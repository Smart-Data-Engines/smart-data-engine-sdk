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

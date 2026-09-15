# Verified engine TLS qualification

The initial probe observed the first five transport bytes with synthetic credentials. Plain
ClickHouse controls emitted `POST ` in both SDKs. Python also emitted `POST ` for `https` on a
custom port; TypeScript emitted `POST ` when a `clickhouse` URI specified `secure=true`.

The repaired adapters use one shared ClickHouse URI profile and native TLS with explicit
verification. [The connection guide](../../engine-connections.md) is the client-facing contract,
including compatibility refusals, CA handling and timeout scope. This directory records local
engineering evidence; the CI workflow repeats native TLS in every supported Python/Node job.

The PostgreSQL probe deliberately separates DNS-only and IP-only certificates signed by the same
CA. The former TypeScript adapter accepted `localhost` for a `127.0.0.1` URI and refused the correct
IP-only certificate. Python made both decisions correctly. A real PostgreSQL 15 with the IP-only
certificate reproduced the TypeScript failure: Node reported that it checked `localhost` against
the certificate's `127.0.0.1` identity. The fix binds TLS to the effective IP and leaves DNS,
native connection options and explicit SSL modes intact. A further negative control showed that
`NODE_TLS_REJECT_UNAUTHORIZED=0` let an unrelated CA through `verify-full`. The per-client TLS
configuration now explicitly enables verification unless the native SSL mode specifies false.
The test covers this on both literal-IP and DNS connections.

The local suites cover actual TCP/TLS bytes, certificate chains and DNS/IP identity, expiration,
CA-file limits, first-request response loss, redirects, UTF-8 credentials and per-client trust.
PostgreSQL's local server only handles SSLRequest and TLS, then records StartupMessage; it does
not simulate a successful database login. Positive native database operations are separate.

`tools/qualify_tls.py` starts disposable PostgreSQL 15 and ClickHouse 24.8 servers with newly
generated test keys/CA and random loopback ports. PostgreSQL presents an IP-only certificate.
The Python and TypeScript workers create their own tables, perform single/batch writes, read
exact values back, and refuse an unrelated CA. The workers also run from a fresh wheel/npm
installation; no release tag or registry publication is involved. The runner removes only its
own labeled containers, including after the expected pre-fix failure.

Mutation runs start from committed source, assert that every replacement changed bytes, and
restore the exact originals in `finally`. Positive controls run before and after each language's
batch. One preliminary mutation of `redirect=False` alone did not change wire behavior because
`retries=False` independently prevents urllib3 redirects. The final run includes the existing
wrapper contract test and removal of both guards together; this distinguishes redundant controls
from an untested redirect boundary. The recorded final outcomes must be read with that scope.

Certificates and private keys are generated under the runner's private scratch directory. None
is stored in this repository. Customer endpoint/CA deployment, role grants and workload acceptance
are separate from this synthetic native transport qualification.

Local results: **1643 Python tests passed**, with only ten optional orderbook skips; **761 TypeScript tests passed**, without skips. The installed Python wheel on clickhouse-connect 1.7.2 passed **127 TLS/transport tests**. All **18 final mutations** were detected, with positive controls and exact restoration. [Machine-readable acceptance](acceptance.json) records artifact digests and native cases; [mutation reports](mutations.json) retain the tested source digests.


CI exposed three additional boundaries after the first local acceptance. Node 22.23.2's native
identity checker rejected valid IPv6 certificates after DNS ASCII conversion; the shared IP
checker now uses native X509 `checkIP`, with independent malformed-certificate and mismatch cases.
The local DNS fixture now listens on both IPv4 and IPv6 loopback at one port, preserving the
assertion that the client actually reached TLS regardless of the runner's localhost resolution.
Python 3.13 rejected the original ephemeral certificates under its stricter defaults because the
issuer key identifier was absent. Generated CA/leaf certificates now carry SKI, AKI and KeyUsage;
the native readiness probe enforces strict verification on all Python versions and reports a
certificate error immediately. The verification flags were not relaxed. The documented
[Python default-context change](https://docs.python.org/3.13/library/ssl.html#ssl.create_default_context)
was reproduced locally by applying the same OpenSSL flags.

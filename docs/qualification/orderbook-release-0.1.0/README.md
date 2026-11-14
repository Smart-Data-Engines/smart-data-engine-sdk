# The libraries against the orderbook engine's release 0.1.0

14 November 2026. The suites run against the orderbook engine at the commit
`.github/orderbook-engine.txt` pins, `31b4390` of 23 October 2026. The engine's only release is
0.1.0, the tag `v0.1.0` on `555ad74` of 12 October 2026, and the Python client PyPI serves
(`orderbook-dbengine` 0.1.0) is that release's. So until this record nothing said what a client who
installs the release gets, in either direction: the libraries against the release's server, and the
release's client against a newer server.

## What ran

One laptop (i3-7100U, Ubuntu 26.04, Python 3.14.4, Node 22.22.1), over TCP on the loopback.

- **Servers.** `ob_tcp_server` built from source at the tag `v0.1.0` and at the pinned commit. Each
  ran twice: plain, and with `--auth-secret-file` and `--tls-client` under a private CA.
- **The Python environment** is the one a client gets from the index: this library installed from
  its tree with `[orderbook,signed]`, `orderbook-dbengine==0.1.0`, and nothing of the engine's
  checkout on the path ([`environment.txt`](environment.txt)). TypeScript and C++ speak the engine's
  protocol themselves, and the reference they read beside is that same interpreter.
- **The cases** are the libraries' own live slices, unchanged: the TCP cases of
  `test_orderbook_slice.py` and `test_orderbook_session_slice.py`, `test_orderbook_books.py`,
  `orderbook.slice.test.ts`, `orderbook.books.test.ts`, and the C++ `OrderbookSlice`,
  `OrderbookSecureSlice` and `BooksSlice`.

[`measure.sh`](measure.sh) is the whole procedure. The library sources it ran are the ones of the
change that added this record; local paths in the logs are replaced by `<sdk>` and `<work>`.

## Results

| Run | Server | Passed | Not passed |
|---|---|---|---|
| Python, the client from PyPI | 0.1.0 | 23: every TCP case of the two slices, the three with credentials and TLS included | 4 book stream cases, each refused by name |
| TypeScript | 0.1.0 | 7: its whole slice, TLS and both directions beside the reference included | 3 book stream cases, each refused by name |
| C++ | 0.1.0 | 25: `OrderbookSlice` and `OrderbookSecureSlice` whole | 4 `BooksSlice` cases, each refused by name |
| Python, the client from PyPI | the pin | 26: the same TCP cases, and `test_orderbook_released_client.py` | none |

Logs, in that order: [`python-released-client-against-0.1.0.log`](python-released-client-against-0.1.0.log),
[`typescript-against-0.1.0.log`](typescript-against-0.1.0.log),
[`cpp-against-0.1.0.log`](cpp-against-0.1.0.log),
[`python-released-client-against-the-pin.log`](python-released-client-against-the-pin.log). The
first three end with a failing status ([`status.txt`](status.txt)), and that is the result rather
than a broken run: the stream cases expect a stream, and against 0.1.0 every one of them stops at
the same refusal, before anything is sent:

> this orderbook server does not replace a side of a book (capability replace_side), which a book
> stream needs: in full mode for every update, in incremental mode for each resync. Upgrade the
> server; nothing was sent.

Those cases stop at the stream, so a book as of a moment was asked of the 0.1.0 server directly
([`book-as-of-a-moment-against-0.1.0.log`](book-as-of-a-moment-against-0.1.0.log)): the server does
not announce `replace_side`, and `book_at` is refused by name, before anything is read.

## What it found

**The release's Python client against a newer server failed without a name.** The pinned server
announces `replace_side`, the 0.1.0 client has no such method, and the adapter asked only the
server. The first update of a book stream was
`AttributeError: 'OrderbookEngine' object has no attribute 'replace_side'`: four cases of
`test_orderbook_books.py` over TCP, measured the same day before the fix. The adapter now refuses a
stream on such a client before anything is sent, naming the client, and
`test_orderbook_released_client.py` holds it in the `orderbook` CI job with the client from the
index. What that client can do stays open to it: an update by price, and a book as of a moment.

**The library said the client was not on PyPI.** It has been there since 18 October 2026. The
adapter's message for a missing client, this repository's README, `docs/orderbook.md`, a comment in
the packaging metadata and two test files said otherwise, one of them by asserting the message. The
client is now the extra `orderbook`.

## What was not measured

- Local mode, the engine in the application's process, against the release's shared library. The
  package on PyPI is the client alone, and the library was not built from the tag here.
- The release's packages (`.deb`, `.rpm`, the tarball). The 0.1.0 server here was built from the
  tag's source, in a Debug build; the pinned one is a Release build.
- Any machine but this one. Nothing here is a number: every result is a case that passed or a
  refusal that was read.

## Reproducing

Build `ob_tcp_server` at `v0.1.0` and at the pinned commit, start each plain and with credentials
and TLS as the `orderbook` job of `.github/workflows/ci.yml` does, build this library's TypeScript
and C++ (`make check` does both), and run:

```bash
SDK=$PWD OUT=/tmp/orderbook-release WORK=/tmp/orderbook-release-work \
  OLD_TCP=127.0.0.1:59190 OLD_SECURE_DSN='orderbook://identity:secret@localhost:59191?tls=on&ca=/path/ca.pem' \
  PIN_TCP=127.0.0.1:59090 PIN_SECURE_DSN='orderbook://identity:secret@localhost:59091?tls=on&ca=/path/ca.pem' \
  docs/qualification/orderbook-release-0.1.0/measure.sh
```

`SHA256SUMS` lists every file here but this one.

# What works with what

Which engine versions the suites run against, which drivers the libraries take, which maps and
signed packets each release reads, and what a release of which number may change.
`python/tests/test_compatibility.py` holds this page to the CI workflow, the packaging metadata and
the libraries' own constants, so a version moved in one of them and not here fails a test.

Runtimes - Python, Node, compilers, operating systems - are on [`platforms.md`](platforms.md). What
each library implements is on [`implementations.md`](implementations.md).

## Engines

Tested means the live suites run against that version on every pull request. It is not the same
claim as supported: a version that is not in this table is one we have not run, and we say nothing
about it that we have not measured.

| Engine | Tested | How CI runs it |
|---|---|---|
| PostgreSQL | 15 | `postgres:15-alpine` |
| ClickHouse | 24.8 and 26.9 | `clickhouse/clickhouse-server:24.8-alpine` and `clickhouse/clickhouse-server:26.9-alpine` |
| Elasticsearch | 9.5.5 | `elasticsearch:9.5.5` |
| the orderbook engine | the commit in [`.github/orderbook-engine.txt`](../.github/orderbook-engine.txt) | built from that commit of its source |

A version enters or leaves this table by an edit of `.github/workflows/ci.yml`, never by a floating
tag: an image name with a version is what makes "tested against" a statement about one server.

What is measured about other versions, and nothing more is claimed:
- **ClickHouse 24.8 holds days and instants from 1900-01-01 to 2299-12-31**, and 26.9 holds the
  contract's whole range. Each adapter reads its server's range once per connection and refuses a row
  outside it before it is sent ([format contract](format-contract.md) §8b).
- **ClickHouse from 26.5 writes a one-column sort key back in parentheses.** The libraries read both
  spellings; the catalogue reading was measured on nine releases from 24.8 to 26.9.
- **Elasticsearch means Elasticsearch 9.** The layout of §7m, its strict mappings and the write
  fence in an index's final pipeline were written against 9.5.5, and no earlier major version was
  run.

### The orderbook engine's release

The pinned commit is newer than the engine's release, 0.1.0, so the suites alone say nothing about
the server a client installs from that release. Measured against it on 14 November 2026, over TCP,
with the server built from the tag `v0.1.0`
([the record](qualification/orderbook-release-0.1.0/README.md)):

| Library | Against the engine's 0.1.0 server |
|---|---|
| Python, with the client from PyPI (`orderbook-dbengine` 0.1.0) | every TCP case of the two slices passes, with credentials and TLS |
| TypeScript | every case of its slice passes, with credentials and TLS, and it and the reference each read what the other wrote |
| C++ | every case of its slice passes, with credentials and TLS, and it and the reference each read what the other wrote |

In all three, a book stream (`Session.book_stream`) and a book as of a moment (`Session.book_at`)
are refused by name on that server, before anything is sent or read: they need a server that
announces `replace_side`, which came after 0.1.0. Local mode, the engine in the application's
process, was not run against the release's library.

The other way round, the client from PyPI against the pinned server, is held on every pull request
by the `orderbook` CI job: the TCP cases of both slices pass, and a book stream is refused by name
because that client cannot replace a side.

## Drivers

The core of the Python library and of the TypeScript library has no dependency: a driver comes
with the engine you use.

| Library | Engine | Driver | Declared |
|---|---|---|---|
| Python | PostgreSQL | psycopg 3 | `psycopg[binary]>=3.1`, the extra `postgres` |
| Python | ClickHouse | clickhouse-connect | `clickhouse-connect>=1.7.2`, the extra `clickhouse` |
| Python | signed maps | cryptography | `cryptography>=42`, the extra `signed` |
| TypeScript | PostgreSQL | pg | `pg` `>=8`, an optional peer dependency |
| TypeScript | ClickHouse | none: the adapter speaks the HTTP interface itself | |
| Python, TypeScript | Elasticsearch | none: the adapter speaks the REST interface itself | |
| Python | the orderbook engine | the engine's own Python client | `orderbook-dbengine>=0.1.0`, the extra `orderbook` |
| TypeScript | the orderbook engine | none: the adapter speaks the engine's protocol itself | |
| C++ | PostgreSQL, ClickHouse, the orderbook engine | libpq, libcurl, OpenSSL 3 | found by CMake; the core needs OpenSSL 3 and utf8proc |

CI runs the Python suite on current drivers, and two floors beside them: on the oldest Python, the
batch and transport slices on `clickhouse-connect==1.7.2`; in the `orderbook` job, the TCP slices on
`orderbook-dbengine==0.1.0`, the engine's client as PyPI serves it. The extra `orderbook` is the
client alone: local mode also needs the engine's shared library, built from its source
([`orderbook.md`](orderbook.md#connecting)).

## Maps and signed packets

A library reads a document by the number the document carries. The numbers a library of this
repository reads are constants in its source, and the table on
[`implementations.md`](implementations.md#supported-by-us) gives them for each library.

| Document | Read by `main` | Where it is defined |
|---|---|---|
| model IR | 1 | [format contract](format-contract.md), `conformance/contract-version.txt` |
| placement map | 1 to 6 | [format contract](format-contract.md) §7 and §11 |
| verification request | 1 | [format contract](format-contract.md) §7b |
| cutover packet | 1 and 2 | [`cutover-packets.md`](cutover-packets.md) |
| staging packet | 1 and 2 | [`staging.md`](staging.md) |
| index build packet | 1 and 2 | [`in-place-index.md`](in-place-index.md) |

By release, for the two libraries on a registry:

| Release | Placement map | Cutover, staging and index build packets |
|---|---|---|
| 0.1.0, 0.1.1 | 1 to 5 | 1 and 2 |
| 0.2.0 | 1 to 6 | 1 and 2 |
| `main` | 1 to 6 | 1 and 2 |

**Backwards compatible, forwards strict.** A library reads every earlier map contract with its
meaning unchanged, and refuses a later one by its number: what came after it cannot be known, so it
is not guessed. Two things follow.
- **Upgrade the libraries before a map of a newer contract reaches them.** A service still on a
  library that reads 1 to 5 refuses a contract-6 map whole when it loads it.
- **Upgrade the operator before a packet of a newer protocol reaches it.** `sde-operator` refuses a
  packet whose protocol it does not know when it loads it, before it changes anything.

The Python and TypeScript libraries of one client need not move together. What makes two of them
agree is not a version number: it is the conformance suite, which holds every library to the same
bytes for a model's version, a map and a document.

## What a release may change

Both libraries are 0.x, where the minor number is the compatibility line: `npm install` saves
`^0.2.0`, which accepts 0.2.x and not 0.3.0, and pip's `~=0.2.0` means the same.

- **A patch release** (0.2.0 to 0.2.1) carries fixes that change nothing a caller relies on.
- **A minor release** (0.2 to 0.3) may change an API, refuse input the previous one accepted, or
  read a document an earlier library cannot. Each such change is marked in the
  [changelog](../CHANGELOG.md), and [`upgrading.md`](upgrading.md) says who is affected and what to
  do.
- **No release changes the bytes of an existing contract.** A model's version, a map's canonical
  form and a document's identifier are held by the conformance vectors; a change to any of them is a
  new contract number, which every earlier library refuses by name rather than misreads.

Security fixes go into the latest release of each library ([`SECURITY.md`](../SECURITY.md)). The C++
library is built from this repository and has no release yet; its compatibility line is the
commit.

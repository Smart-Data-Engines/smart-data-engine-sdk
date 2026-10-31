# Changelog

The Python and TypeScript libraries are released separately, one tag per language: `python-v*` to
PyPI and `typescript-v*` to npm ([`docs/publishing.md`](docs/publishing.md) §5). The C++ library is
not released yet; it is built from this repository. A shared version number does not make them
agree. What does is the conformance suite and
[`conformance/contract-version.txt`](conformance/contract-version.txt).

## Unreleased

**Fixed: TypeScript's ClickHouse adapter left a listener on its socket after every request**
(TypeScript). Node's global agent keeps sockets alive from Node 19, so one socket serves request after
request, and each request added an `error` listener to it that stayed. One engine's socket held 52
after 50 requests, measured, and Node warned of a leak after eleven: memory that grew with every query
of a long-lived application. A request's listeners now come off when it ends, and a live test counts
them after thirty requests.

**Fixed: the C++ library's PostgreSQL adapter read a `real` as the decimal its text names** (C++,
decision 42 of the port's findings). The server's shortest text for the float nearest 1.1 is `1.1`,
and the adapter parsed it as a double, so it read 1.1: another number than the one stored, and
another than its ClickHouse adapter reads for the same value from RowBinary. It parses a `real` as
a float now and widens it exactly, to 1.100000023841858, as the reference and TypeScript have read
every engine's float32 since #139. A `double precision` column is read as before. It never reached
a release.

**Fixed: a value its field's type does not hold is refused before any engine** (both libraries,
[`docs/format-contract.md`](docs/format-contract.md#a-value-its-type-holds) §8b). A row's values
went to the driver as the application gave them, and each engine answered for them in its own way.
Measured through `Session.save` on PostgreSQL 15 and ClickHouse 24.8:
- a decimal with more fractional digits than its column was rounded by PostgreSQL and truncated by
  ClickHouse, from both libraries;
- a decimal past its precision was stored by Python's ClickHouse driver with a digit changed;
- from TypeScript, ClickHouse wrapped an integer outside int32 to the opposite sign and stored
  `NaN` in a decimal column as 0.00;
- ClickHouse stored 2026-02-30 as 2026-03-02, and a timestamp made from a number or from words;
- ClickHouse stored the infinity or the zero a `float32` rounds a value to, where PostgreSQL
  refused it.

A value now takes a form a filter of its type takes, and its type must hold it. The refusal names
the field, the type and what does not fit, never the value. The value reaches the engine in one
form per type: a decimal at its column's scale, a timestamp as its UTC instant, a UUID in lower case.
Shared vectors: `errors/113`-`120` and `migration/200`.
- **Breaking** for code that relied on a driver's conversion:
  - a Python `float`, or a TypeScript `number` with a fraction, for a decimal field: pass a
    `Decimal` or decimal text;
  - a boolean for an integer field, and text for a number;
  - a `Date` (TypeScript) or a `datetime` (Python) for a `date` field: pass the day.
- TypeScript writes `NaN` and the infinities to ClickHouse as those values. JSON has no token for
  them, and ClickHouse had stored the `null` it got as NULL, or as 0 in a required column.
- Maps, DDL and stored data do not change. A `json` field is not checked.

**The C++ library refuses a value its field's type does not hold, as the other two do**
([`docs/format-contract.md`](docs/format-contract.md) §8b, point 4). The check comes before any
engine is called, and each admitted value goes on in the form a read plan gives it: a decimal at
its column's scale, a timestamp as its UTC instant. The vectors `errors/113`-`120` and
`migration/200` hold it in all three libraries. Its ClickHouse adapter had already refused a
decimal past its column; now the session refuses it first, in the contract's words.

**Fixed: TypeScript's orderbook reader judged an answer by its first sixteen bytes** (TypeScript).
It waited for sixteen bytes before deciding whether they began an answer. A row a subscription
pushed in front of an answer, longer than that and still arriving, was refused as an answer the
client could not read. A few bytes that began no answer were waited on until the timeout. It now
decides by what an answer can begin with: it waits while the bytes are on their way to one, a
pushed row included, and refuses at once when they cannot be. Finding 35 of the C++ port.

**Fixed: TypeScript's orderbook adapter accepted a symbol with a character that does not print**
([`docs/orderbook.md`](docs/orderbook.md)). It refused whitespace and controls, but not a zero-width
space, a bidirectional mark, a byte-order mark or private use. Two books that printed alike were
then two addresses. It refuses what the reference's `str.isprintable()` refuses now, by Unicode
category. Compared over every code point, the only difference left is Unicode's version: Node 18
knows 15.1, Python 3.12 knows 15.0. Finding 33 of the C++ port.

**Fixed: the orderbook adapter's connection, as the C++ port found it** (Python,
[`docs/orderbook.md`](docs/orderbook.md)).
- A write with no connection was reported as a write that may have been stored. The adapter read
  its connection inside the write's own `try`, so `insert` before `connect()` answered that the
  engine did not confirm the write and logged `sde.write.failed`. `insert_many` failed with an
  `AttributeError`, and after a lost connection it reported an unknown outcome for updates never
  sent. Both now refuse with `not connected; call connect() first`, before anything is sent, as
  TypeScript does.
- `connect()` kept a connection the engine's client had closed. After an exchange that does not
  finish, the client closes its connection and says why on every later call, and `connect()`
  returned at once because a client was set. It pings the connection now and replaces a closed one.

**Fixed: `sde.schema.extra_columns` named the library's own generation column** (Python). The event
is for a column added outside SDE. From contract 4 a table carries `__sde_write_epoch`, which
provisioning adds for a map with write generations, and every schema check named it. An alert on the
event fired on every start of every such deployment, about a column that is ours. A column a client
adds is still named.

**Measured: what the C++ library costs per operation** ([`docs/implementing.md`](docs/implementing.md#measuring-your-overhead)).
Requirement 3.5 gives the library one percent of an operation. Measured in C++ for the first time,
a point read through the session cost 0.64 µs at the median built optimised, 2.5% of the cheapest
round trip C++ makes: every call copied and sorted its key and found its shape, group, route,
engine and table by name. The session now decides each entity's point read once, when it opens:
0.11-0.12 µs without telemetry, 0.37-0.47% of that floor (break-even 11-12 µs), and 0.24 µs with a
recorder, 0.09% of a PostgreSQL point read. CI judges both on an optimised build. It never reached
a release.

**New: the trading example in C++** ([`cpp/examples/trading/trading.cpp`](cpp/examples/trading/trading.cpp)).
[`examples/trading`](examples/trading/README.md) - book depth on the orderbook engine, orders and
fills on PostgreSQL, market trades on ClickHouse, through one signed map - as a C++ program with the
Python program's commands, arguments and traffic. In the `orderbook` CI job, under both sanitizers,
it provisions the three engines; the reference reads back every row of its run, every field, and
it reads back every row of the reference's; and its telemetry window of the traffic is the
reference's, call for call, but for timings and sizes.

**New: the orderbook adapter in the C++ library** ([`cpp/README.md`](cpp/README.md),
[`docs/orderbook.md`](docs/orderbook.md#from-c)). `sde::OrderbookEngine`, over our engine's own
text protocol on TCP or TLS 1.3, which the library speaks itself, as TypeScript does, in a target of
its own, `sde::orderbook`, over OpenSSL; the engine's library is never linked. It is the reference's
adapter, decision for decision: the fixed shape and key, a single write at level 0, batches as the
engine's updates pipelined 64 at a time, the sequence number left to the server, pages of one book
in key order from windows of time, two rows with one key refused, count and summary refused by
name. Its DSN is read as the reference reads it, compared on 280,000 DSNs, and its words are the
reference's and those of the engine's Python client the reference puts into its own.
- The protocol: the greeting, the AUTH challenge answered with HMAC-SHA256 - the secret never goes
  on the wire - PUSH lines skipped, commands pipelined, and an exchange that does not finish closes
  the connection with its reason for every later call. Every wait is bounded by the DSN's timeout of
  silence. TLS 1.3 at least, the DSN's CA the one trust anchor, the certificate bound to the host by
  name with SNI or by address without it.
- Its tests run against a fake engine and a TLS server in the process, and its slice against the
  pinned `ob_tcp_server`, plain and with credentials and TLS, with one book written here and read by
  the reference, and the other way round.
- Two decisions differed from the reference until #122 brought it to both: a write with no
  connection is refused before anything is sent, and `connect()` opens a new connection after an
  exchange closed the old one. The reference client's IPv4-only sockets and a timeout it accepts and
  cannot wait for are findings in [`docs/implementing.md`](docs/implementing.md#the-orderbook-adapter-over-the-engines-own-protocol).
- Fixed on the way, in the C++ library: Python's `float()` of text, which the DSN's timeout is,
  stripped U+001C to U+001F, which CPython keeps, and read a number past the double range as an
  infinity or zero by the sign of its exponent rather than its size. Neither reached a release.

**Fixed in the C++ library, and found in all three: a ClickHouse 26.5 or later catalogue read as
another design.** From 26.5 ClickHouse keeps the parentheses of a single expression written in
them, so the one-column sort key every library renders as `ORDER BY (id)` reads back `(id)` where
26.4 and earlier read `id` - measured on nine releases from 24.8 to 26.9. Every table the libraries
create there read as differing from its design: a session reported a physical mismatch, and
provisioning, staging and a copy under a new generation refused. The C++ catalogue reader takes one
pair of parentheses around a whole list, and its live suite passes against 26.5 and runs in CI
against a current release beside the LTS. The Python and TypeScript libraries take the same
reading since 0.2.0 (#116); the finding is in [`docs/implementing.md`](docs/implementing.md#the-clickhouse-adapter-against-the-reference-on-one-server).

**New: the ClickHouse adapter in the C++ library** ([`cpp/README.md`](cpp/README.md)).
`sde::ClickHouseEngine`, over ClickHouse's HTTP interface through libcurl, in a target of its own,
`sde::clickhouse`, so an application that places nothing in ClickHouse carries no libcurl. It is the
reference's adapter, method for method: schema with the catalogue's check and the physical design's
findings, writes and batches, logical reads, counts and exact summaries, the forward-only
bookkeeping, the migration operations, write barriers - a constraint and a drain bound to the
table's UUID - storage sizes, and TLS against the CA the URI names, read once and pinned. Its
connection URI is parsed as the reference parses it, rule for rule and message for message,
compared on 102,000 URIs.
- An exchange is one POST on a connection of its own, never reused: a failed one is reported with
  its outcome unknown and never sent again, measured against a proxy that drops the answer to an
  accepted insert. The statement is the body, never part of a URL.
- Rows travel in `RowBinaryWithNamesAndTypes` both ways, so a date of year 1 or 9999 is the date it
  was. A value its column cannot hold is refused before anything is sent. The reference's driver
  stored `Decimal("1.239")` in a `Decimal(12, 2)` as 1.23 - PostgreSQL rounds the same save to 1.24
  - and `Decimal("12345678901.23")` as 12345678901.20, without a word, until §8b point 4 refused a
  value its type does not hold in every library. It still stores an enum name its type does not
  declare as 0, and cuts a moment's fraction a column does not keep, in tables only a hand-written
  map names: recorded in [`docs/implementing.md`](docs/implementing.md).
- Its live tests are the reference's ClickHouse slice, each message captured from the reference
  against the same server; what the reference runs against both engines with one body runs here
  with one body for each adapter built - copies in every direction, write generations, the frozen
  comparison, the bookkeeping, and the two engines agreeing on one value. CI runs them against a
  ClickHouse server at both compiler floors and under the sanitizers.
- Fixed on the way, in the C++ library: a PostgreSQL size the catalogue refused was reported as
  `failed` rather than `refused`, because the session looked for the SQLSTATE in a message that
  never carries it.

**New: the PostgreSQL adapter in the C++ library** ([`cpp/README.md`](cpp/README.md)).
`sde::PostgresEngine`, over libpq, in a target of its own, `sde::postgres`: the core still links no
network library, and an application that places nothing in PostgreSQL carries no libpq. It is the
reference's adapter, method for method - schema with the catalogue's check of columns and types and
the physical findings, writes and batches, logical reads, counts and exact summaries, transactions
with savepoints, the forward-only bookkeeping, the migration operations, native write fences and
storage sizes - and TLS is the DSN's, verified by libpq. With it, `sde::prepare_schema`: what a
person provisioning a map runs before the application opens it, since from contract 4 a session
creates nothing.
- Its live tests are the reference's PostgreSQL slice, ported, and every message they compare was
  captured from the reference against the same server. They run against a real server in CI, and
  fail rather than skip there without one.
- Fixed in the C++ library on the way: `sde::PhysicalFinding::to_string` was declared and defined
  nowhere, so calling it failed to link; `Session::ensure_schema` did not emit
  `sde.schema.physical_mismatch`, which the reference emits there; and `sde::LOG_EVENTS` named two
  events nothing emits and lacked three the adapter does - a test now holds it to the code.

**New: migration participation and signed packets in the C++ library, Tier 2**
([`cpp/README.md`](cpp/README.md)). `sde::backfill` copies a group's rows to the copies its map fans
writes out to, chunk first and marker after, and resumes; `sde::verify` compares them with the
source and confirms every missing row with a point read. `sde::VerificationRequest` binds a
comparison to its request, project, map and group, and `sde::verify_frozen` compares under named
write barriers. `sde::load_cutover_plan`, `sde::load_staging_plan` and `sde::load_index_plan` decode
and authorise the signed packets of §7g, §7h and §7j without executing anything.
- Every `migration/` vector passes, so the library claims Tier 2: the vectors. Which engines it
  reaches is the engines cell of [`docs/implementations.md`](docs/implementations.md), separately,
  and an engine enters it when its adapter round-trips.
- The three packet loaders were compared with the reference's on 102,384 signed packets, made
  by changing every accepted packet vector at each of its paths and in random pairs and signing them
  again. Outside three named classes, recorded in [`docs/implementing.md`](docs/implementing.md),
  the outcome, fingerprint, record and message were the same in every case.
- A staging refusal names a staging. The reference's helpers it shares with the cutover packet say
  "cutover" ("cutover stage_id must be ..."), and no vector pins those messages; this library takes
  TypeScript's wording, and the reference's is a finding.

**New: the session in the C++ library** ([`cpp/README.md`](cpp/README.md)). `sde::Session` opens
against `sde::Engine`s with the reference's checks. It writes and batches, fans writes out to copies
after the commit, reads by key and by plan, and keeps a transaction to one group. It stamps and
checks write generations, holds `sde::WriteFence`s and refuses a signed map that goes backwards. An
adapter's optional abilities are data on the value (`Engine::capabilities()`). The in-memory engine
of the `migration/` vectors is `sde::testing::MemoryEngine`, in a target of its own.
- It passes the session and write stages of `errors/` and the `migration/` cases of fences,
  generations, batches, fan-out and the forward-only check. The tier stays 1 until backfill and
  verification.
- A log sink that throws can no longer fail a map load; the C++ loader called it unguarded.

**Fixed: TypeScript sent each ClickHouse statement in the URL, values included** (TypeScript,
[`docs/engine-connections.md`](docs/engine-connections.md#clickhouse-the-same-connection-intent-in-python-and-typescript)).
A point read's statement holds its key, and a scan's, a count's and a summary's the values they
filter on. A URL is what a proxy or a load balancer between the application and ClickHouse logs, and
the server caps its length (`http_max_uri_size`). The statement is now the request's body, as
Python's driver sends it. An INSERT's rows stay the body, under a statement in the URL that names the
table and the columns and holds no value. Nothing the server answers or stores changes.
- Measured on ClickHouse 24.8 and 26.9 in the server's own log of request URIs: a get, a scan and
  a count put their value in three URLs before, and in none now. Python's put it in none.
- The port of the adapter to C++ found it.

**Tightened: one rule for a read value written as text** (both libraries,
[`docs/logical-reads.md`](docs/logical-reads.md#values-written-as-text)). A decimal or a timestamp
given as text in a filter, a bound or a position is read alike in every library, with a vector for
each case (`query/024`-`028`):
- around a decimal, Unicode's `White_Space` is accepted and nothing else. Python stops stripping
  U+001C-U+001F; TypeScript stops stripping U+FEFF and starts stripping U+0085;
- a timestamp keeps ISO 8601's ranges in every part. Python refuses an offset part past 59, which
  it used to fold into the total (`+10:60` was eleven hours), and `24:00`, which CPython 3.14 reads
  as the next midnight.

**Tightened: a model and a map have the shapes the contract writes** (both libraries,
[`docs/format-contract.md`](docs/format-contract.md) §4a, §7, §8a). Writing the C++ library found
values the two libraries read two ways, or failed on with their runtime's own error. Each is refused
now, alike in all three, with a vector (`errors/084`-`112` and `121`, `routing/004`):
- in a declaration: `nullable` that is not a boolean; `residency` that is not a string; a relation
  that is not three strings; atomic groups that overlap, repeat a member or have one; a
  `cost_ceiling` that is not exactly `{amount, currency}` strings, from either front door;
- in a map:
  - a materialisation that is not an object, or whose `id` or `engine` is not a string;
  - a lag budget that is not a non-negative integer;
  - layout tables and columns that are not names and types;
  - `derived` that is not a list, and `groups` that is empty;
  - `routing` that is a non-empty non-object;
  - an uppercase `project_id`;
  - from contract 4, an integer past 2^53 - 1;
  - a signed map with no canonical form, now a map error rather than the encoder's.
- in routing: a write shape routed at a copy, which the control plane already refused to issue;
- a copy in the source's engine reusing its tables is refused without a model too, and Python and
  TypeScript refuse it at one point (§8a);
- a signature value is base64 as Python 3.12 reads it. TypeScript decoded any value and refused
  later, as a signature that does not verify.

A hand-written map or declaration that relied on a coercion is refused with the rule it breaks.
Every runner compares a vector's `match` literally, as §10 says; four write-stage cases had been
written for regular expressions.

**New: values, DDL and read plans in the C++ library, the first part of Tier 2**
([`cpp/README.md`](cpp/README.md)). `sde::Value` holds one host type per neutral type, with an exact
`sde::Decimal` and microsecond timestamps. `sde::schema_statements` renders a layout's DDL and
`sde::compatibility_views` the views on a target. `sde::plan_read` checks a read and normalises its
values; `sde::numeric_summary` computes an exact summary, with the mean rounded half to even.
- It passes every `schema/` and `query/` vector; the library stays at Tier 1 until it takes part in
  a migration.
- Its answers were compared with the reference's own on 240,000 random inputs. The comparison
  found text the two other libraries read in two ways: the whitespace around a decimal, an offset
  part past 59, and 24:00. Each now has one rule, held by `query/024`-`028`, which this library
  takes as well.
- `sde::QueryRefused` is a `sde::ModelPlanningError`, as in the reference, and `sde::schema_is_fixed`
  refuses a dialect it does not know rather than answering `false`.

**Fixed: a PostgreSQL DSN that only mentioned `connect_timeout` lost the connect bound** (Python,
[`docs/failure-semantics.md`](docs/failure-semantics.md)). The 10-second default was applied only
when the text `connect_timeout` was absent from the DSN, so `?application_name=connect_timeout_probe`
left a host that accepts and stays silent holding the call: still waiting after 20 seconds. The
adapter now reads the DSN's keys as libpq does, and a `connect_timeout` key still wins.

**Changed: an operation a newer map overtook says where and which versions.** A staging, a cutover
or an index build that finds a newer map on one of its engines refused with "a newer map was
adopted" and nothing else, so an operator had to read this library to find which engine held which
map. The product loop of a move onto Elasticsearch met it on a test cluster carrying another
stream of maps. The refusal now names the engine binding, the version it records and the version
the operation was prepared from - the control plane's numbers, never the client's data - and on
Elasticsearch adds that a cluster keeps one stream of maps whatever project wrote them (format
contract 7m). After a durable decision the same refusal is the cause beneath the error that asks
for a resume.

**New: a group's size on Elasticsearch.** `Session.measure_storage()` / `measureStorage()` read
nothing there, so a window of a group on Elasticsearch never knew its `total_bytes`, its
`index_to_table_ratio` or its growth, and a decision to move it off had no evidence of what the copy
would cost. The product loop of a move onto Elasticsearch found it. Both adapters now read each
index's bytes on its primary shards from `_stats` - one request per engine, with
`ignore_unavailable`, so a missing index is absent rather than a zero and an alias is never measured
under its name. An index keeps no secondary index beside its store, so the ratio is a measured 0.
- **It needs the index privilege `monitor`**, an optional grant in the key's role
  ([`docs/runtime-roles.md`](docs/runtime-roles.md)). Without it the answer is a 403, and the size is
  unknown with the reason `refused`, as on ClickHouse without its `system.parts` grant. The local
  operator admits it.
- The adapter's error carries the status beneath it (`HttpStatus`), which is how the session tells
  `refused` from `failed`; never the engine's text.
- A size from fewer primary shards than the index's `number_of_shards` is refused (second review).
  An unassigned primary counts as neither a success nor a failure in `_shards`, so the sum came
  back short of it with nothing to say so.

**Fixed: a float32 reads as the float32 it is, from every engine and in both languages.**
PostgreSQL's text for a `real` - and ClickHouse's JSON, which the TypeScript adapter reads - is the
shortest decimal that rounds back to the stored value: `0.1` for the float32 nearest 0.1. Parsed,
that is another double than the float32 itself, `0.10000000149011612`, which the Python ClickHouse
adapter and both Elasticsearch adapters return. So one stored value read differently by engine, and
a group with a float32 column never verified between two of them. The first copy from PostgreSQL to
Elasticsearch found it.
- Python's PostgreSQL adapter and TypeScript's PostgreSQL and ClickHouse adapters - reads by key
  and TypeScript's ClickHouse scans alike - now return the float32 exactly. Stored values are
  unchanged; a read that returned `0.1` returns `0.10000000149011612`.
- Exactly, rather than the shortest decimal most of them gave, because every library can widen alike
  and only the engines' own formatter prints alike: PostgreSQL and ClickHouse print 2^87 as
  `1.5474251e+26`, where the first precision that round-trips gives `1.54742505e+26`.
- The agreement tests write `0.1` to a float32 column, not `0.5`, which every engine read alike.

**New: the local operator moves a group onto Elasticsearch and off it** - breaking for maps
written since #134, which no release contained. A group there carries a write generation: the
library keeps the write-fence protocol in the index's own final pipeline, with a write block and a
refresh as the drain ([`docs/write-fences.md`](docs/write-fences.md)). Staging, the frozen
comparison and the cutover then take it like a group on any other engine
([`docs/local-cutover.md`](docs/local-cutover.md#elasticsearch-access-by-blocks)).
- **Access moves by blocks, not grants.** An API key's privileges are fixed when it is made, so
  nothing is revoked from one login. The side a decision retires is blocked for reads for good and
  keeps the holds that block its writes, against the runtime, an unknown login and the
  administrator's own search alike. The pause refuses writes and leaves the frozen data readable.
- **Runtime keys are qualified through `_has_privileges`.** No `manage`, `all`, `write`,
  `delete_index` or `delete` on their indexes, no cluster privilege that rewrites a fence,
  `read_pipeline`, and the fresh `sde_m_*` names covered in advance, because the operator grants
  nothing. A key holds `index`, not `write`: a delete runs no ingest pipeline, where an index or an
  update runs the final one (measured on 9.5.5), so a key that may delete goes past the fence.
  A role can also name an action and none of those privileges. So the operator asks by action, too,
  and refuses the update of an index's settings, which lifts a block. It also refuses the cluster
  actions that write or delete a pipeline, restore a snapshot, or write a role, a role mapping, a
  user or a password. The operator's own login reads the cluster's identity (`monitor`).
- **Both adapters copy and compare in key order** (`key_range`, `nth_key`, `count`, `copy_in`,
  backfill markers in `sde_backfill_state`), so `backfill` and `verify` work between Elasticsearch
  and the SQL engines in both languages. A copy is `create`: a document already there stays.
- `sde_map_state` and `sde_backfill_state` are read before anything creates them, so a runtime key
  needs no `create_index`, and one under another mapping is refused.
- A staging's index carries its creation marker in `_meta.sde.stage`, which every reader accepts
  (§7m). A relayout and an in-place index build on Elasticsearch are refused by name.
- A contract-6 map that leaves the generation out for an Elasticsearch group is now refused, as for
  every engine with fences. No released version wrote one, and such a map is issued again with
  one.
- Both libraries render the same fence pipeline, pinned by `conformance/fence-processors.json`, and
  a fence one library provisions is read by the other in the live tests.
- **Fixed after a second review, in both languages**
  ([`docs/write-fences.md`](docs/write-fences.md)):
  - Releasing the last hold lifts the block first, then rewrites the pipeline. In the other order
    an interruption between the two requests left a block with no hold to say whose it was. No
    retry lifted it, a resume refused, and the index of a published map stayed closed to its
    writes. What is left now is the hold, still refusing every write a runtime key can make, which
    a retry or a resume removes.
  - A fence whose pipeline is not the index's `final_pipeline` enforces nothing, and read as in
    force. It reads as an interrupted setup now, closed, and `resume_prepare` sets the pipeline
    again. A pipeline-level `on_failure` would index every write the processor refuses, so a
    pipeline carrying one is refused.
  - Elasticsearch answers an `if_version` conflict with a 400 reading "version conflict", not a
    409, so another executor's change to a fence came back as an engine error. Both are refused as
    another executor's.

**New: Elasticsearch in the TypeScript library** (`@smart-data-engines/sde/engines/elasticsearch`).
The same engine in the same layout as the Python adapter, with the mapping and the documents held
byte for byte by the shared vectors. Integers past 2^53 stay exact in both directions: responses are
parsed with them as `bigint` and requests write a `bigint` as its digits. A live slice writes with
one library and reads with the other, both ways. `searchEngine` lets an adapter refuse a search by
name, as the orderbook engine now does in both languages.

**New: `examples/book-stream/`**, Python and TypeScript. One exchange feed is sent to the orderbook
engine whole and as its changes. The book the engine composes is read back at every tenth step and
compared with the feed's own. Both halves send the same updates, and the `orderbook` CI job runs them.

**The orderbook engine's pin moves to `31b4390`**, which has its #244 and #245. In local mode:
- `flush()` keeps the engine open, where it closed it and opened it again.
- A flush after a write syncs before its checkpoint: 2.48-2.74 ms median after one row, on an
  i3-7100U.
- A process that ends without a flush or a close loses none of its writes, because the engine
  replays its WAL when it opens.

**New: Elasticsearch in the Python library** ([`docs/format-contract.md` §7m](docs/format-contract.md#7m-elasticsearch-an-index-per-entity-a-document-per-row)).
`sde.engines.elasticsearch.ElasticsearchEngine` holds a group in Elasticsearch 9: an index per
entity with a strict mapping, a document per row under its key's digest, writes, reads by key,
bounded scans, exact counts and `Session.search` from a text index of the library's own.
- Every value comes back as written: microseconds in a `long` for both instants, a decimal of up
  to eighteen digits times `10^scale`. `bytes`, `json` and wider decimals are refused at layout.
- The tokens a search matches are computed by the library and kept beside each text field, so the
  engine's analyzer decides nothing: `Session.search` reads only the rows that hold the tokens.
- No transactions, no summaries and no write generations: a group placed here is not moved.
- The transport is the standard library's, with TLS verified against `ca_cert`; plain HTTP is
  accepted to a loopback address only. `make es-up` starts one with its own CA and prints the DSN.
- `conformance/vectors/documents/` is the suite's thirteenth family: a row's `_id` and `_source`.
  `schema/019` and `020` pin the mapping. The TypeScript library renders both byte for byte.
- **Fixed after a second review, in both languages** (§7m):
  - A decimal no longer depends on the caller's decimal context. At a precision of ten, Python
    stored an eighteen-digit value rounded, and two keys shared one `_id`.
  - A `float` field keeps the float32 the engine indexes, so a filter and a read agree. A value
    past the largest float32 is refused before sending.
  - A search, a page, a count and the watermark refuse an answer some shard did not contribute to.
  - An index's whole mapping is compared, not only each field's type. A `normalizer` or a disabled
    `_source` changed answers while every type matched.
  - A token longer than one term and a lone surrogate are refused before sending, and a refused
    write names the engine's error type, never a reason that can quote the value.
  - A decimal bound with more digits than the column is answered as PostgreSQL answers it.
  - TypeScript keeps the sign of `-0.0`, and prints no credentials through `inspect` or `JSON`.
  - Both languages refuse the same DSNs. TypeScript had connected `:secret@` without the secret.
  - `documents/009` to `013` pin the float32, the token, the negative key and the surrogate.

**New: search, Python and TypeScript** ([`docs/logical-reads.md`](docs/logical-reads.md#search)).
`Session.search` returns the rows whose chosen text fields hold every token of a query, a page in
key order. The library applies one rule to every row - tokens of letters and digits after NFC,
matched without case one code point for one - so the answer is the same on every engine.
- On PostgreSQL and ClickHouse the library reads rows in key order and checks each one, at most
  `max_examined` rows a call. An engine's own `lower()` or collation never decides an answer.
- A search always reads the group's source. A map that routes a `search` shape is refused at load:
  a library that predates the kind refuses it as an unknown shape, so this one refuses it too.
- §6 enumerates one `search` shape for each entity with a `string` field, and the C++ library
  enumerates it as well. The window reports `examined` and `searched_on` for it, never the text.
- `conformance/vectors/search/` is the suite's twelfth family, with `routing/003`, `errors/083` and
  `telemetry/017` beside it, and [`format-contract.md` §7l](docs/format-contract.md#7l-search)
  states the rule. Neither the IR's number nor the map's moves: `model/002`-`004` gained their
  `search` shapes in `shapes.json`, and every existing identifier is unchanged.
- `QueryRefused` in TypeScript declares `name` as a `string`, so that `SearchRefused` can extend it.

**New: book streams and books by price, Python and TypeScript** ([`docs/orderbook.md`](docs/orderbook.md#books-by-price)).
`Session.book_stream` sends one book of the orderbook engine either whole, each side as `MREPLACE`,
or as its changes, with a whole side at the first send, after a failed one and at every resync.
`Session.book_at` reads the book the engine composes from them at an instant.
- Both need an orderbook engine that reads updates by price and announces `replace_side` (its #233),
  and refuse one that does not before sending or reading anything. The pinned engine is one.
- `conformance/vectors/books/` is the suite's eleventh family, and
  [`format-contract.md` §8c](docs/format-contract.md#8c-a-book-stream) states its rules. Neither the
  IR's number nor the map's moves: no IR and no map contains what a stream sends.
- `Session.save_many` writes updates by price on such an engine too: an update of a book's top
  levels leaves in it the prices that dropped out of them, unless it removes them with a quantity of 0.

**New: telemetry in the C++ library, Tier 1** ([`cpp/README.md`](cpp/README.md)). `sde::Recorder`
measures operations by shape and rolls windows, and `Window::as_record` writes the §6a document; every
`telemetry/` vector passes. The recorder takes no lock on an operation's path: a window is a block of
atomic counters, swapped at a roll that waits for the writes begun on the old block. Its concurrency
is tested under ThreadSanitizer in CI.

**New: a C++ library, Tier 0 with hashing** ([`cpp/README.md`](cpp/README.md)). It declares models
and computes their versions and shapes, reads and writes the neutral declaration, loads placement
maps of contracts 1 to 6 with their signatures and key sets, and routes operations. It has no
engine adapters yet. It is built with CMake from this repository (GCC 12 or Clang 16 at the oldest,
OpenSSL 3, utf8proc) and is on no package registry.
- It passes every vector of its tier.
- Where the other two libraries coerce a value or fail with their runtime's own error, it refuses
  with the contract's. Those inputs are listed in
  [`docs/implementing.md`](docs/implementing.md#the-third-implementation-c), to become rules in every
  library.
- Four required checks join the ruleset, nineteen in all: `cpp (gcc-12)`, `cpp (clang-16)`,
  `cpp-sanitizers` and CodeQL's `analyze (c-cpp)`.

## `smart-data-engine-sdk` 0.2.0 and `@smart-data-engines/sde` 0.2.0

Published on 10 October 2026, each tag on its first run of the release workflow, with attestations
on PyPI and provenance on npm. On npm it is `latest`.

Our own L2 orderbook engine becomes the third engine of both libraries, under placement map
contract 6. On the way there, a design evaluation and a general test of the whole product on a test
host found defects that are fixed here:
- rows the model forbids reached the engines;
- the library's reads used no index on a text column;
- the local operator refused to work beside a ClickHouse administrator.

Both libraries also work with ClickHouse 26.5 and later, where the released ones refused a table
with a one-column key right after creating it.

**Why 0.2.0 and not a patch.** Both libraries change things a caller depends on. The version number
has to say so to version ranges: npm's `^0.1.0` and pip's `~=0.1.1` both stop below 0.2.0, so an
application moves on when it decides to (§5.1 of [`docs/publishing.md`](docs/publishing.md)). What
changes for a caller:
- **A row the model does not allow is refused before any engine.** PostgreSQL used to store NULL in a
  required field, and ClickHouse stored `0` or `""` for one left out. Such a write now raises.
- **`sde.fixed_schema_mismatch` takes a required `nullable` argument** (Python).
- **A contract-6 map is refused by 0.1.x**, and a group on the orderbook engine needs one.
- **PostgreSQL tables are created with text columns `COLLATE "C"`.** A table created by 0.1.x keeps
  its collation, and Python names it with the remedy.
- **The operator counts a ClickHouse administrator only with the whole `ACCESS MANAGEMENT` group**
  (Python). A partial revoke of anything in it leaves no administrator, on 24.8 too, where such a
  user was admitted before.

**Fixed: ClickHouse 26.5 and later** (both libraries; the operator's two are Python's). From 26.5
the server reports three things in a new form, and each reader took the new form for something
else. Measured on 24.8, 26.5 and 26.9, and the key on nine releases:
- **A one-column key.** The libraries create it as `ORDER BY (id)`, and `system.tables.sorting_key`
  reads it back as `(id)`, where 24.8 to 26.4 read `id`; a list of two or more reads `a, b` on all of
  them. The catalogue reader refused the parentheses, so every table with a one-column key read as
  another design. Provisioning refused even the table it had just created, and so did everything
  that verifies a design: staging, a cutover, an index build. A running session reported it. Now
  one pair of parentheses around the whole of a key, an index's expression or a partition key is
  read as what is inside it, and only a list of names, or one function of one name, may be there.
- **An index built in place** (`sde-operator index`). The server records the materialization as
  `(MATERIALIZE INDEX <name>)` where 24.8 records `MATERIALIZE INDEX <name>`, and the operator looked
  for the second. So on 26.5 a build never saw its materialization finish: it waited out the signed
  budget and asked for recovery, and every resume started another materialization. Removing an
  index left an unfinished materialization of it running. Both forms are matched now.
- **An administrator beside the operator** (staging and cutover,
  [`docs/runtime-roles.md`](docs/runtime-roles.md#the-operators-login-and-the-principals-beside-it)).
  A user with a direct global `ACCESS MANAGEMENT` is admitted there. From 26.5 the server lists that
  grant member by member, because seven of its members are granted per user name, and the operator
  looked for the single row of 24.8. So on a current server it found no administrator and refused
  every staging and cutover beside one, naming the server's own `default` user. Now the whole group
  counts, as `system.privileges` defines it, however it is listed. A partial revoke of anything in it
  leaves no administrator, on 24.8 too, where such a user was admitted before.

CI runs the live slices of both libraries against ClickHouse 26.9 as well as the 24.8 LTS, on the
newest Python and the newest Node. The first was found by a checkpoint of the C++ library's engine
adapters against a current server, and the other two by running this suite against one.

**Fixed: the local operator beside the engine's administrator** (Python,
[`docs/runtime-roles.md`](docs/runtime-roles.md#the-operators-login-and-the-principals-beside-it)).
On ClickHouse the operator refused every operation when the server's administrator was not its own
login. It took the administrator's global grants for an undeclared grantee of the tables.
- A user with a direct global `ACCESS MANAGEMENT` is now admitted, as a PostgreSQL superuser always
  was and as `docs/local-cutover.md` promised.
- An in-place index build no longer refuses any other grantee, because it moves no authority.
- Staging and cutover still refuse anyone else covering a table, a role or a monitoring login with
  `SELECT ON *.*` among them. The refusal now names the principal and its grants.

A general test on 2 October 2026 found it: the first build was refused beside the administrator.
No test had run an operator that was not the administrator; now staging, cutover and a build do.

**Fixed: the library's own reads use the engines' indexes** (both libraries,
[`docs/format-contract.md`](docs/format-contract.md) §7a). On PostgreSQL every scan, count and
summary compares text as `("x" COLLATE "C")`, but the library created text columns in the default
collation, and every index on them with it, primary keys included. The planner uses an index only in
the predicate's own collation, so none of them served a read. Measured on 200 000 rows, a scan of one
station of a `(station, at)` key read the whole table in 27 ms; now it reads the key in 0.103 ms. On
ClickHouse a UUID equality was `toString(id) = ...` and read 25 of 25 granules; now it compares
natively and reads 1. A general test found it: a B-tree an agent decided, and the operator built
under traffic, left the scans it was decided for at 131 ms, before it and after it.
- PostgreSQL text columns are created `COLLATE "C"`. So is a text column in every index: built at
  provisioning, in place, or on a staged copy. Six `schema/` vectors carry the new bytes. Maps, model
  versions and signatures do not change.
- A table created before this release keeps its collation. An index built on it now serves the
  reads, but its primary key still cannot. Python logs `sde.schema.text_collation` for the primary key
  and for each declared index in that state, with the remedy. Either stage a fresh copy, or have an
  administrator run `ALTER COLUMN ... TYPE text COLLATE "C"`, which holds an exclusive lock on the
  table while its indexes rebuild.
- An in-place build resumed after an upgrade builds again an index of its own that an earlier library
  left without the collation.
- `python/tests/test_index_use_live.py` and `typescript/tests/index-use.live.test.ts` ask each engine's
  planner for the plan of the exact statement the library sent. Point `get` already used plain
  equality on both engines and was not affected.

**Fixed: a row the model does not allow is refused before any engine** (both libraries,
[`docs/format-contract.md`](docs/format-contract.md) §8b). `Session.save` refuses a field its entity
does not declare, a required field left out, and a required field given null. In TypeScript
`undefined` is refused like null. `Session.save_many` / `saveMany` refuses a null in a required field,
naming the row. A field is required when it is declared without `nullable`, and a key field always is.
Until now the engines answered for the library, each its own way, measured on PostgreSQL 15 and
ClickHouse 24.8:
- PostgreSQL stored NULL in a required field;
- ClickHouse stored a value nobody wrote: `0` for an integer left out, `""` for a string;
- ClickHouse's driver refused a null with a message about a column type.

A new `write` stage in the shared error vectors, `errors/078`-`082`, holds the refusal in both
runners. In each, one accepted write comes first, and the refused one must reach no engine. With
hashed identifiers, Python's refusal of an undeclared field now reads `declares no field`, as in
TypeScript. Rows stored before this release are unchanged.

**Changed: which fields may be null is part of the orderbook shape.** `sde.fixed_schema_mismatch`
takes a required `nullable` argument, which `sde.group_nullable(model, group)` derives. A model
declares `sequence_number` nullable and no other field (`sde.ORDERBOOK_NULLABLE`), and
`default_layout` refuses anything else. Both kinds of mismatch used to pass and then could not be
written, on an engine a group can never move off:
- a nullable `quantity`;
- a required `sequence_number`, which the server assigns.

The engine facts say so: `fixed_shape.nullable` and `fixed_shape.assigned_by_server`. A caller of
`fixed_schema_mismatch` must now pass `nullable`, and a call without it fails rather than returning
half an answer.

**Fixed: an orderbook read refuses a value outside the model's types** (both libraries). An engine
before `c1f14c0` read a stored quantity of 2^60 - 1 back as 2^64 - 1, and the adapters returned it as
an `int64`. Now the read is refused, naming the field.

**The orderbook adapter over TCP** (`smart-data-engine-sdk`, [`docs/orderbook.md`](docs/orderbook.md)).
- **Credentials, TLS and a timeout**, and an `orderbook://` DSN.
- **A chosen sequence number is refused before sending.** The server numbers every update itself.
  The current server refuses a chosen number, and an older one discarded it, so the row read back
  disagreed with the row written.
- **`Session.save_many` writes whole updates.**
- **`Session.scan` pages one book in key order** although the engine answers in arrival order.
- **`count` and `summarize` are refused by name.**
- **Values the engine cannot store are refused with the field named**, before anything is sent.

A new CI job, `orderbook`, builds the engine at a pinned commit and runs every orderbook slice
in-process and over TCP, plain and with credentials and TLS.

**Placement map contract 6: a group without a write generation** (both libraries,
[`docs/format-contract.md`](docs/format-contract.md) §7k).
- **A group may leave `write_epoch` out** when its engine cannot fence writes, as the orderbook
  engine cannot. Such a group has only a source, and `null` is not absence. A contract-5 library
  refuses the whole document.
- **A session checks the engine against the map, both ways.** It refuses such a group on an engine
  with write fences, and a group with a generation on an engine without them. Its writes carry no
  generation column, and the column stays reserved.
- **No staging, cutover or in-place index build acts on such a group**, and each carries it
  unchanged. The local operator needs no binding for its engine and refuses one.
- New vectors: `errors/074`-`077`, `migration/188`-`199`, `signature/015`. `errors/006` now declares
  contract 7.

**The orderbook adapter in TypeScript** (`@smart-data-engines/sde/engines/orderbook`,
[`docs/orderbook.md`](docs/orderbook.md)). It speaks the engine's TCP protocol itself, with
credentials and TLS, and makes the reference's decisions with its refusals. That covers whole
updates in batches of 64, pages in key order from an engine that answers in arrival order, the
server's sequence number, and count and summarize refused by name. A TypeScript session can now
open on a map with an orderbook group. As in Python, a count's or a summary's refusal now comes from
the adapter's own reason (`countRefusal`, `summaryRefusal`), before the call is timed.

**A trading firm's application on three engines** ([`examples/trading/`](examples/trading/)). Depth
on the orderbook, orders and fills on PostgreSQL and trades on ClickHouse, from one contract-6 map.
It provisions, writes and verifies every row from Python, and its TypeScript half reads what the
Python half wrote. CI runs it on all three engines.

**The orderbook engine pin is `9f55e84`**, past the engine's #198. A quantity of exactly 2^60 - 1
used to read back as 2^64 - 1, and every later quantity in its segment as 0. Both libraries' slices
now read the edges of the quantity range back.

**`sde.engine_facts(dialect)`** (Python): what each engine can and cannot do, as data. It covers the
schema (derived, or the fixed orderbook shape), transactions, the key, write generations, the write
unit, the reads, and what an orderbook scan needs. The control plane gives it to the model that
decides placement. Each fact is tested against its adapter and engine.

## `smart-data-engine-sdk` 0.1.1

Published on 28 September 2026, on the first run of `python-v0.1.1`, with attestations.

A patch release of the Python library alone. `@smart-data-engines/sde` has no 0.1.1. Of what it
ships, only the version field has changed since 0.1.0, to a development version. One tag per
language keeps an otherwise identical package from going out under a new number (§5.1 of
[`docs/publishing.md`](docs/publishing.md)).

**Fixed.** Resume and abandonment of a PostgreSQL in-place index build
used to read the catalogue while the interrupted statement was still running. A deadline or a
killed operator ends the client, not its `CREATE INDEX CONCURRENTLY`. Recovery then either raced
that statement's commit and failed with "tuple concurrently updated", or dropped the index it was
finishing. Both now wait for the statement to end, then keep what it finished
([`docs/in-place-index.md`](docs/in-place-index.md)).

## `smart-data-engine-sdk` 0.1.0 and `@smart-data-engines/sde` 0.1.0

Published on 27 September 2026, each tag on its first run of the release workflow, with attestations
on PyPI and provenance on npm. On npm it is `latest`.

The first final release. The libraries are the release candidates below: apart from the version
itself, nothing in `python/src`, `typescript/src` or `typescript/bin` changed after `python-v0.1.0rc1`
and `typescript-v0.1.0-rc.1`.
The candidates were verified from the registries before this number was set:
- the registries' artefacts are the ones the release gate checked;
- the PyPI package runs the library's suite;
- the npm package's signatures and provenance verify;
- both compute the same model version;
- the Weather starter ran end to end from the registries.

Two things changed around the libraries:
- the release workflow checks the registry for longer than its cache (#95), and says why npm
  refused a publish if it does (#96);
- the documents describe a released SDK (#96).

## `smart-data-engine-sdk` 0.1.0rc1 and `@smart-data-engines/sde` 0.1.0-rc.1

Published on 27 September 2026. These are the first release candidates published through the
release workflow, with attestations on PyPI and provenance on npm. They cover everything since
`0.1.0.dev0`, the development release that claimed the PyPI name on 12 September 2026.

**Runtimes.** Python 3.11 to 3.14 and Node 18 to 26, every version tested in CI
([`docs/platforms.md`](docs/platforms.md)).

**Placement maps and moving data.**
- Map contract 5 carries a physical design for each group: key order, a time partition, and indexes
  from a closed vocabulary. It is checked against the engine's own catalogue
  ([`docs/physical-design.md`](docs/physical-design.md)).
- Write generations are enforced by the engine. Migration verification is bound to the project, the
  immutable map and the group.
- The local operator, `sde-operator`, handles all copy and index work:
  - it executes and recovers signed cutovers;
  - it stages fresh copies, also within one engine;
  - it builds, removes and replaces indexes in place, without a copy or a write pause;
  - it abandons a staging that cannot finish.

**Sessions.**
- Both languages have bounded batch writes, point reads, keyset scans, counts and exact numeric
  summaries.
- Session and transaction ownership.
- Verified TLS to both engines ([`docs/engine-connections.md`](docs/engine-connections.md)).
- Values keep their precision. Timestamps keep their microseconds in TypeScript reads and in
  ClickHouse parameters from Python, and ClickHouse JSON numbers stay exact.

**Telemetry.** The window document reports three things:
- each operation's shape;
- the fields each read filtered on, by name only;
- the group's size, index share, daily growth, time-filtered share and write burstiness
  (`Session.measure_storage()` / `session.measureStorage()`), as numbers only.

**The Weather starter.** `sde-weather` (Python) and `sde-weather-ts` make up a local, recoverable
customer starter ([`docs/weather-starter.md`](docs/weather-starter.md)):
- restricted runtime credentials;
- point, analytics, fleet and alerts workloads;
- operator handoffs;
- an ownership-checked reset.

**Releasing.** A release is a per-language tag, behind a gate, artefact checks and OIDC publishing
with no stored credential. The npm dist-tag is chosen from what the registry already holds.

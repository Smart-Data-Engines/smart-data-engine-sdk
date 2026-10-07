# SDE for C++

The C++ library of Smart Data Engine: declare a logical model, load the placement map the control
plane issues - or one you wrote by hand - and ask where each operation goes. It is the third
implementation of [the format contract](../docs/format-contract.md), after Python (the reference)
and TypeScript, and it is held to the same shared vectors in [`conformance/`](../conformance) as
they are.

**Status: Tier 2 and hashing, with the PostgreSQL, ClickHouse and orderbook adapters** ([`format-contract.md` §9](../docs/format-contract.md#9-capability-tiers)).

| | |
|---|---|
| Model, IR, `model_version`, colocation groups, operation shapes | yes |
| The neutral declaration (§4a), both ways | yes |
| Placement maps, contracts 1 to 6: signatures and key sets, write generations, physical design, groups without a generation | yes |
| Routing (§8) | yes |
| Name hashing (§2a) | yes |
| Telemetry (Tier 1): the recorder, windows and the window document of §6a | yes |
| Values, the DDL of §7a, read plans and exact summaries (Tier 2, the part without engines) | yes |
| Sessions (Tier 2): writes and batches, fan-out to copies, point and logical reads, transactions, write generations, write fences and the forward-only check, against an engine interface with an in-memory engine | yes |
| Migration participation (Tier 2): backfill, verification and verification requests, the comparison under write barriers, and the signed cutover, staging and index build packets | yes |
| Schema preparation: what a person provisioning a map runs before the application opens it | yes |
| PostgreSQL (Tier 2), over libpq: schema, reads, writes, transactions, bookkeeping, migration, write fences, sizes, TLS | yes |
| ClickHouse (Tier 2), over libcurl: schema and the physical design, reads, writes and batches, bookkeeping, migration, write fences, sizes, TLS | yes |
| The orderbook engine (Tier 2), over its own protocol on TCP or TLS 1.3: the fixed shape checked, writes as updates, batches, reads in key order, credentials, TLS | yes |

Tier 2 is the vectors of §9 - `schema/`, `query/` and `migration/` - and this library passes all of
them against the engine interface and its in-memory engine. The engines are separate: PostgreSQL is
`sde::PostgresEngine`, in a target of its own, `sde::postgres`, which is the only part that links
libpq, and ClickHouse is `sde::ClickHouseEngine`, in `sde::clickhouse`, the only part that links
libcurl, and the orderbook engine is `sde::OrderbookEngine`, in `sde::orderbook`, which speaks the
engine's protocol itself and links OpenSSL's TLS - the core links no network library, and an
application carries only the adapters it links. The recorder reads a monotonic clock, or the one you
give it, and that is the only state kept between calls.

## Requirements

- CMake 3.20 or newer.
- A C++20 compiler: **GCC 12** or **Clang 16** at the oldest. CI builds with both floors, every
  warning an error, and runs the suite under AddressSanitizer and UndefinedBehaviorSanitizer with
  the newest Clang.
- OpenSSL 3 (`libcrypto`): SHA-256, HMAC-SHA256 and Ed25519.
- utf8proc: NFC and case mapping.
- libpq, for the PostgreSQL adapter only. `-DSDE_POSTGRES=OFF` builds without it, and without the
  adapter.
- libcurl 7.77 or newer, for the ClickHouse adapter only. `-DSDE_CLICKHOUSE=OFF` builds without it,
  and without the adapter.
- OpenSSL 3's `libssl`, for the orderbook adapter only, which speaks the engine's protocol itself and
  never links the engine. `-DSDE_ORDERBOOK=OFF` builds without the adapter.
- GoogleTest, for the tests only. An installed one is used; otherwise CMake fetches 1.15.2, pinned by
  its archive's SHA-256.

On Debian and Ubuntu:

```bash
sudo apt-get install cmake ninja-build libssl-dev libutf8proc-dev libpq-dev libcurl4-openssl-dev
```

## Build and test

```bash
cmake -S cpp -B cpp/build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build cpp/build
ctest --test-dir cpp/build
```

`-DSDE_WERROR=ON` turns warnings into errors, as CI does. `-DSDE_SANITIZE=address,undefined` builds
with both sanitizers. The same three builds are presets, run from `cpp/`:
`cmake --preset debug`, then `cmake --build --preset debug` and `ctest --preset debug`, and likewise
`release` and `sanitizers`. The tests read the vectors in place from `conformance/vectors`, and a vector
family this suite has never heard of fails the build's tests rather than passing unread.

The live tests run the adapters against real servers, labelled `live`:

```bash
SDE_POSTGRES_DSN=postgresql://postgres:sde@127.0.0.1:55432/sde \
SDE_CLICKHOUSE_DSN=clickhouse://default:sde@127.0.0.1:58123/sde \
  ctest --test-dir cpp/build -L live
```

`make engines-up` starts both servers. Without a DSN each live test is skipped and says so; with
`CI=true` it fails instead, because a green job that ran nothing looks exactly like one that passed.
Each test works in a schema - in ClickHouse, a database - of its own, with a runtime login of its own
where it needs one, and drops both. The TLS tests need no server: they talk to a certificate witness the suite runs itself.
So do the orderbook adapter's own tests, against a fake engine in the process that speaks its
protocol. Against a real `ob_tcp_server` - plain, and one with `--auth-secret-file` and
`--tls-client` - the slice is labelled `orderbook`, and `SDE_PYTHON` names an interpreter with the
reference and the engine's Python client for the half where the reference reads what this library
wrote:

```bash
SDE_ORDERBOOK_TCP=127.0.0.1:59090 \
SDE_ORDERBOOK_SECURE_DSN='orderbook://identity:secret@localhost:59091?tls=on&ca=/path/ca.pem' \
SDE_PYTHON=python3 \
  ctest --test-dir cpp/build -L orderbook
```

## Use it

From a checkout, with `add_subdirectory(cpp)`, or installed:

```bash
cmake --install cpp/build --prefix /opt/sde
```

```cmake
find_package(sde CONFIG REQUIRED)
target_link_libraries(your_app PRIVATE sde::sde)
```

This library is not on any package registry yet; it is built from this repository. A model, a map
and a route, from [`examples/route.cpp`](examples/route.cpp), which the test suite builds and runs:

```cpp
const sde::Model model =
    sde::ModelBuilder{}
        .entity({"Order", {{"id", "uuid"}, {"placed", "timestamptz"}, {"total", "decimal(12,2)"}},
                 {"id"}})
        .entity({"Fill", {{"id", "uuid"}, {"order_id", "uuid"}, {"qty", "int64"}}, {"id"}})
        .relation("order", "Fill", "Order")
        .build();

sde::LoadOptions options;
options.model = &model;  // a signed map would also need options.public_keys
const sde::PlacementMap map = sde::load_map(map_text, options);

for (const sde::OperationShape& shape : model.shapes()) {
  const sde::Materialization& copy = sde::resolve(map, shape);
  // copy.engine, copy.layout.table_for(shape.entity), ...
}
```

Against PostgreSQL, link `sde::postgres` as well. From contract 4 a session creates nothing, so a
person provisioning the map prepares the schema first, on a login that may issue DDL, and the
application opens its session on a runtime login that needs only its tables:

```cpp
sde::PostgresEngine provisioning(provisioning_dsn);
provisioning.connect();
sde::prepare_schema(model, map, {{"pg", &provisioning}}, project_id);

sde::PostgresEngine engine(runtime_dsn);  // a DSN or URI; sslmode and sslrootcert as libpq reads them
engine.connect();
sde::SessionOptions options;
options.project_id = project_id;  // the local enrollment's, never learned from the map
sde::Session session(model, map, {{"pg", &engine}}, options);
session.save("Order", {{"id", *sde::Uuid::parse(id)}, {"placed", now}, {"total", sde::Decimal("12.50")}});
```

Against ClickHouse, link `sde::clickhouse`, and the same holds. Its connection is a URI - the
profile of [`docs/engine-connections.md`](../docs/engine-connections.md), shared with the other two
libraries - and a runtime login needs `SELECT, INSERT` on its tables; `SELECT` on
`system.data_skipping_indices` to verify a design's indexes rather than report them unverified, and
on the size columns of `system.parts` for a session to measure its group:

```cpp
sde::ClickHouseEngine engine(  // https or clickhouses; ca_cert names the CA, read once
    "clickhouses://app:secret@ch.internal:8443/analytics?ca_cert=%2Fetc%2Fsde%2Fca.pem");
engine.connect();
sde::Session session(model, map, {{"ch", &engine}}, options);
```

Against our orderbook engine, link `sde::orderbook`. Its schema is fixed in the engine, so a group
placed there either is that shape (`sde::ORDERBOOK_SHAPE`) or cannot be placed there, and nothing is
provisioned. Its connection is `orderbook://[identity:secret@]host:port[?tls=on&ca=PATH&timeout=S]`,
read as the reference reads it; the secret answers the server's challenge and never goes on the
wire:

```cpp
sde::OrderbookEngine engine("orderbook://desk:secret@ob.internal:59091?tls=on&ca=%2Fetc%2Fsde%2Fca.pem");
engine.connect();  // refuses a server that cannot store a write's event time
sde::Session session(model, map, {{"ob", &engine}});
session.save_many("DepthLevel", rows);  // the rows of one side of one book at one instant are one update
```

A map with no signature is valid: that is the no-account mode, documented and supported. A signed
map needs the keys it may be verified with - `sde::PublicKeys::bare(key)` for one, or
`sde::PublicKeys::named({{"k1", key1}, {"k2", key2}})` during a rotation, in which case
`map.verified_with()` says which one verified it. There is no expiry anywhere in this library: a map
keeps working for as long as its keys are configured.

A whole application on all three engines is [`examples/trading/trading.cpp`](examples/trading/trading.cpp):
[`examples/trading`](../examples/trading/README.md) - a desk's book depth, market trades, orders and
fills - in C++, with the same commands, arguments and traffic as the Python program there, so either
one verifies the other's runs. It declares its model with `sde::ModelBuilder`, and `declare` prints
the neutral declaration the control plane's `declare` takes - `examples/trading/model.json`,
document for document. It reads the signed map and its keys from files and each engine's DSN from
the environment; it links `sde::postgres`,
`sde::clickhouse` and `sde::orderbook`, and OpenSSL for the name-based UUIDs of its traffic. This
build makes it `examples/sde_example_trading`, and so does a client build of `cpp/examples` against
the installed package. The SDK's `orderbook` CI job runs it under both sanitizers against the three
engines: it provisions them, the reference reads back every row of its run and it reads back every
row of the reference's, and its telemetry window of the traffic is the reference's
(`python/tests/test_orderbook_trading_example.py`).

## What to expect from it

- **Every refusal is an exception of the contract's classes**: `sde::DeclarationError` for a model,
  `sde::MapError` for a map, `sde::CanonicalError` for a value with no canonical form, all derived
  from `sde::SdeError`, which is a `std::runtime_error`. Their messages carry the reference's
  wording, and a value interpolated into one is written the way Python's `repr` writes it, so one
  defect reads the same in all three libraries.
- **No value of yours is in what the library says.** A log event, a telemetry window and a refusal
  the library makes name entities and fields, never a row's value; `tests/unit/test_invariants.cpp`
  searches all three for values put in on purpose. What an engine says back is passed on in the
  engine's words, and can quote what the engine holds - that text is the engine's, to its own
  client, and stays in your process. The same file holds the other invariants: a map's parser reads
  no date, the wall clock is read once, to stamp a verification report, and an unsigned map costs no
  table and no query; and CTest reads the core's archive for a network symbol and finds none
  (`invariant.*`).
- **A `Model` and a `PlacementMap` are immutable once built.** Share them between threads freely.
  Only `load_map` makes a `PlacementMap`, so holding one means every rule of §7 was checked.
- **`sde::Recorder` takes no lock on an operation's path.** A window is a block of per-shape
  slots of atomic counters, and `roll()` swaps the block, waits for the writes begun on the old one
  and reads it: a write racing a roll lands in the next window, never half in one. Recording never
  throws; what it cannot record it drops and counts (`rejected()`). The window document is
  `Window::as_record(model)`, the same §6a document the other two libraries write, number for number.
- **A value has one host type per neutral type** (`sde/value.hpp`): `std::int64_t` for both integer
  types, `double` for both floats, `std::string` for text, and `sde::Decimal`, `sde::Bytes`,
  `sde::Uuid`, `sde::Date`, `sde::Timestamp` (no zone) and `sde::TimestampTz` (an instant) for the
  rest, all in one `sde::Value`. A decimal is exact, compared by value and written at the scale it
  was given (`1.10` stays `1.10`); it holds up to 1,000 digits, more than any engine here stores.
  Timestamps are microseconds between years 1 and 9999.
- **DDL is a value** (`sde/schema.hpp`): `sde::schema_statements(layout, keys, dialect)` returns the
  statements that create a layout, byte for byte the reference's, and running nothing is the answer
  for a fixed-schema engine (`sde::schema_is_fixed`).
- **A read is planned before any engine is called** (`sde/query.hpp`): `sde::plan_read` checks the
  fields and normalises every value to its column's type, refusing with `sde::QueryRefused` - a
  `sde::ModelPlanningError` - as the reference does, message for message. A page limit is an
  `sde::PageLimit`, which no `bool` converts to, so `limit = true` does not compile where the
  reference refuses it at run time (`query/012`). `sde::numeric_summary` decodes an engine's summary
  exactly, however wide the total, and rounds the mean half to even.
- **An engine is an `sde::Engine`, and what it can do besides is data on the value**
  (`sde/engine.hpp`). `capabilities()` returns which optional interfaces the adapter implements -
  logical reads, counts, summaries, batches, the forward-only bookkeeping, migration, write fences,
  a schema check, storage sizes - and an empty pointer means it does not take part, which a session
  reports by name rather than discovering at a call.
- **A session is opened, checked, and then routes** (`sde/session.hpp`). Its constructor refuses a
  map naming an engine nobody supplied, a fan-out that would truncate a value between two dialects,
  write generations the engines do not enforce, and a signed map older than one the engines have
  seen; an unsigned map costs no call at all. A write reaches the source, then every copy the map
  fans out to - after the commit, inside a transaction - and a copy's failure is logged and counted,
  never the caller's. A transaction is one group's, refused when it would span two. One thread uses
  a session at a time, and a second thread is refused (`sde::ResourceBusy`) rather than raced. The
  model, the map and the engines must outlive it.
- **A migration is taken part in, never decided** (`sde/migration.hpp`, `sde/verification.hpp`).
  `sde::backfill` copies a group's rows to the copies its map fans writes out to, a chunk at a time
  in key order: the chunk first, then the marker that counts it, so a crash between the two costs a
  recopy and never a lost chunk, and the next call resumes. `sde::verify` compares the copies with
  the source - in chunks below the marker, and above it by the rows a copy is missing - and confirms
  every row missing from a copy with a point read before reporting it. A `sde::VerificationRequest` binds a comparison
  to the control plane's request, the locally configured project, the map's fingerprint and the
  group, and `sde::verify_frozen` compares under named write barriers it checks before and after.
- **Signed packets are decoded and authorised, never executed** (`sde/packets.hpp`).
  `sde::load_cutover_plan`, `sde::load_staging_plan` and `sde::load_index_plan` check every rule of
  §7g, §7h and §7j against the model, the project and the keys, and give an operator the maps and
  index definitions it acts on and the exact bytes it publishes. A plan exists only as loaded.
- **An in-memory engine for tests** (`sde/testing/memory.hpp`, target `sde::testing`): the engine
  the `migration/` vectors run against, which records every call in one sequence across engines.
  Link it in your adapter's tests, never in production.
- **Nothing is logged unless you pass a sink** (`LoadOptions::log`, `SessionOptions::log`), and a
  sink that throws cannot fail an operation. Events are named from the
  reference's closed vocabulary (`sde.map.loaded`, `sde.map.rejected`) and carry structure, never a
  row's values.
- **The PostgreSQL adapter reports a failure and never works around it.** A failed write carries the
  server's message as psycopg writes it, and `sde.write.failed` the class psycopg would raise; nothing
  is retried, rerouted or reconnected. Opening a connection is bounded - ten seconds unless the DSN's
  `connect_timeout` says otherwise - text is UTF-8 whatever the DSN says, and a server's notices are
  dropped rather than printed. One thread uses an adapter at a time: a second is refused
  (`sde::ResourceBusy`), and an adapter inherited across `fork` refuses everything
  (`sde::ResourceClosed`).
- **The ClickHouse adapter never sends anything twice.** An exchange is one HTTP POST on a
  connection of its own, never reused, so a failed one is reported with its outcome unknown and is
  not retried - measured with a proxy that drops the answer to an insert the server accepted: the
  row is there once. The statement is the request's body, so no value of a row is ever part of a URL
  a proxy would log. Opening is bounded by the URI's `connect_timeout` (ten seconds), and every
  exchange by `send_receive_timeout` (fifteen) of silence - a long query keeps the connection alive
  with progress headers. There are no transactions, and `transaction` refuses before its body runs.
  Rows travel in `RowBinaryWithNamesAndTypes` both ways, so a date of year 1 or 9999 is the date it
  was, and a value its column cannot hold - an integer out of range, a decimal or a moment with more
  digits than the column keeps, an enum name its type does not declare - is refused before
  anything is sent, where the reference's driver changes it silently
  ([`docs/implementing.md`](../docs/implementing.md#the-third-implementation-c)). Every read of a
  table reads `FINAL`. A write barrier is a constraint and a drain - the table detached and attached
  again, in an Atomic database, after a durable intent naming its UUID - so an interrupted drain is
  resumed by name and attaches that table and no other.
- **The orderbook adapter states what the engine does not promise rather than smoothing it over**,
  as the reference does. There are no transactions; the key is not enforced, so a read meeting two
  rows with one key refuses rather than picks one; a write is an update of N levels, so a single row
  is level 0 or refused and `save_many` writes whole updates, pipelined 64 at a time; the server
  numbers every update, so a row's `sequence_number` is null when written and the server's when
  read; the engine answers in arrival order, so a page is assembled from windows of time read whole
  and sorted; it counts nothing over its history, so a count and a summary are refused by name. A
  read flushes first when something was written. The protocol is spoken here, its words those of
  the engine's own client: an exchange that does not finish closes the connection, every later call
  says why, and `connect()` opens another; every wait is bounded by the DSN's timeout of silence;
  TLS is 1.3 at least, the DSN's CA the one trust anchor, the certificate bound to the host by name
  or by address. No write of this library raises SIGPIPE.
- **It is stricter than the two other libraries in a few places** where they coerce a value or fail
  with their runtime's own error: a materialisation's `id` and `engine` must be strings, a
  `lag_budget_ms` a non-negative integral number, a layout's tables and columns names and types,
  `derived` a list. Those differences are written down as findings in
  [`docs/implementing.md`](../docs/implementing.md#the-third-implementation-c), to be made the
  contract's rule in every library rather than this one's.

## Agreement that the vectors cannot reach

Some of what this library does has to agree with the reference character for character, and the
vectors do not exercise it. An auto layout's table names are the model's entity names in
`snake_case`, after a full Unicode lowercase. A signature's value is base64 as Python 3.12 decodes
it. Both were compared with the reference directly, and agreed everywhere:
- the lowercase on all 1,112,064 scalar code points against Python's `str.lower()`;
- the final sigma on 300,000 generated strings against Python and JavaScript;
- `snake_case` on 100,025 names against Python's and the TypeScript library's;
- base64 on 449,593 strings, every one up to six characters over a hostile alphabet among them;
- the whole message of every model- and map-stage refusal in `errors/`, all 76: identical to the
  reference's, apart from one function name spelled the C++ way;
- the read planner's value rules and the exact summary on 240,000 random inputs against the
  reference's `sde.query` - decimal text up to and past libmpdec's exponent limits, ISO timestamps
  with every part in and out of range, dates, UUIDs, and summaries with totals of up to 40 digits:
  identical, message for message, once the reference took the rules of `query/024`-`028`. Before
  that, every difference was one of those two rules, and they are the reason the vectors exist;
- the ClickHouse connection URI on 102,000 generated URIs against the reference's parser: the same
  outcome, the same message and every field equal, the timeouts to the bit;
- the orderbook DSN on 280,000 generated DSNs against the reference's parser, the same way, its
  timeout read as Python's `float()` reads text - underscores, any Unicode digit, an infinity - and
  every number of an engine's answer read as `int()` reads it, checked against CPython;
- the three packet loaders on 102,384 signed packets, made by changing every accepted packet
  vector at each of its paths and in random pairs, and signing them again so that each change reaches
  the rule it is about: the same outcome, fingerprint, record and message in every case except three
  named classes - this library's stricter map loader, the reference failing with its runtime's own
  error, and integers past 64 bits, which §1 requires this library to refuse.

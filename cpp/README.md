# SDE for C++

The C++ library of Smart Data Engine: declare a logical model, load the placement map the control
plane issues - or one you wrote by hand - and ask where each operation goes. It is the third
implementation of [the format contract](../docs/format-contract.md), after Python (the reference)
and TypeScript, and it is held to the same shared vectors in [`conformance/`](../conformance) as
they are.

**Status: Tier 1 and hashing** ([`format-contract.md` §9](../docs/format-contract.md#9-capability-tiers)).

| | |
|---|---|
| Model, IR, `model_version`, colocation groups, operation shapes | yes |
| The neutral declaration (§4a), both ways | yes |
| Placement maps, contracts 1 to 6: signatures and key sets, write generations, physical design, groups without a generation | yes |
| Routing (§8) | yes |
| Name hashing (§2a) | yes |
| Telemetry (Tier 1): the recorder, windows and the window document of §6a | yes |
| Engines - PostgreSQL, ClickHouse, the orderbook engine (Tier 2) | not yet |

Until Tier 2, this library reads, checks and measures; it does not connect to anything. It makes no
network call. The recorder reads a monotonic clock, or the one you give it, and that is the only
state kept between calls.

## Requirements

- CMake 3.20 or newer.
- A C++20 compiler: **GCC 12** or **Clang 16** at the oldest. CI builds with both floors, every
  warning an error, and runs the suite under AddressSanitizer and UndefinedBehaviorSanitizer with
  the newest Clang.
- OpenSSL 3 (`libcrypto`): SHA-256, HMAC-SHA256 and Ed25519.
- utf8proc: NFC and case mapping.
- GoogleTest, for the tests only. An installed one is used; otherwise CMake fetches 1.15.2, pinned by
  its archive's SHA-256.

On Debian and Ubuntu:

```bash
sudo apt-get install cmake ninja-build libssl-dev libutf8proc-dev
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

A map with no signature is valid: that is the no-account mode, documented and supported. A signed
map needs the keys it may be verified with - `sde::PublicKeys::bare(key)` for one, or
`sde::PublicKeys::named({{"k1", key1}, {"k2", key2}})` during a rotation, in which case
`map.verified_with()` says which one verified it. There is no expiry anywhere in this library: a map
keeps working for as long as its keys are configured.

## What to expect from it

- **Every refusal is an exception of the contract's classes**: `sde::DeclarationError` for a model,
  `sde::MapError` for a map, `sde::CanonicalError` for a value with no canonical form, all derived
  from `sde::SdeError`, which is a `std::runtime_error`. Their messages carry the reference's
  wording, and a value interpolated into one is written the way Python's `repr` writes it, so one
  defect reads the same in all three libraries.
- **A `Model` and a `PlacementMap` are immutable once built.** Share them between threads freely.
  Only `load_map` makes a `PlacementMap`, so holding one means every rule of §7 was checked.
- **`sde::Recorder` takes no lock on an operation's path.** A window is a block of per-shape
  slots of atomic counters, and `roll()` swaps the block, waits for the writes begun on the old one
  and reads it: a write racing a roll lands in the next window, never half in one. Recording never
  throws; what it cannot record it drops and counts (`rejected()`). The window document is
  `Window::as_record(model)`, the same §6a document the other two libraries write, number for number.
- **Nothing is logged unless you pass a sink** (`LoadOptions::log`). Events are named from the
  reference's closed vocabulary (`sde.map.loaded`, `sde.map.rejected`) and carry structure, never a
  row's values.
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
  reference's, apart from one function name spelled the C++ way.

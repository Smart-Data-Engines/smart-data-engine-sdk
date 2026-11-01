#pragma once

#include <array>
#include <string_view>

namespace sde {

/// The library's own version, `0.1.0-dev` on a development build. It names a build, not the
/// contract: what makes two libraries agree is the conformance suite, and the contract versions a
/// library implements are separate constants (`IR_CONTRACT`, `MAP_CONTRACT`).
[[nodiscard]] std::string_view version() noexcept;

/// The highest capability tier this library passes (format contract section 9). Tier 0 is the
/// model, its version and shapes, map parsing, signatures, routing and the error semantics; Tier 1
/// adds telemetry; Tier 2 schema creation, logical reads and summaries, and taking part in a
/// migration. Which engines a library round-trips is a separate claim, made per adapter.
/// docs/implementations.md states the same number, and a test there holds the two together.
inline constexpr int TIER = 2;

/// Whether this library offers name hashing (section 2a). A mode rather than a tier: a library that
/// offers it must pass every `hashing/` vector, whatever its tier.
inline constexpr bool HASHING = true;

/// Whether this library offers `Session.search` (section 7l). Three Tier 2 families go with a
/// capability as well as the tier (section 9), and `search/` goes with this one.
inline constexpr bool SEARCH = false;

/// Whether this library offers book streams (section 8c), which need the orderbook engine:
/// `books/` goes with this capability (section 9). `sde/books.hpp`, and `Session::book_stream` and
/// `Session::book_at` over the orderbook adapter.
inline constexpr bool BOOK_STREAMS = true;

/// The engines this library has an adapter for, by dialect: `documents/` goes with the
/// Elasticsearch engine (section 9). The engines cell of docs/implementations.md names the same
/// three, which a test there holds. The orderbook adapter reads and writes a book; book streams are
/// `BOOK_STREAMS`, a separate claim.
inline constexpr std::array<std::string_view, 3> ADAPTERS{"clickhouse", "orderbook", "postgres"};

}  // namespace sde

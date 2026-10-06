#pragma once

#include <string_view>

namespace sde {

/// The library's own version, `0.1.0-dev` on a development build. It names a build, not the
/// contract: what makes two libraries agree is the conformance suite, and the contract versions a
/// library implements are separate constants (`IR_CONTRACT`, `MAP_CONTRACT`).
[[nodiscard]] std::string_view version() noexcept;

/// The highest capability tier this library passes (format contract section 9). Tier 0 is the
/// model, its version and shapes, map parsing, signatures, routing and the error semantics; Tier 1
/// adds telemetry, Tier 2 the engines. docs/implementations.md states the same number, and a test
/// there holds the two together.
inline constexpr int TIER = 1;

/// Whether this library offers name hashing (section 2a). A mode rather than a tier: a library that
/// offers it must pass every `hashing/` vector, whatever its tier.
inline constexpr bool HASHING = true;

}  // namespace sde

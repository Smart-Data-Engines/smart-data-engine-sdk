#pragma once

#include <string_view>

namespace sde {

/// The library's own version, `0.1.0-dev` on a development build. It names a build, not the
/// contract: what makes two libraries agree is the conformance suite, and the contract versions a
/// library implements are separate constants (`IR_CONTRACT`, `MAP_CONTRACT`).
[[nodiscard]] std::string_view version() noexcept;

}  // namespace sde

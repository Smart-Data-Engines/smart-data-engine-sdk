#pragma once

#include <optional>
#include <string>
#include <string_view>

namespace sde::detail {

/// Standard base64 (RFC 4648, with padding). Decoding is strict: an alphabet character outside the
/// standard set, missing or misplaced padding, or nonzero trailing bits all mean "not base64".
[[nodiscard]] std::optional<std::string> base64_decode(std::string_view text);
[[nodiscard]] std::string base64_encode(std::string_view bytes);

/// Lowercase hexadecimal, and its inverse, which accepts either case.
[[nodiscard]] std::string hex_encode(std::string_view bytes);
[[nodiscard]] std::optional<std::string> hex_decode(std::string_view text);

}  // namespace sde::detail

#pragma once

#include <optional>
#include <string>
#include <string_view>

namespace sde::detail {

/// Standard base64 (RFC 4648, with padding). Decoding is strict: an alphabet character outside the
/// standard set, missing or misplaced padding, or nonzero trailing bits all mean "not base64".
[[nodiscard]] std::optional<std::string> base64_decode(std::string_view text);

/// Base64 as Python 3.12's `base64.b64decode(text, validate=True)` reads it - how the reference
/// decodes a map's signature. The standard alphabet and nothing else: no whitespace, no padding at
/// the start, between data, or short of the end of a quantum. But padding after a whole quantum,
/// and bits under the padding that are not zero, are accepted: a signature both references accept
/// must not be refused here.
[[nodiscard]] std::optional<std::string> base64_decode_as_python(std::string_view text);
[[nodiscard]] std::string base64_encode(std::string_view bytes);

/// Lowercase hexadecimal, and its inverse, which accepts either case.
[[nodiscard]] std::string hex_encode(std::string_view bytes);
[[nodiscard]] std::optional<std::string> hex_decode(std::string_view text);

}  // namespace sde::detail

#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>

namespace sde::detail {

/// The length of the well-formed UTF-8 sequence starting at `pos`, or 0 when the bytes there are
/// not one: an overlong form, a surrogate (U+D800-U+DFFF), a value above U+10FFFF, a truncated or
/// stray continuation byte.
[[nodiscard]] std::size_t utf8_sequence_length(std::string_view text, std::size_t pos) noexcept;

/// Appends the code point's UTF-8 form, surrogates included: a JSON escape of a lone surrogate keeps
/// its three bytes so that a reader can refuse it by name (they are not UTF-8, deliberately).
void append_code_point_unchecked(std::string& out, std::uint32_t code_point);

}  // namespace sde::detail

#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
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

/// The code point of the well-formed sequence of `length` bytes at `pos` (see above).
[[nodiscard]] char32_t decode_sequence(std::string_view text, std::size_t pos,
                                       std::size_t length) noexcept;

/// The code points of well-formed UTF-8, or nothing when it is not.
[[nodiscard]] std::optional<std::u32string> decode_utf8(std::string_view text);

/// Whether the code point has Unicode's `White_Space` property: U+0009 to U+000D, U+0020, U+0085,
/// U+00A0, U+1680, U+2000 to U+200A, U+2028, U+2029, U+202F, U+205F and U+3000. Neither Python's
/// `str.isspace()` (which adds U+001C to U+001F) nor JavaScript's `trim()` (which adds U+FEFF and
/// lacks U+0085): the three libraries' own stripping disagreed, so the rule names the property.
[[nodiscard]] bool is_white_space(char32_t code_point) noexcept;

/// The text without `White_Space` at either end. Stops at an ill-formed byte, which is not space.
[[nodiscard]] std::string_view strip_white_space(std::string_view text) noexcept;

/// Whether the code point is a decimal digit (general category Nd): what `\d` matches in a Python
/// regular expression over text, which is more than `[0-9]`.
[[nodiscard]] bool is_decimal_digit(char32_t code_point) noexcept;

}  // namespace sde::detail

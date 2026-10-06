#pragma once

/// The Unicode questions the format contract asks, and nothing more: is this text, what is its
/// NFC form, how does it sort, and what is its lowercase (which auto layouts' table names are).

#include <string>
#include <string_view>

namespace sde {

/// Whether the bytes are well-formed UTF-8 encoding Unicode scalar values only - no surrogate, no
/// overlong form, nothing above U+10FFFF. Section 7f refuses a map string that is not.
[[nodiscard]] bool is_scalar_text(std::string_view text) noexcept;

/// The NFC form of the text. Refuses (`CanonicalError`) text that is not scalar UTF-8, because
/// normalising bytes that are not text would invent characters.
[[nodiscard]] std::string nfc(std::string_view text);

/// Whether the text is scalar UTF-8 and already in NFC.
[[nodiscard]] bool is_nfc(std::string_view text);

/// The default lowercase of the text, with the full case mapping: what Python's `str.lower()` and
/// JavaScript's `toLowerCase()` give. U+0130 becomes `i` followed by U+0307, and a capital sigma
/// that ends a word becomes the final form. An auto layout's table names are this lowercase
/// (`snake_case`), so a library lowercasing any other way would read and write another table.
/// Refuses (`CanonicalError`) text that is not scalar UTF-8.
[[nodiscard]] std::string to_lower(std::string_view text);

/// Code point order. On well-formed UTF-8 this is byte order, which is the property that makes
/// UTF-8 the convenient encoding for this contract - and the reason not to reach for a locale-aware
/// or UTF-16 comparison here (`canonical/001`, `model/004`).
[[nodiscard]] int compare_code_points(std::string_view left, std::string_view right) noexcept;

/// `compare_code_points(left, right) < 0`, for sorting.
struct CodePointLess {
  [[nodiscard]] bool operator()(std::string_view left, std::string_view right) const noexcept {
    return compare_code_points(left, right) < 0;
  }
};

}  // namespace sde

#pragma once

/// The three Unicode questions the format contract asks, and nothing more.

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

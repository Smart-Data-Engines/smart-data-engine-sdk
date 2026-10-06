#pragma once

/// The reference library's way of writing and reading values, where the contract depends on it.
///
/// Two things, both about agreeing with Python rather than about Python:
/// - **diagnostics.** A vector's `match` is a fragment of the reference's message, and a message
///   that interpolates a value writes it the way Python's `repr` does. Writing it the same way here
///   makes every message read alike in all three libraries, not only the fragments a vector pins.
/// - **JSON numbers.** From map contract 4 an integral number is an integer however it is spelled
///   (section 7d: `2.0` is `2` before signing and fingerprinting). Which spellings are integral is
///   decided by the double Python's `float()` makes of them, so it is decided the same way here.

#include <string>
#include <string_view>
#include <vector>

#include "sde/json.hpp"

namespace sde::detail {

/// `repr()` of the value Python's `json.loads` makes of this JSON: `None`, `True`, `'text'`,
/// `[1, 'a']`, `{'k': 2.5}`.
[[nodiscard]] std::string python_repr(const Json& value);
/// `repr()` of a string: single quotes unless it holds a single quote and no double one, `\n`,
/// `\t`, `\r`, `\\` and the quote escaped, and every character Python does not print as `\x..`,
/// `\u....` or `\U........`.
[[nodiscard]] std::string python_repr(std::string_view text);
// Without these a `std::string` or a literal would convert to both `std::string_view` and `Json`.
[[nodiscard]] inline std::string python_repr(const std::string& text) {
  return python_repr(std::string_view(text));
}
[[nodiscard]] inline std::string python_repr(const char* text) {
  return python_repr(std::string_view(text));
}
/// `repr()` of a list of strings.
[[nodiscard]] std::string python_repr(const std::vector<std::string>& texts);

/// `type(value).__name__`: `NoneType`, `bool`, `int`, `float`, `str`, `list`, `dict`.
[[nodiscard]] std::string python_type_name(const Json& value);

/// `bool(value)`: null, false, zero, and empty strings, arrays and objects are false.
[[nodiscard]] bool python_truthy(const Json& value) noexcept;

/// `float(lexeme)` of a JSON number: correctly rounded, infinite past the double range and zero
/// below it, as Python's parser makes it (C++'s `from_chars` reports both as an error instead).
[[nodiscard]] double python_float(std::string_view lexeme) noexcept;

/// `repr(float)`: the shortest digits that read back as the same double, in fixed notation from
/// 1e-4 up to 1e16 with `.0` on an integral value, and as `1e+16`, `1.5e-05` outside it.
[[nodiscard]] std::string python_float_repr(double value);

/// The exact decimal digits of a finite integral double, however large: `int(1e20)` is
/// `100000000000000000000`.
[[nodiscard]] std::string integral_double_digits(double value);

/// Map contract 4's `json_numbers`: every number whose value is integral becomes an integer
/// lexeme - `2.0`, `2e0` and `20e-1` all become `2` - and every other number is left as written,
/// for the canonical encoder to refuse.
[[nodiscard]] Json integral_numbers(const Json& value);

}  // namespace sde::detail

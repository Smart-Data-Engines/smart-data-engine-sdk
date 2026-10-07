#pragma once

/// Values to and from PostgreSQL's text format: what a parameter is sent as, and what a result
/// cell is, from the type the server sent with it. Pure functions, so they are tested without a
/// server.

#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "sde/value.hpp"

namespace sde::detail::postgres {

/// Type OIDs this adapter converts. Fixed for built-in types (`pg_type.oid`).
namespace oid {
inline constexpr unsigned kBool = 16;
inline constexpr unsigned kBytea = 17;
inline constexpr unsigned kInt8 = 20;
inline constexpr unsigned kInt2 = 21;
inline constexpr unsigned kInt4 = 23;
inline constexpr unsigned kJson = 114;
inline constexpr unsigned kFloat4 = 700;
inline constexpr unsigned kFloat8 = 701;
inline constexpr unsigned kDate = 1082;
inline constexpr unsigned kTimestamp = 1114;
inline constexpr unsigned kTimestamptz = 1184;
inline constexpr unsigned kNumeric = 1700;
inline constexpr unsigned kUuid = 2950;
inline constexpr unsigned kJsonb = 3802;
}  // namespace oid

/// A value as a text parameter, nothing for NULL; the server infers the type from where the
/// parameter stands. Refuses (`EngineError`) text holding a NUL, which no PostgreSQL text can hold
/// and which libpq, reading C strings, would silently cut.
[[nodiscard]] std::optional<std::string> parameter_text(const Value& value);

/// A result cell as the value it is, from the type the server sent. Dates and timestamps are read
/// in the ISO style, which the caller makes sure of. A value this library does not represent -
/// `infinity`, a date before year 1 or after 9999, a numeric `NaN` - is refused (`EngineError`)
/// rather than approximated; any type not listed comes back as its text.
[[nodiscard]] Value cell_value(std::string_view text, unsigned type);

/// A PostgreSQL array literal of text, for `= ANY($1::text[])` and `unnest($1::text[])`: every
/// element quoted, with `"` and `\` escaped inside the quotes.
[[nodiscard]] std::string text_array(const std::vector<std::string>& items);

}  // namespace sde::detail::postgres

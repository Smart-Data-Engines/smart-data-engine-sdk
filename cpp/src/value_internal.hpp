#pragma once

/// The parsing and calendar arithmetic behind `sde/value.hpp`, shared with the read planner, which
/// has to answer for decimal text exactly as the reference does - including the text Python's
/// `Decimal` refuses before the planner's own rules are reached.

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>

#include "sde/value.hpp"

namespace sde::detail {

inline constexpr std::int64_t kMicrosPerSecond = 1'000'000;
inline constexpr std::int64_t kMicrosPerDay = 86'400 * kMicrosPerSecond;
/// 0001-01-01 and 9999-12-31, in days from 1970-01-01.
inline constexpr std::int64_t kMinDays = -719'162;
inline constexpr std::int64_t kMaxDays = 2'932'896;
/// 0001-01-01T00:00:00 and 9999-12-31T23:59:59.999999, in microseconds from the epoch.
inline constexpr std::int64_t kMinMicros = kMinDays * kMicrosPerDay;
inline constexpr std::int64_t kMaxMicros = (kMaxDays + 1) * kMicrosPerDay - 1;

/// An exponent's magnitude is read up to this and no further. Every exponent past it is one
/// Python's `Decimal` refuses whatever the digits around it, so the clamp changes no answer.
inline constexpr std::int64_t kExponentClamp = 4'000'000'000'000'000'000;
/// libmpdec's limits on a 64-bit build, which is what CPython's `decimal` is: text whose adjusted
/// exponent is above `kMaxEmax`, or whose exponent is below `kMinEtiny`, raises `InvalidOperation`
/// on construction. Measured on CPython 3.12: `1e999999999999999999` builds, `1e10^18` does not;
/// `1e-1999999999999999997` builds and one less does not.
inline constexpr std::int64_t kMaxEmax = 999'999'999'999'999'999;
inline constexpr std::int64_t kMinEtiny = -1'999'999'999'999'999'997;

[[nodiscard]] constexpr bool ascii_digit_char(char c) noexcept { return c >= '0' && c <= '9'; }

/// Decimal text taken apart, without allocating:
/// `[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?`, ASCII digits only.
struct DecimalParts {
  bool negative = false;
  std::string_view integer;   ///< digits before the point, possibly empty or all zeros
  std::string_view fraction;  ///< digits after the point, possibly empty
  std::int64_t exponent = 0;  ///< the written exponent, its magnitude clamped at `kExponentClamp`
};

[[nodiscard]] std::optional<DecimalParts> decimal_parts(std::string_view text) noexcept;

/// What Python's `Decimal` would make of the parts, in its terms.
struct DecimalCounts {
  std::int64_t exponent = 0;        ///< `as_tuple().exponent`
  std::int64_t adjusted = 0;        ///< `adjusted()`
  std::int64_t scale = 0;           ///< `max(0, -exponent)`
  std::int64_t integer_digits = 0;  ///< `max(0, adjusted + 1)`
  std::int64_t digits = 0;          ///< `integer_digits + scale`
};

[[nodiscard]] DecimalCounts decimal_counts(const DecimalParts& parts) noexcept;

/// Whether Python's `Decimal(text)` raises `InvalidOperation` for text with these counts.
[[nodiscard]] constexpr bool python_decimal_invalid(const DecimalCounts& counts) noexcept {
  return counts.adjusted > kMaxEmax || counts.exponent < kMinEtiny;
}

/// The decimal the parts denote; empty when it has more than `Decimal::MAX_DIGITS` digits.
[[nodiscard]] std::optional<Decimal> decimal_from_parts(const DecimalParts& parts);

/// A decimal from a coefficient's digits and a scale; leading zeros are dropped.
[[nodiscard]] Decimal make_decimal(bool negative, std::string digits, int scale);

/// Reaches the private state of `Decimal`, for the two functions above.
struct DecimalAccess {
  static void set(Decimal& decimal, bool negative, std::string digits, int scale) {
    decimal.negative_ = negative;
    decimal.digits_ = std::move(digits);
    decimal.scale_ = scale;
  }
};

/// Days from 1970-01-01 of a proleptic Gregorian date; empty for a date that does not exist or a
/// year outside 1 to 9999.
[[nodiscard]] std::optional<std::int64_t> days_from_civil(std::int64_t year, unsigned month,
                                                          unsigned day) noexcept;

struct Civil {
  std::int64_t year;
  unsigned month;
  unsigned day;
};

[[nodiscard]] Civil civil_from_days(std::int64_t days) noexcept;

/// `YYYY-MM-DD[T ]HH:MM:SS[.f{1,6}][Z|+HH:MM[:SS]]`, ASCII digits, every field in range.
struct IsoTimestamp {
  std::int64_t local_micros = 0;              ///< the written wall-clock reading
  std::optional<std::int64_t> offset_seconds;  ///< empty when no zone was written
};

[[nodiscard]] std::optional<IsoTimestamp> parse_iso_timestamp(std::string_view text) noexcept;

[[nodiscard]] bool micros_in_range(std::int64_t micros) noexcept;

/// `YYYY-MM-DDTHH:MM:SS.ffffff` for microseconds from the epoch.
[[nodiscard]] std::string format_micros(std::int64_t micros);

}  // namespace sde::detail

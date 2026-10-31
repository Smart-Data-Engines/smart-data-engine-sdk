#pragma once

/// Values (Tier 2): one host type per type of the neutral vocabulary (format contract section 3),
/// so a value reaches an engine without passing through a type that cannot hold it.
///
/// | Neutral type      | Host type                                                     |
/// |-------------------|---------------------------------------------------------------|
/// | `bool`            | `bool`                                                        |
/// | `int32`, `int64`  | `std::int64_t`                                                |
/// | `float32`, `float64` | `double`                                                   |
/// | `decimal(p,s)`    | `sde::Decimal`: exact, as text, never a binary float          |
/// | `string`          | `std::string`, UTF-8                                          |
/// | `bytes`           | `sde::Bytes`                                                  |
/// | `uuid`            | `sde::Uuid`                                                   |
/// | `date`            | `sde::Date`: days from 1970-01-01                             |
/// | `timestamp`       | `sde::Timestamp`: microseconds, no zone                       |
/// | `timestamptz`     | `sde::TimestampTz`: microseconds since the epoch, UTC         |
/// | `json`            | `sde::JsonDocument`                                           |
///
/// A row is `sde::Row`: field name to value, in code point order of the names.
///
/// **A row's value takes a form its field's type admits, and reaches the engine in one form per
/// type** (section 8b, point 4). Before any engine is called the session refuses a value its field's
/// type does not hold - a decimal past its scale or precision, an integer outside int32, a date that
/// does not exist - and passes each value on in the form a read plan (`sde/query.hpp`) gives the same
/// value: a decimal at its column's scale, a timestamp as its UTC instant, a UUID as `sde::Uuid`.
/// The engines had answered for such values each in its own way, and one row meant two things.

#include <array>
#include <chrono>
#include <compare>
#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <variant>
#include <vector>

#include "sde/json.hpp"

namespace sde {

namespace detail {
struct DecimalAccess;
}  // namespace detail

/// An exact decimal number: a sign, a coefficient and a scale (digits after the point).
///
/// Compared by value, like Python's `Decimal`: `1.10 == 1.1`, and `-0 == 0`. The text keeps what it
/// was given - `1.10` stays `1.10` and `-0` stays `-0` - because the scale is what an engine stores.
class Decimal {
 public:
  /// The most digits a decimal holds, integer and fraction together. No engine this library places
  /// data in stores more: PostgreSQL's widest declared `numeric` has precision 1000.
  static constexpr std::size_t MAX_DIGITS = 1000;

  /// Zero, at scale 0.
  Decimal() = default;
  /// Parses decimal text: an optional sign, digits with an optional fraction, an optional exponent
  /// (`-12.50`, `.5`, `1e-3`). Throws `std::invalid_argument` for anything else - no whitespace, no
  /// `NaN` or `Infinity` - and for a value of more than `MAX_DIGITS` digits.
  explicit Decimal(std::string_view text);
  /// An integer, at scale 0.
  explicit Decimal(std::int64_t value);

  /// Like the constructor, without the exception.
  [[nodiscard]] static std::optional<Decimal> parse(std::string_view text) noexcept;

  [[nodiscard]] bool negative() const noexcept { return negative_; }
  [[nodiscard]] bool is_zero() const noexcept { return digits_ == "0"; }
  /// The coefficient's digits, without leading zeros (`0` for zero).
  [[nodiscard]] const std::string& coefficient() const noexcept { return digits_; }
  /// Digits after the decimal point.
  [[nodiscard]] int scale() const noexcept { return scale_; }
  /// Digits before the decimal point, not counting leading zeros: 0 for `0.5`.
  [[nodiscard]] int integer_digits() const noexcept;

  /// Fixed-point text, never an exponent: `-12.50`, `0.001`, `1000` for `1e3`. The same text as
  /// Python's `format(value, "f")`.
  [[nodiscard]] std::string to_string() const;

  friend bool operator==(const Decimal& left, const Decimal& right) noexcept;
  friend std::strong_ordering operator<=>(const Decimal& left, const Decimal& right) noexcept;

 private:
  friend struct detail::DecimalAccess;

  bool negative_ = false;
  std::string digits_ = "0";
  int scale_ = 0;
};

/// A byte string.
struct Bytes {
  std::vector<std::uint8_t> data;

  /// Lower-case hexadecimal, two digits a byte.
  [[nodiscard]] std::string to_hex() const;
  /// Parses an even number of hexadecimal digits, either case; empty for anything else.
  [[nodiscard]] static std::optional<Bytes> from_hex(std::string_view hex);

  friend bool operator==(const Bytes&, const Bytes&) = default;
  friend auto operator<=>(const Bytes&, const Bytes&) = default;
};

/// A UUID: sixteen bytes.
class Uuid {
 public:
  Uuid() = default;
  explicit Uuid(const std::array<std::uint8_t, 16>& bytes) noexcept : bytes_(bytes) {}
  /// The canonical text form, `8-4-4-4-12` hexadecimal digits in either case. Throws
  /// `std::invalid_argument` for any other form.
  explicit Uuid(std::string_view text);

  [[nodiscard]] static std::optional<Uuid> parse(std::string_view text) noexcept;

  [[nodiscard]] const std::array<std::uint8_t, 16>& bytes() const noexcept { return bytes_; }
  /// Canonical text, lower case.
  [[nodiscard]] std::string to_string() const;

  friend bool operator==(const Uuid&, const Uuid&) = default;
  /// Byte order, which is the order of the canonical text.
  friend auto operator<=>(const Uuid&, const Uuid&) = default;

 private:
  std::array<std::uint8_t, 16> bytes_{};
};

/// A calendar date in the proleptic Gregorian calendar, years 1 to 9999.
class Date {
 public:
  /// 1970-01-01.
  Date() = default;
  /// Throws `std::invalid_argument` for a date that does not exist or a year outside 1 to 9999.
  Date(int year, unsigned month, unsigned day);
  /// `YYYY-MM-DD`; throws `std::invalid_argument` for anything else.
  explicit Date(std::string_view text);

  [[nodiscard]] static std::optional<Date> parse(std::string_view text) noexcept;
  /// The date this many days from 1970-01-01; empty outside years 1 to 9999.
  [[nodiscard]] static std::optional<Date> from_days(std::int64_t days) noexcept;

  [[nodiscard]] std::int32_t days() const noexcept { return days_; }
  [[nodiscard]] std::chrono::sys_days sys_days() const noexcept {
    return std::chrono::sys_days{std::chrono::days{days_}};
  }
  /// `YYYY-MM-DD`.
  [[nodiscard]] std::string to_string() const;

  friend bool operator==(const Date&, const Date&) = default;
  friend auto operator<=>(const Date&, const Date&) = default;

 private:
  std::int32_t days_ = 0;
};

/// A date and a time of day without a zone, to the microsecond, years 1 to 9999.
class Timestamp {
 public:
  /// 1970-01-01T00:00:00.
  Timestamp() = default;
  /// Text as `YYYY-MM-DD[T ]HH:MM:SS[.ffffff]`, one to six fractional digits, no zone: a timestamp
  /// without one is a wall-clock reading, and text with an offset is an instant (`TimestampTz`).
  /// Throws `std::invalid_argument` for anything else.
  explicit Timestamp(std::string_view text);

  [[nodiscard]] static std::optional<Timestamp> parse(std::string_view text) noexcept;
  /// Microseconds from 1970-01-01T00:00:00; empty outside years 1 to 9999.
  [[nodiscard]] static std::optional<Timestamp> from_micros(std::int64_t micros) noexcept;

  [[nodiscard]] std::int64_t micros() const noexcept { return micros_; }
  /// `YYYY-MM-DDTHH:MM:SS.ffffff`, always six fractional digits.
  [[nodiscard]] std::string to_string() const;

  friend bool operator==(const Timestamp&, const Timestamp&) = default;
  friend auto operator<=>(const Timestamp&, const Timestamp&) = default;

 private:
  std::int64_t micros_ = 0;
};

/// An instant, to the microsecond, between years 1 and 9999 in UTC.
class TimestampTz {
 public:
  /// The epoch.
  TimestampTz() = default;
  /// From the system clock's time point, truncated to microseconds; throws `std::invalid_argument`
  /// outside years 1 to 9999.
  explicit TimestampTz(std::chrono::sys_time<std::chrono::microseconds> time);
  /// ISO 8601 text: `YYYY-MM-DD[T ]HH:MM:SS[.ffffff]` followed by `Z` or an offset `+HH:MM[:SS]`.
  /// Text with no zone is read as UTC, as the reference reads a naive timestamp given for an
  /// instant. Throws `std::invalid_argument` for anything else, and for an instant whose UTC reading
  /// falls outside years 1 to 9999.
  explicit TimestampTz(std::string_view text);

  [[nodiscard]] static std::optional<TimestampTz> parse(std::string_view text) noexcept;
  /// Microseconds since the epoch; empty outside years 1 to 9999.
  [[nodiscard]] static std::optional<TimestampTz> from_micros(std::int64_t micros) noexcept;

  [[nodiscard]] std::int64_t micros() const noexcept { return micros_; }
  [[nodiscard]] std::chrono::sys_time<std::chrono::microseconds> time() const noexcept {
    return std::chrono::sys_time<std::chrono::microseconds>{std::chrono::microseconds{micros_}};
  }
  /// `YYYY-MM-DDTHH:MM:SS.ffffffZ`, always six fractional digits, always UTC.
  [[nodiscard]] std::string to_string() const;

  friend bool operator==(const TimestampTz&, const TimestampTz&) = default;
  friend auto operator<=>(const TimestampTz&, const TimestampTz&) = default;

 private:
  std::int64_t micros_ = 0;
};

/// A value of a `json` field: a document, wrapped so that a string literal is a `string` value and
/// never a JSON one.
struct JsonDocument {
  Json document;

  friend bool operator==(const JsonDocument&, const JsonDocument&) = default;
};

/// SQL NULL, and an absent optional field given explicitly.
using Null = std::monostate;

/// One value of a row.
using Value = std::variant<Null, bool, std::int64_t, double, Decimal, std::string, Bytes, Uuid,
                           Date, Timestamp, TimestampTz, JsonDocument>;

/// A row: field name to value. Ordered by the name's bytes, which for UTF-8 is code point order.
using Row = std::map<std::string, Value, std::less<>>;

[[nodiscard]] inline bool is_null(const Value& value) noexcept {
  return std::holds_alternative<Null>(value);
}

/// The value's host type, in words, for a message: `int64`, `string`, `Decimal` and so on.
[[nodiscard]] std::string_view value_kind(const Value& value) noexcept;

}  // namespace sde

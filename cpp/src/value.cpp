#include "sde/value.hpp"

#include <algorithm>
#include <cstddef>
#include <cstdio>
#include <iterator>
#include <utility>
#include <stdexcept>
#include <string>

#include "value_internal.hpp"

namespace sde {

namespace detail {

namespace {

bool ascii_digit(char c) noexcept { return c >= '0' && c <= '9'; }

/// Reads `count` ASCII digits at `position`, or nothing when they are not all digits.
std::optional<int> digits_at(std::string_view text, std::size_t position, std::size_t count) {
  if (position + count > text.size()) return std::nullopt;
  int value = 0;
  for (std::size_t i = 0; i < count; ++i) {
    const char c = text[position + i];
    if (!ascii_digit(c)) return std::nullopt;
    value = value * 10 + (c - '0');
  }
  return value;
}

}  // namespace

std::optional<DecimalParts> decimal_parts(std::string_view text) noexcept {
  DecimalParts parts;
  std::size_t i = 0;
  if (i < text.size() && (text[i] == '+' || text[i] == '-')) {
    parts.negative = text[i] == '-';
    ++i;
  }
  const std::size_t integer_start = i;
  while (i < text.size() && ascii_digit(text[i])) ++i;
  parts.integer = text.substr(integer_start, i - integer_start);
  if (i < text.size() && text[i] == '.') {
    ++i;
    const std::size_t fraction_start = i;
    while (i < text.size() && ascii_digit(text[i])) ++i;
    parts.fraction = text.substr(fraction_start, i - fraction_start);
  }
  if (parts.integer.empty() && parts.fraction.empty()) return std::nullopt;
  if (i < text.size() && (text[i] == 'e' || text[i] == 'E')) {
    ++i;
    bool exponent_negative = false;
    if (i < text.size() && (text[i] == '+' || text[i] == '-')) {
      exponent_negative = text[i] == '-';
      ++i;
    }
    const std::size_t exponent_start = i;
    std::int64_t exponent = 0;
    while (i < text.size() && ascii_digit(text[i])) {
      // Clamped rather than overflowed: past the clamp every exponent is refused alike.
      exponent = exponent > (kExponentClamp - 9) / 10 ? kExponentClamp
                                                       : exponent * 10 + (text[i] - '0');
      ++i;
    }
    if (i == exponent_start) return std::nullopt;
    parts.exponent = exponent_negative ? -exponent : exponent;
  }
  if (i != text.size()) return std::nullopt;
  return parts;
}

DecimalCounts decimal_counts(const DecimalParts& parts) noexcept {
  // Python's coefficient is the digits before and after the point with leading zeros dropped, and
  // zero is the one-digit coefficient 0: `0012.50` is 1250, `0.05` is 5, `0.000` is 0.
  std::size_t leading = 0;
  while (leading < parts.integer.size() && parts.integer[leading] == '0') ++leading;
  std::int64_t coefficient_digits = 0;
  if (leading < parts.integer.size()) {
    coefficient_digits = static_cast<std::int64_t>(parts.integer.size() - leading + parts.fraction.size());
  } else {
    std::size_t zeros = 0;
    while (zeros < parts.fraction.size() && parts.fraction[zeros] == '0') ++zeros;
    coefficient_digits = static_cast<std::int64_t>(parts.fraction.size() - zeros);
    if (coefficient_digits == 0) coefficient_digits = 1;
  }
  DecimalCounts counts;
  counts.exponent = parts.exponent - static_cast<std::int64_t>(parts.fraction.size());
  counts.adjusted = counts.exponent + coefficient_digits - 1;
  counts.scale = std::max<std::int64_t>(0, -counts.exponent);
  counts.integer_digits = std::max<std::int64_t>(0, counts.adjusted + 1);
  counts.digits = counts.integer_digits + counts.scale;
  return counts;
}

std::optional<Decimal> decimal_from_parts(const DecimalParts& parts) {
  const DecimalCounts counts = decimal_counts(parts);
  if (counts.digits > static_cast<std::int64_t>(Decimal::MAX_DIGITS) ||
      counts.scale > static_cast<std::int64_t>(Decimal::MAX_DIGITS)) {
    return std::nullopt;
  }
  std::string digits(parts.integer);
  digits += parts.fraction;
  std::int64_t scale = static_cast<std::int64_t>(parts.fraction.size()) - parts.exponent;
  if (scale < 0) {
    digits.append(static_cast<std::size_t>(-scale), '0');
    scale = 0;
  }
  return make_decimal(parts.negative, std::move(digits), static_cast<int>(scale));
}

Decimal make_decimal(bool negative, std::string digits, int scale) {
  Decimal out;
  std::size_t first = 0;
  while (first + 1 < digits.size() && digits[first] == '0') ++first;
  digits.erase(0, first);
  if (digits.empty()) digits = "0";
  DecimalAccess::set(out, negative, std::move(digits), scale);
  return out;
}

std::optional<std::int64_t> days_from_civil(std::int64_t year, unsigned month, unsigned day) noexcept {
  if (year < 1 || year > 9999 || month < 1 || month > 12 || day < 1) return std::nullopt;
  static constexpr unsigned kDays[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
  const bool leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
  const unsigned last = month == 2 && leap ? 29U : kDays[month - 1];
  if (day > last) return std::nullopt;
  // Howard Hinnant's days_from_civil, for a year that is never negative here.
  const std::int64_t y = static_cast<std::int64_t>(year) - (month <= 2 ? 1 : 0);
  const std::int64_t era = y / 400;
  const std::int64_t yoe = y - era * 400;
  const std::int64_t mp = (static_cast<std::int64_t>(month) + 9) % 12;
  const std::int64_t doy = (153 * mp + 2) / 5 + static_cast<std::int64_t>(day) - 1;
  const std::int64_t doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
  return era * 146097 + doe - 719468;
}

Civil civil_from_days(std::int64_t days) noexcept {
  const std::int64_t z = days + 719468;
  const std::int64_t era = (z >= 0 ? z : z - 146096) / 146097;
  const std::int64_t doe = z - era * 146097;
  const std::int64_t yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
  const std::int64_t doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
  const std::int64_t mp = (5 * doy + 2) / 153;
  const std::int64_t day = doy - (153 * mp + 2) / 5 + 1;
  const std::int64_t month = mp < 10 ? mp + 3 : mp - 9;
  const std::int64_t year = yoe + era * 400 + (month <= 2 ? 1 : 0);
  return Civil{year, static_cast<unsigned>(month), static_cast<unsigned>(day)};
}

std::optional<IsoTimestamp> parse_iso_timestamp(std::string_view text) noexcept {
  // \d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2}(?::\d{2})?)?
  if (text.size() < 19) return std::nullopt;
  const auto year = digits_at(text, 0, 4);
  const auto month = digits_at(text, 5, 2);
  const auto day = digits_at(text, 8, 2);
  const auto hour = digits_at(text, 11, 2);
  const auto minute = digits_at(text, 14, 2);
  const auto second = digits_at(text, 17, 2);
  if (!year || !month || !day || !hour || !minute || !second || text[4] != '-' ||
      text[7] != '-' || (text[10] != 'T' && text[10] != ' ') || text[13] != ':' ||
      text[16] != ':') {
    return std::nullopt;
  }
  IsoTimestamp out;
  std::size_t i = 19;
  std::int64_t micros = 0;
  if (i < text.size() && text[i] == '.') {
    ++i;
    const std::size_t start = i;
    while (i < text.size() && ascii_digit(text[i])) ++i;
    const std::size_t count = i - start;
    if (count < 1 || count > 6) return std::nullopt;
    for (std::size_t k = 0; k < 6; ++k) {
      micros = micros * 10 + (k < count ? text[start + k] - '0' : 0);
    }
  }
  if (i < text.size()) {
    if (text[i] == 'Z') {
      out.offset_seconds = 0;
      ++i;
    } else if (text[i] == '+' || text[i] == '-') {
      const bool negative = text[i] == '-';
      const auto offset_hours = digits_at(text, i + 1, 2);
      const auto offset_minutes = digits_at(text, i + 4, 2);
      if (!offset_hours || !offset_minutes || i + 3 >= text.size() || text[i + 3] != ':') {
        return std::nullopt;
      }
      int offset_seconds_part = 0;
      std::size_t end = i + 6;
      if (end < text.size() && text[end] == ':') {
        const auto seconds_part = digits_at(text, end + 1, 2);
        if (!seconds_part) return std::nullopt;
        offset_seconds_part = *seconds_part;
        end += 3;
      }
      // Python's timezone takes an offset strictly inside a day, with minutes and seconds in range.
      if (*offset_hours > 23 || *offset_minutes > 59 || offset_seconds_part > 59) return std::nullopt;
      const int total = *offset_hours * 3600 + *offset_minutes * 60 + offset_seconds_part;
      out.offset_seconds = negative ? -total : total;
      i = end;
    } else {
      return std::nullopt;
    }
  }
  if (i != text.size()) return std::nullopt;
  if (*hour > 23 || *minute > 59 || *second > 59) return std::nullopt;
  const auto days = days_from_civil(*year, static_cast<unsigned>(*month), static_cast<unsigned>(*day));
  if (!days) return std::nullopt;
  out.local_micros = ((*days * 24 + *hour) * 60 + *minute) * 60 * kMicrosPerSecond +
                     static_cast<std::int64_t>(*second) * kMicrosPerSecond + micros;
  return out;
}

bool micros_in_range(std::int64_t micros) noexcept {
  return micros >= kMinMicros && micros <= kMaxMicros;
}

std::string format_micros(std::int64_t micros) {
  const std::int64_t days = micros >= 0 ? micros / kMicrosPerDay
                                        : -((-micros + kMicrosPerDay - 1) / kMicrosPerDay);
  const std::int64_t of_day = micros - days * kMicrosPerDay;
  const Civil civil = civil_from_days(days);
  const std::int64_t seconds = of_day / kMicrosPerSecond;
  const std::int64_t fraction = of_day % kMicrosPerSecond;
  char buffer[40];
  const int written = std::snprintf(
      buffer, sizeof buffer, "%04lld-%02u-%02uT%02lld:%02lld:%02lld.%06lld",
      static_cast<long long>(civil.year), civil.month, civil.day,
      static_cast<long long>(seconds / 3600), static_cast<long long>(seconds / 60 % 60),
      static_cast<long long>(seconds % 60), static_cast<long long>(fraction));
  return std::string(buffer, static_cast<std::size_t>(written));
}

}  // namespace detail

// --- Decimal ------------------------------------------------------------------------------------

Decimal::Decimal(std::string_view text) {
  auto parsed = parse(text);
  if (!parsed) {
    throw std::invalid_argument(
        "not a decimal: expected digits with an optional sign, fraction and exponent, at most " +
        std::to_string(MAX_DIGITS) + " digits");
  }
  *this = std::move(*parsed);
}

Decimal::Decimal(std::int64_t value) {
  if (value < 0) {
    negative_ = true;
    // The magnitude of INT64_MIN does not fit in an int64; unsigned arithmetic carries it.
    digits_ = std::to_string(0ULL - static_cast<unsigned long long>(value));
  } else {
    digits_ = std::to_string(value);
  }
}

std::optional<Decimal> Decimal::parse(std::string_view text) noexcept {
  try {
    const auto parts = detail::decimal_parts(text);
    if (!parts) return std::nullopt;
    return detail::decimal_from_parts(*parts);
  } catch (...) {  // allocation failure: not a decimal this process can hold
    return std::nullopt;
  }
}

int Decimal::integer_digits() const noexcept {
  const int total = static_cast<int>(digits_.size());
  return total > scale_ ? total - scale_ : 0;
}

std::string Decimal::to_string() const {
  std::string digits = digits_;
  const auto scale = static_cast<std::size_t>(scale_);
  if (digits.size() <= scale) digits.insert(0, scale + 1 - digits.size(), '0');
  if (scale > 0) digits.insert(digits.size() - scale, 1, '.');
  return negative_ ? "-" + digits : digits;
}

namespace {

/// The magnitudes of two decimals compared: aligned to the larger scale, then by length and digits.
std::strong_ordering compare_magnitude(const Decimal& left, const Decimal& right) {
  const int scale = std::max(left.scale(), right.scale());
  std::string a = left.coefficient();
  std::string b = right.coefficient();
  if (a != "0") a.append(static_cast<std::size_t>(scale - left.scale()), '0');
  if (b != "0") b.append(static_cast<std::size_t>(scale - right.scale()), '0');
  if (a.size() != b.size()) return a.size() <=> b.size();
  const int order = a.compare(b);
  return order < 0 ? std::strong_ordering::less
                   : (order > 0 ? std::strong_ordering::greater : std::strong_ordering::equal);
}

}  // namespace

bool operator==(const Decimal& left, const Decimal& right) noexcept {
  return (left <=> right) == std::strong_ordering::equal;
}

std::strong_ordering operator<=>(const Decimal& left, const Decimal& right) noexcept {
  // Zero has no sign: -0 and 0 are the same number.
  const bool left_negative = left.negative_ && !left.is_zero();
  const bool right_negative = right.negative_ && !right.is_zero();
  if (left_negative != right_negative) {
    return left_negative ? std::strong_ordering::less : std::strong_ordering::greater;
  }
  const std::strong_ordering magnitude = compare_magnitude(left, right);
  return left_negative ? 0 <=> magnitude : magnitude;
}

// --- Bytes --------------------------------------------------------------------------------------

std::string Bytes::to_hex() const {
  static constexpr char kHex[] = "0123456789abcdef";
  std::string out;
  out.reserve(data.size() * 2);
  for (const std::uint8_t byte : data) {
    out += kHex[byte >> 4];
    out += kHex[byte & 0x0F];
  }
  return out;
}

namespace {

int hex_value(char c) noexcept {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

}  // namespace

std::optional<Bytes> Bytes::from_hex(std::string_view hex) {
  if (hex.size() % 2 != 0) return std::nullopt;
  Bytes out;
  out.data.reserve(hex.size() / 2);
  for (std::size_t i = 0; i < hex.size(); i += 2) {
    const int high = hex_value(hex[i]);
    const int low = hex_value(hex[i + 1]);
    if (high < 0 || low < 0) return std::nullopt;
    out.data.push_back(static_cast<std::uint8_t>(high * 16 + low));
  }
  return out;
}

// --- Uuid ---------------------------------------------------------------------------------------

Uuid::Uuid(std::string_view text) {
  auto parsed = parse(text);
  if (!parsed) {
    throw std::invalid_argument("not a UUID: expected 8-4-4-4-12 hexadecimal digits");
  }
  bytes_ = parsed->bytes_;
}

std::optional<Uuid> Uuid::parse(std::string_view text) noexcept {
  // [0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}
  if (text.size() != 36) return std::nullopt;
  std::array<std::uint8_t, 16> bytes{};
  std::size_t out = 0;
  for (std::size_t i = 0; i < text.size();) {
    if (i == 8 || i == 13 || i == 18 || i == 23) {
      if (text[i] != '-') return std::nullopt;
      ++i;
      continue;
    }
    const int high = hex_value(text[i]);
    const int low = hex_value(text[i + 1]);
    if (high < 0 || low < 0) return std::nullopt;
    bytes[out++] = static_cast<std::uint8_t>(high * 16 + low);
    i += 2;
  }
  return Uuid(bytes);
}

std::string Uuid::to_string() const {
  static constexpr char kHex[] = "0123456789abcdef";
  std::string out;
  out.reserve(36);
  for (std::size_t i = 0; i < bytes_.size(); ++i) {
    if (i == 4 || i == 6 || i == 8 || i == 10) out += '-';
    out += kHex[bytes_[i] >> 4];
    out += kHex[bytes_[i] & 0x0F];
  }
  return out;
}

// --- Date ---------------------------------------------------------------------------------------

Date::Date(int year, unsigned month, unsigned day) {
  const auto days = detail::days_from_civil(year, month, day);
  if (!days) throw std::invalid_argument("not a date between years 1 and 9999");
  days_ = static_cast<std::int32_t>(*days);
}

Date::Date(std::string_view text) {
  auto parsed = parse(text);
  if (!parsed) throw std::invalid_argument("not a date: expected YYYY-MM-DD, years 1 to 9999");
  days_ = parsed->days_;
}

std::optional<Date> Date::parse(std::string_view text) noexcept {
  if (text.size() != 10 || text[4] != '-' || text[7] != '-') return std::nullopt;
  int year = 0;
  int month = 0;
  int day = 0;
  for (std::size_t i = 0; i < text.size(); ++i) {
    if (i == 4 || i == 7) continue;
    if (!detail::ascii_digit_char(text[i])) return std::nullopt;
    const int digit = text[i] - '0';
    if (i < 4) {
      year = year * 10 + digit;
    } else if (i < 7) {
      month = month * 10 + digit;
    } else {
      day = day * 10 + digit;
    }
  }
  const auto days =
      detail::days_from_civil(year, static_cast<unsigned>(month), static_cast<unsigned>(day));
  if (!days) return std::nullopt;
  Date out;
  out.days_ = static_cast<std::int32_t>(*days);
  return out;
}

std::optional<Date> Date::from_days(std::int64_t days) noexcept {
  if (days < detail::kMinDays || days > detail::kMaxDays) return std::nullopt;
  Date out;
  out.days_ = static_cast<std::int32_t>(days);
  return out;
}

std::string Date::to_string() const {
  const detail::Civil civil = detail::civil_from_days(days_);
  char buffer[16];
  const int written = std::snprintf(buffer, sizeof buffer, "%04lld-%02u-%02u",
                                    static_cast<long long>(civil.year), civil.month, civil.day);
  return std::string(buffer, static_cast<std::size_t>(written));
}

// --- Timestamp ----------------------------------------------------------------------------------

Timestamp::Timestamp(std::string_view text) {
  auto parsed = parse(text);
  if (!parsed) {
    throw std::invalid_argument(
        "not a timestamp: expected YYYY-MM-DDTHH:MM:SS with up to six fractional digits and no "
        "zone");
  }
  micros_ = parsed->micros_;
}

std::optional<Timestamp> Timestamp::parse(std::string_view text) noexcept {
  const auto parsed = detail::parse_iso_timestamp(text);
  if (!parsed || parsed->offset_seconds.has_value()) return std::nullopt;
  return from_micros(parsed->local_micros);
}

std::optional<Timestamp> Timestamp::from_micros(std::int64_t micros) noexcept {
  if (!detail::micros_in_range(micros)) return std::nullopt;
  Timestamp out;
  out.micros_ = micros;
  return out;
}

std::string Timestamp::to_string() const { return detail::format_micros(micros_); }

// --- TimestampTz --------------------------------------------------------------------------------

TimestampTz::TimestampTz(std::chrono::sys_time<std::chrono::microseconds> time) {
  const auto micros = time.time_since_epoch().count();
  if (!detail::micros_in_range(micros)) {
    throw std::invalid_argument("an instant outside UTC years 1 to 9999");
  }
  micros_ = micros;
}

TimestampTz::TimestampTz(std::string_view text) {
  auto parsed = parse(text);
  if (!parsed) {
    throw std::invalid_argument(
        "not an instant: expected YYYY-MM-DDTHH:MM:SS with up to six fractional digits and Z or "
        "an offset, within UTC years 1 to 9999");
  }
  micros_ = parsed->micros_;
}

std::optional<TimestampTz> TimestampTz::parse(std::string_view text) noexcept {
  const auto parsed = detail::parse_iso_timestamp(text);
  if (!parsed) return std::nullopt;
  const std::int64_t offset = parsed->offset_seconds.value_or(0);
  return from_micros(parsed->local_micros - offset * detail::kMicrosPerSecond);
}

std::optional<TimestampTz> TimestampTz::from_micros(std::int64_t micros) noexcept {
  if (!detail::micros_in_range(micros)) return std::nullopt;
  TimestampTz out;
  out.micros_ = micros;
  return out;
}

std::string TimestampTz::to_string() const { return detail::format_micros(micros_) + "Z"; }

// --- Value --------------------------------------------------------------------------------------

std::string_view value_kind(const Value& value) noexcept {
  static constexpr std::string_view kNames[] = {
      "null", "bool", "int64",     "double",      "Decimal",      "string",
      "Bytes", "Uuid", "Date", "Timestamp", "TimestampTz", "JsonDocument"};
  static_assert(std::size(kNames) == std::variant_size_v<Value>);
  return kNames[value.index()];
}

}  // namespace sde

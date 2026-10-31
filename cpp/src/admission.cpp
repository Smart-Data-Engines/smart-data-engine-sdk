#include "admission.hpp"

#include <charconv>
#include <cmath>
#include <cstdint>
#include <limits>
#include <map>
#include <string_view>
#include <utility>

#include "sde/errors.hpp"
#include "sde/query.hpp"
#include "unicode_internal.hpp"
#include "value_internal.hpp"

namespace sde::detail {

namespace {

/// The types whose rule is a filter value's rule exactly, and what a refusal calls them.
const std::map<std::string, std::string, std::less<>>& nouns() {
  static const std::map<std::string, std::string, std::less<>> table{
      {"bool", "a boolean"},       {"bytes", "bytes"},         {"date", "a date"},
      {"string", "text"},          {"timestamp", "a timestamp"}, {"timestamptz", "a timestamp"},
      {"uuid", "a UUID"},
  };
  return table;
}

Check as_filter(const std::string& kind, const std::string& noun) {
  return [column = ReadColumn{"", kind}, refusal = "a value that is not " + noun](const Value& value) {
    try {
      return query_value(column, value);
    } catch (const QueryRefused&) {
      throw Misfit(refusal);
    }
  };
}

Check integer(const std::string& kind) {
  const bool narrow = kind == "int32";
  return [narrow](const Value& value) -> Value {
    const auto* number = std::get_if<std::int64_t>(&value);
    if (number == nullptr) throw Misfit("a value that is not an integer");
    // Every value of the host's integer is inside int64; only int32 has to look.
    if (narrow && (*number < std::numeric_limits<std::int32_t>::min() ||
                   *number > std::numeric_limits<std::int32_t>::max())) {
      throw Misfit("an integer outside int32");
    }
    return value;
  };
}

/// 2^128 - 2^103, halfway between float's largest finite value and 2^128: from here a double
/// rounds to an infinity in float, ties to even included.
constexpr double kFloat32Overflow = 0x1.ffffffp+127;
/// 2^-150, half of float's smallest subnormal: up to here a double rounds to zero in float.
constexpr double kFloat32Underflow = 0x1p-150;

Check floating(const std::string& kind) {
  const bool single = kind == "float32";
  return [single](const Value& value) -> Value {
    double number = 0;
    if (const auto* real = std::get_if<double>(&value)) {
      number = *real;
    } else if (const auto* whole = std::get_if<std::int64_t>(&value)) {
      number = static_cast<double>(*whole);
    } else {
      throw Misfit("a value that is not a number");
    }
    // NaN and the infinities are values both float types hold, and both engines store them. What
    // float32 cannot hold is a finite number it rounds to an infinity, or one that is not zero and
    // that it rounds to zero: PostgreSQL refuses both for `real`, and ClickHouse stores the
    // infinity or the zero. Decided by bounds, not by a cast: converting a double past float's
    // range to float is undefined behaviour.
    if (single && number != 0.0 && std::isfinite(number)) {
      const double magnitude = std::fabs(number);
      if (magnitude >= kFloat32Overflow || magnitude <= kFloat32Underflow) {
        throw Misfit("a number outside float32");
      }
    }
    return number;
  };
}

/// `decimal(p,s)` taken apart; the model has checked its form.
std::pair<std::int64_t, std::int64_t> precision_and_scale(std::string_view kind) {
  const std::string_view inner = kind.substr(8, kind.size() - 9);  // "decimal(" ... ")"
  const std::size_t comma = inner.find(',');
  std::int64_t precision = 0;
  std::int64_t scale = 0;
  std::from_chars(inner.data(), inner.data() + comma, precision);
  std::from_chars(inner.data() + comma + 1, inner.data() + inner.size(), scale);
  return {precision, scale};
}

const std::string kNotExact = "a value that is not an exact decimal";

Check decimal(const std::string& kind) {
  // Plain names rather than a structured binding: the lambda below captures them, which Clang 16,
  // the floor, does not do for a binding.
  const std::pair<std::int64_t, std::int64_t> declared = precision_and_scale(kind);
  const std::int64_t scale = declared.second;
  const std::int64_t whole = declared.first - declared.second;
  return [scale, whole](const Value& value) -> Value {
    bool negative = false;
    std::string digits;  // the coefficient, without leading zeros; empty for zero
    std::int64_t exponent = 0;
    if (const auto* integer = std::get_if<std::int64_t>(&value)) {
      negative = *integer < 0;
      // The magnitude as unsigned: the most negative integer has no positive counterpart.
      const std::uint64_t magnitude =
          negative ? std::uint64_t{0} - static_cast<std::uint64_t>(*integer)
                   : static_cast<std::uint64_t>(*integer);
      if (magnitude != 0) digits = std::to_string(magnitude);
    } else if (const auto* given = std::get_if<Decimal>(&value)) {
      negative = given->negative();
      if (!given->is_zero()) digits = given->coefficient();
      exponent = -given->scale();
    } else if (const auto* text = std::get_if<std::string>(&value)) {
      const auto parts = decimal_parts(strip_white_space(*text));
      // Past the reference's own limits its Decimal() calls the text invalid, and so does this.
      if (!parts || python_decimal_invalid(decimal_counts(*parts))) throw Misfit(kNotExact);
      negative = parts->negative;
      digits = std::string(parts->integer) + std::string(parts->fraction);
      const std::size_t first = digits.find_first_not_of('0');
      digits.erase(0, first == std::string::npos ? digits.size() : first);
      exponent = parts->exponent - static_cast<std::int64_t>(parts->fraction.size());
    } else {
      throw Misfit(kNotExact);
    }
    if (!digits.empty()) {
      // Trailing zeros say nothing about the value: 1.230 fits two fractional digits.
      const std::size_t significant = digits.find_last_not_of('0') + 1;
      exponent += static_cast<std::int64_t>(digits.size() - significant);
      digits.resize(significant);
      if (-exponent > scale) throw Misfit("more than " + std::to_string(scale) + " fractional digits");
      if (static_cast<std::int64_t>(digits.size()) + exponent > whole) {
        throw Misfit("more than " + std::to_string(whole) + " integer digits");
      }
    }
    if (std::holds_alternative<std::int64_t>(value)) return value;  // an integer stays itself
    // At the column's scale, the form every engine receives. Only now, with at most the field's
    // precision in digits: text this long is refused above before it could cost anything.
    if (digits.empty()) return make_decimal(false, "0", static_cast<int>(scale));
    digits.append(static_cast<std::size_t>(exponent + scale), '0');
    return make_decimal(negative, std::move(digits), static_cast<int>(scale));
  };
}

}  // namespace

Check check_for(const std::string& kind) {
  if (const auto found = nouns().find(kind); found != nouns().end()) {
    return as_filter(kind, found->second);
  }
  if (kind == "int32" || kind == "int64") return integer(kind);
  if (kind == "float32" || kind == "float64") return floating(kind);
  if (kind.rfind("decimal(", 0) == 0) return decimal(kind);
  // json: whatever the adapter can serialise. The section 8b rule does not reach it yet.
  return {};
}

}  // namespace sde::detail

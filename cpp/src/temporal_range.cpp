#include <string>
#include <string_view>
#include <variant>

#include "sde/engine.hpp"

namespace sde {

namespace {

constexpr std::string_view kRangeSql =
    "SELECT toString(toDate32('0001-01-01')), toString(toDate32('9999-12-31')), "
    "toString(toDateTime64('0001-01-01 00:00:00', 6, 'UTC')), "
    "toString(toDateTime64('9999-12-31 23:59:59.999999', 6, 'UTC'))";

/// The microseconds of an instant's text as a server answers it, or of a field's value given as
/// text: with an offset it is an instant, without one a wall-clock reading in UTC.
std::optional<std::int64_t> micros_of(std::string_view text) {
  if (const auto naive = Timestamp::parse(text)) return naive->micros();
  if (const auto aware = TimestampTz::parse(text)) return aware->micros();
  return std::nullopt;
}

}  // namespace

bool TemporalRange::holds(std::string_view kind, const Value& value) const {
  if (const auto* day = std::get_if<Date>(&value)) return first_day <= *day && *day <= last_day;
  std::optional<std::int64_t> micros;
  if (const auto* naive = std::get_if<Timestamp>(&value)) micros = naive->micros();
  if (const auto* aware = std::get_if<TimestampTz>(&value)) micros = aware->micros();
  if (const auto* text = std::get_if<std::string>(&value)) {
    if (kind == "date") {
      const auto day = Date::parse(*text);
      return !day.has_value() || (first_day <= *day && *day <= last_day);
    }
    micros = micros_of(*text);
  }
  return !micros.has_value() || (first_micros <= *micros && *micros <= last_micros);
}

std::string TemporalRange::describe() const {
  return "from " + first_day.to_string() + " to " + last_day.to_string();
}

std::optional<TemporalRange> TemporalRange::from_engine_text(std::string_view first_day,
                                                             std::string_view last_day,
                                                             std::string_view first,
                                                             std::string_view last) {
  TemporalRange range{Date(first_day), Date(last_day), micros_of(first).value_or(0),
                      micros_of(last).value_or(0)};
  const bool whole = range.first_day == Date(1, 1, 1) && range.last_day == Date(9999, 12, 31) &&
                     range.first_micros == Timestamp("0001-01-01T00:00:00").micros() &&
                     range.last_micros == Timestamp("9999-12-31T23:59:59.999999").micros();
  if (whole) return std::nullopt;
  return range;
}

std::string_view TemporalRange::range_sql() noexcept { return kRangeSql; }

std::string_view temporal_noun(std::string_view kind) noexcept {
  return kind == "date" ? "a date" : "a timestamp";
}

}  // namespace sde

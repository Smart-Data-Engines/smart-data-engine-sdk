#include "engines/postgres/values.hpp"

#include <charconv>
#include <cmath>
#include <limits>
#include <span>
#include <string>

#include "encoding.hpp"
#include "python_compat.hpp"
#include "sde/errors.hpp"
#include "sde/json.hpp"

namespace sde::detail::postgres {

namespace {

[[noreturn]] void unrepresentable(std::string_view text, std::string_view what) {
  throw EngineError("PostgreSQL returned " + python_repr(text.substr(0, 40)) + " for a " +
                    std::string(what) + ", which this library does not represent");
}

std::int64_t integer_cell(std::string_view text) {
  std::int64_t value = 0;
  const auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), value);
  if (error != std::errc() || end != text.data() + text.size()) unrepresentable(text, "integer");
  return value;
}

double float_cell(std::string_view text) {
  if (text == "NaN") return std::numeric_limits<double>::quiet_NaN();
  if (text == "Infinity") return std::numeric_limits<double>::infinity();
  if (text == "-Infinity") return -std::numeric_limits<double>::infinity();
  double value = 0;
  const auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), value);
  if (error != std::errc() || end != text.data() + text.size()) unrepresentable(text, "float");
  return value;
}

/// The offset of a `timestamptz` as the ISO style writes it - `+HH`, `+HH:MM` or `+HH:MM:SS` -
/// made the `+HH:MM[:SS]` the value's parser reads.
std::string with_minutes(std::string_view text) {
  const std::size_t sign = text.find_last_of("+-");
  std::string out(text);
  if (sign != std::string_view::npos && sign >= 19 && text.size() - sign == 3) out += ":00";
  return out;
}

}  // namespace

std::optional<std::string> parameter_text(const Value& value) {
  if (is_null(value)) return std::nullopt;
  if (const auto* flag = std::get_if<bool>(&value)) return std::string(*flag ? "true" : "false");
  if (const auto* integer = std::get_if<std::int64_t>(&value)) return std::to_string(*integer);
  if (const auto* number = std::get_if<double>(&value)) {
    if (std::isnan(*number)) return std::string("NaN");
    if (std::isinf(*number)) return std::string(*number > 0 ? "Infinity" : "-Infinity");
    return python_float_repr(*number);  // the shortest text that reads back as this double
  }
  if (const auto* decimal = std::get_if<Decimal>(&value)) return decimal->to_string();
  if (const auto* text = std::get_if<std::string>(&value)) {
    if (text->find('\0') != std::string::npos) {
      throw EngineError("PostgreSQL text cannot hold a NUL character, and this value has one");
    }
    return *text;
  }
  if (const auto* bytes = std::get_if<Bytes>(&value)) {
    return "\\x" + hex_encode(std::string_view(reinterpret_cast<const char*>(bytes->data.data()),
                                               bytes->data.size()));
  }
  if (const auto* uuid = std::get_if<Uuid>(&value)) return uuid->to_string();
  if (const auto* date = std::get_if<Date>(&value)) return date->to_string();
  if (const auto* naive = std::get_if<Timestamp>(&value)) return naive->to_string();
  if (const auto* instant = std::get_if<TimestampTz>(&value)) return instant->to_string();
  return dump_json(std::get<JsonDocument>(value).document);
}

Value cell_value(std::string_view text, unsigned type) {
  switch (type) {
    case oid::kBool:
      if (text == "t") return true;
      if (text == "f") return false;
      unrepresentable(text, "boolean");
    case oid::kInt2:
    case oid::kInt4:
    case oid::kInt8:
      return integer_cell(text);
    case oid::kFloat4:
    case oid::kFloat8:
      return float_cell(text);
    case oid::kNumeric:
      if (auto decimal = Decimal::parse(text)) return *decimal;
      unrepresentable(text, "numeric");
    case oid::kBytea: {
      if (!text.starts_with("\\x")) unrepresentable(text, "bytea (bytea_output must be hex)");
      const std::optional<std::string> raw = hex_decode(text.substr(2));
      if (!raw) unrepresentable(text, "bytea");
      return Bytes{std::vector<std::uint8_t>(raw->begin(), raw->end())};
    }
    case oid::kUuid:
      if (auto uuid = Uuid::parse(text)) return *uuid;
      unrepresentable(text, "uuid");
    case oid::kDate:
      if (auto date = Date::parse(text)) return *date;
      unrepresentable(text, "date");
    case oid::kTimestamp:
      if (auto naive = Timestamp::parse(text)) return *naive;
      unrepresentable(text, "timestamp");
    case oid::kTimestamptz:
      if (auto instant = TimestampTz::parse(with_minutes(text))) return *instant;
      unrepresentable(text, "timestamptz");
    case oid::kJson:
    case oid::kJsonb:
      return JsonDocument{parse_json(text)};
    default:
      return std::string(text);
  }
}

std::string text_array(const std::vector<std::string>& items) {
  std::string out = "{";
  for (std::size_t i = 0; i < items.size(); ++i) {
    if (i > 0) out += ',';
    out += '"';
    for (char c : items[i]) {
      if (c == '"' || c == '\\') out += '\\';
      out += c;
    }
    out += '"';
  }
  return out + "}";
}

}  // namespace sde::detail::postgres

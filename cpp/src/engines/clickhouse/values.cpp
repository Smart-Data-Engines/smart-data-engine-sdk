#include "engines/clickhouse/values.hpp"

#include <bit>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>
#include <map>
#include <optional>
#include <string>
#include <variant>

#include "bignum.hpp"
#include "python_compat.hpp"
#include "sde/errors.hpp"
#include "unicode_internal.hpp"
#include "value_internal.hpp"

namespace sde::detail::clickhouse {

static_assert(std::endian::native == std::endian::little,
              "RowBinary is little-endian, and this codec copies integers as the host holds them");

namespace {

template <class... Ts>
struct Overloaded : Ts... {
  using Ts::operator()...;
};
template <class... Ts>
Overloaded(Ts...) -> Overloaded<Ts...>;

[[noreturn]] void refuse_type(std::string_view type) {
  throw EngineError("a value of type " + std::string(type) +
                    " has no ClickHouse column in any layout this library writes, so it cannot be "
                    "sent there");
}

/// `YYYY-MM-DD HH:MM:SS.ffffff`, UTC: the moment as `toDateTime64(..., 'UTC')` reads it.
std::string moment(std::int64_t micros) {
  std::string text = format_micros(micros);
  if (const std::size_t t = text.find('T'); t != std::string::npos) text[t] = ' ';
  return text;
}

/// clickhouse-connect's `format_str`: a backslash before each of `\`, `'`, `` ` ``, tab, newline.
std::string quoted(const std::string& text) {
  std::string out = "'";
  for (const char c : text) {
    if (c == '\\' || c == '\'' || c == '`' || c == '\t' || c == '\n') out += '\\';
    out += c;
  }
  return out + "'";
}

// --- types --------------------------------------------------------------------------------------

/// The text between `prefix(` and the closing parenthesis, when the type is that wrapper.
std::optional<std::string_view> wrapped(std::string_view type, std::string_view prefix) {
  if (type.size() > prefix.size() + 1 && type.starts_with(prefix) && type[prefix.size()] == '(' &&
      type.back() == ')') {
    return type.substr(prefix.size() + 1, type.size() - prefix.size() - 2);
  }
  return std::nullopt;
}

std::string_view trimmed(std::string_view text) {
  while (!text.empty() && text.front() == ' ') text.remove_prefix(1);
  while (!text.empty() && text.back() == ' ') text.remove_suffix(1);
  return text;
}

int number(std::string_view text) {
  text = trimmed(text);
  int out = 0;
  const auto [end, problem] = std::from_chars(text.data(), text.data() + text.size(), out);
  if (problem != std::errc{} || end != text.data() + text.size()) {
    throw EngineError("ClickHouse returned a type this library does not read");
  }
  return out;
}

struct DecimalType {
  int precision;
  int scale;
  [[nodiscard]] std::size_t bytes() const noexcept {
    return precision <= 9 ? 4 : precision <= 18 ? 8 : precision <= 38 ? 16 : 32;
  }
};

std::optional<DecimalType> decimal_type(std::string_view type) {
  if (const auto inside = wrapped(type, "Decimal")) {
    const std::size_t comma = inside->find(',');
    if (comma == std::string_view::npos) return std::nullopt;
    return DecimalType{number(inside->substr(0, comma)), number(inside->substr(comma + 1))};
  }
  for (const auto& [prefix, precision] : {std::pair<std::string_view, int>{"Decimal32", 9},
                                          {"Decimal64", 18},
                                          {"Decimal128", 38},
                                          {"Decimal256", 76}}) {
    if (const auto inside = wrapped(type, prefix)) return DecimalType{precision, number(*inside)};
  }
  return std::nullopt;
}

struct MomentType {
  int precision;  ///< digits after the second
  bool zoned;
};

std::optional<MomentType> moment_type(std::string_view type) {
  if (type == "DateTime") return MomentType{0, false};
  if (wrapped(type, "DateTime")) return MomentType{0, true};
  if (const auto inside = wrapped(type, "DateTime64")) {
    const std::size_t comma = inside->find(',');
    return MomentType{number(inside->substr(0, comma)), comma != std::string_view::npos};
  }
  return std::nullopt;
}

std::int64_t pow10(int exponent) {
  std::int64_t out = 1;
  for (int i = 0; i < exponent; ++i) out *= 10;
  return out;
}

/// An `Enum8('a' = 1, 'b' = 2)`'s values by number.
std::map<std::int64_t, std::string> enum_names(std::string_view inside) {
  std::map<std::int64_t, std::string> out;
  std::size_t at = 0;
  while (at < inside.size()) {
    while (at < inside.size() && (inside[at] == ' ' || inside[at] == ',')) ++at;
    if (at >= inside.size()) break;
    if (inside[at] != '\'') throw EngineError("ClickHouse returned a type this library does not read");
    ++at;
    std::string name;
    while (at < inside.size() && inside[at] != '\'') {
      if (inside[at] == '\\' && at + 1 < inside.size()) ++at;
      name += inside[at++];
    }
    ++at;
    const std::size_t equals = inside.find('=', at);
    const std::size_t next = inside.find(',', equals);
    out[number(inside.substr(equals + 1, next == std::string_view::npos ? std::string_view::npos
                                                                        : next - equals - 1))] = name;
    at = next == std::string_view::npos ? inside.size() : next + 1;
  }
  return out;
}

// --- big integers -------------------------------------------------------------------------------

/// A two's complement little-endian integer of `bytes` bytes, as decimal digits with its sign.
std::pair<bool, std::string> wide_integer(const char* data, std::size_t bytes, bool is_signed) {
  const bool negative = is_signed && (static_cast<unsigned char>(data[bytes - 1]) & 0x80U) != 0;
  BigUnsigned value;
  const BigUnsigned base(256);
  for (std::size_t i = bytes; i-- > 0;) {
    value = value * base + BigUnsigned(static_cast<unsigned char>(data[i]));
  }
  if (negative) {
    BigUnsigned whole(1);
    for (std::size_t i = 0; i < bytes; ++i) whole = whole * base;
    value = whole - value;
  }
  return {negative, value.to_decimal()};
}

/// The other way: `digits` with a sign as `bytes` bytes of two's complement. Refuses a value that
/// does not fit.
std::string wide_bytes(bool negative, const std::string& digits, std::size_t bytes) {
  BigUnsigned value = *BigUnsigned::from_decimal(digits);
  const BigUnsigned base(256);
  BigUnsigned whole(1);
  for (std::size_t i = 0; i < bytes; ++i) whole = whole * base;
  BigUnsigned half = divmod(whole, BigUnsigned(2)).first;
  if (negative ? value > half : value >= half) {
    throw EngineError("a value does not fit the column's " + std::to_string(bytes * 8) + " bits");
  }
  if (negative && !value.is_zero()) value = whole - value;
  std::string out(bytes, '\0');
  for (std::size_t i = 0; i < bytes; ++i) {
    auto [quotient, remainder] = divmod(value, base);
    out[i] = static_cast<char>(*remainder.to_uint64());
    value = std::move(quotient);
  }
  return out;
}

/// A decimal's digits from its unscaled integer and scale: `-12345` at 2 is `-123.45`.
Decimal decimal_from(bool negative, const std::string& unscaled, int scale) {
  std::string digits = unscaled;
  if (scale > 0) {
    if (static_cast<int>(digits.size()) <= scale) digits.insert(0, static_cast<std::size_t>(scale) + 1 - digits.size(), '0');
    digits.insert(digits.size() - static_cast<std::size_t>(scale), ".");
  }
  return Decimal((negative && unscaled != "0" ? "-" : "") + digits);
}

// --- reading ------------------------------------------------------------------------------------

class Reader {
 public:
  explicit Reader(std::string_view data) : data_(data) {}

  const char* take(std::size_t count) {
    if (data_.size() - at_ < count) throw EngineError("ClickHouse returned an answer that ends early");
    const char* out = data_.data() + at_;
    at_ += count;
    return out;
  }
  template <class T>
  T fixed() {
    T out;
    std::memcpy(&out, take(sizeof(T)), sizeof(T));
    return out;
  }
  std::uint64_t varint() {
    std::uint64_t out = 0;
    for (int shift = 0; shift < 64; shift += 7) {
      const auto byte = static_cast<unsigned char>(*take(1));
      out |= static_cast<std::uint64_t>(byte & 0x7FU) << static_cast<unsigned>(shift);
      if ((byte & 0x80U) == 0) return out;
    }
    throw EngineError("ClickHouse returned a length past 64 bits");
  }
  std::string string() {
    const std::uint64_t length = varint();
    if (length > data_.size() - at_) throw EngineError("ClickHouse returned an answer that ends early");
    const char* start = take(static_cast<std::size_t>(length));
    return {start, static_cast<std::size_t>(length)};
  }
  [[nodiscard]] bool done() const noexcept { return at_ == data_.size(); }

 private:
  std::string_view data_;
  std::size_t at_ = 0;
};

Value read_value(Reader& in, std::string_view type) {
  if (const auto inside = wrapped(type, "Nullable")) {
    if (in.fixed<std::uint8_t>() != 0) return Null{};
    return read_value(in, *inside);
  }
  if (const auto inside = wrapped(type, "LowCardinality")) return read_value(in, *inside);
  if (type == "Bool") return in.fixed<std::uint8_t>() != 0;
  if (type == "Int8") return std::int64_t{in.fixed<std::int8_t>()};
  if (type == "Int16") return std::int64_t{in.fixed<std::int16_t>()};
  if (type == "Int32") return std::int64_t{in.fixed<std::int32_t>()};
  if (type == "Int64") return in.fixed<std::int64_t>();
  if (type == "UInt8") return std::int64_t{in.fixed<std::uint8_t>()};
  if (type == "UInt16") return std::int64_t{in.fixed<std::uint16_t>()};
  if (type == "UInt32") return std::int64_t{in.fixed<std::uint32_t>()};
  if (type == "UInt64") {
    const auto value = in.fixed<std::uint64_t>();
    if (value > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())) {
      throw EngineError("ClickHouse returned an integer past what 64 signed bits hold, which this "
                        "library does not round");
    }
    return static_cast<std::int64_t>(value);
  }
  for (const auto& [name, bytes, is_signed] :
       {std::tuple<std::string_view, std::size_t, bool>{"Int128", 16, true},
        {"UInt128", 16, false},
        {"Int256", 32, true},
        {"UInt256", 32, false}}) {
    if (type == name) {
      const auto [negative, digits] = wide_integer(in.take(bytes), bytes, is_signed);
      return Decimal((negative ? "-" : "") + digits);
    }
  }
  if (type == "Float32") return static_cast<double>(in.fixed<float>());
  if (type == "Float64") return in.fixed<double>();
  if (const auto decimal = decimal_type(type)) {
    const std::size_t bytes = decimal->bytes();
    if (bytes <= 8) {
      const std::int64_t unscaled = bytes == 4 ? std::int64_t{in.fixed<std::int32_t>()}
                                               : in.fixed<std::int64_t>();
      const bool negative = unscaled < 0;
      const std::uint64_t magnitude =
          negative ? static_cast<std::uint64_t>(-(unscaled + 1)) + 1 : static_cast<std::uint64_t>(unscaled);
      return decimal_from(negative, std::to_string(magnitude), decimal->scale);
    }
    const auto [negative, digits] = wide_integer(in.take(bytes), bytes, true);
    return decimal_from(negative, digits, decimal->scale);
  }
  if (type == "String") return in.string();
  if (const auto inside = wrapped(type, "FixedString")) {
    const int size = number(*inside);
    return std::string(in.take(static_cast<std::size_t>(size)), static_cast<std::size_t>(size));
  }
  if (type == "UUID") {
    // Two 64-bit halves, each little-endian, the high one first.
    const auto high = in.fixed<std::uint64_t>();
    const auto low = in.fixed<std::uint64_t>();
    char text[40];
    std::snprintf(text, sizeof text, "%08x-%04x-%04x-%04x-%012llx",
                  static_cast<unsigned>(high >> 32U), static_cast<unsigned>((high >> 16U) & 0xFFFFU),
                  static_cast<unsigned>(high & 0xFFFFU), static_cast<unsigned>(low >> 48U),
                  static_cast<unsigned long long>(low & 0xFFFFFFFFFFFFULL));
    return *Uuid::parse(text);
  }
  if (type == "Date") return *Date::from_days(std::int64_t{in.fixed<std::uint16_t>()});
  if (type == "Date32") {
    const std::optional<Date> day = Date::from_days(std::int64_t{in.fixed<std::int32_t>()});
    if (!day) throw EngineError("ClickHouse returned a date outside years 1 to 9999");
    return *day;
  }
  if (const auto kind = moment_type(type)) {
    std::int64_t ticks = kind->precision == 0 && type.starts_with("DateTime") && !type.starts_with("DateTime64")
                             ? std::int64_t{in.fixed<std::uint32_t>()}
                             : in.fixed<std::int64_t>();
    std::int64_t micros = 0;
    if (kind->precision <= 6) {
      micros = ticks * pow10(6 - kind->precision);
    } else {
      const std::int64_t divisor = pow10(kind->precision - 6);
      if (ticks % divisor != 0) {
        throw EngineError("ClickHouse returned a moment finer than a microsecond, which this "
                          "library does not round");
      }
      micros = ticks / divisor;
    }
    if (kind->zoned) {
      const std::optional<TimestampTz> at = TimestampTz::from_micros(micros);
      if (!at) throw EngineError("ClickHouse returned a moment outside years 1 to 9999");
      return *at;
    }
    const std::optional<Timestamp> at = Timestamp::from_micros(micros);
    if (!at) throw EngineError("ClickHouse returned a moment outside years 1 to 9999");
    return *at;
  }
  for (const auto& [prefix, bytes] : {std::pair<std::string_view, int>{"Enum8", 1}, {"Enum16", 2}}) {
    if (const auto inside = wrapped(type, prefix)) {
      const std::int64_t code = bytes == 1 ? std::int64_t{in.fixed<std::int8_t>()} : std::int64_t{in.fixed<std::int16_t>()};
      const auto names = enum_names(*inside);
      const auto found = names.find(code);
      if (found == names.end()) throw EngineError("ClickHouse returned an enum value its type does not name");
      return found->second;
    }
  }
  throw EngineError("ClickHouse returned a " + std::string(type) + " column, which this library does not read");
}

// --- writing ------------------------------------------------------------------------------------

void varint(std::string& out, std::uint64_t value) {
  while (value >= 0x80U) {
    out += static_cast<char>((value & 0x7FU) | 0x80U);
    value >>= 7U;
  }
  out += static_cast<char>(value);
}

template <class T>
void fixed(std::string& out, T value) {
  char bytes[sizeof(T)];
  std::memcpy(bytes, &value, sizeof(T));
  out.append(bytes, sizeof(T));
}

[[noreturn]] void cannot_hold(std::string_view type) {
  throw EngineError("a value cannot be written to a ClickHouse column of type " + std::string(type) +
                    " without changing it");
}

template <class T>
void bounded(std::string& out, std::int64_t value, std::string_view type) {
  if (value < static_cast<std::int64_t>(std::numeric_limits<T>::min()) ||
      (value > 0 && static_cast<std::uint64_t>(value) > static_cast<std::uint64_t>(std::numeric_limits<T>::max()))) {
    cannot_hold(type);
  }
  fixed<T>(out, static_cast<T>(value));
}

void write_value(std::string& out, const Value& value, std::string_view type) {
  if (const auto inside = wrapped(type, "Nullable")) {
    if (std::holds_alternative<Null>(value)) {
      out += '\x01';
      return;
    }
    out += '\x00';
    write_value(out, value, *inside);
    return;
  }
  if (const auto inside = wrapped(type, "LowCardinality")) {
    write_value(out, value, *inside);
    return;
  }
  if (std::holds_alternative<Null>(value)) {
    throw EngineError("a null cannot be written to a ClickHouse column of type " + std::string(type) +
                      ", which is not Nullable");
  }
  if (type == "Bool") {
    if (!std::holds_alternative<bool>(value)) cannot_hold(type);
    out += std::get<bool>(value) ? '\x01' : '\x00';
    return;
  }
  const auto* integer = std::get_if<std::int64_t>(&value);
  if (type == "Int8" || type == "Int16" || type == "Int32" || type == "Int64" || type == "UInt8" ||
      type == "UInt16" || type == "UInt32" || type == "UInt64") {
    if (integer == nullptr) cannot_hold(type);
    if (type == "Int8") return bounded<std::int8_t>(out, *integer, type);
    if (type == "Int16") return bounded<std::int16_t>(out, *integer, type);
    if (type == "Int32") return bounded<std::int32_t>(out, *integer, type);
    if (type == "Int64") return fixed<std::int64_t>(out, *integer);
    if (type == "UInt8") return bounded<std::uint8_t>(out, *integer, type);
    if (type == "UInt16") return bounded<std::uint16_t>(out, *integer, type);
    if (type == "UInt32") return bounded<std::uint32_t>(out, *integer, type);
    if (*integer < 0) cannot_hold(type);
    return fixed<std::uint64_t>(out, static_cast<std::uint64_t>(*integer));
  }
  if (type == "Float32" || type == "Float64") {
    double number = 0;
    if (const auto* real = std::get_if<double>(&value)) {
      number = *real;
    } else if (integer != nullptr) {
      number = static_cast<double>(*integer);
    } else {
      cannot_hold(type);
    }
    if (type == "Float32") return fixed<float>(out, static_cast<float>(number));
    return fixed<double>(out, number);
  }
  if (const auto decimal = decimal_type(type)) {
    Decimal number;
    if (const auto* exact = std::get_if<Decimal>(&value)) {
      number = *exact;
    } else if (integer != nullptr) {
      number = Decimal(*integer);
    } else {
      cannot_hold(type);
    }
    if (number.scale() > decimal->scale) {
      // Writing it would cut the digits past the column's scale: refused, never rounded.
      throw EngineError("a decimal with " + std::to_string(number.scale()) +
                        " digits after the point cannot be written to a ClickHouse column of type " +
                        std::string(type) + " without cutting them");
    }
    std::string unscaled = number.coefficient();
    if (unscaled != "0") unscaled.append(static_cast<std::size_t>(decimal->scale - number.scale()), '0');
    if (static_cast<int>(unscaled.size()) > decimal->precision && unscaled != "0") cannot_hold(type);
    out += wide_bytes(number.negative(), unscaled, decimal->bytes());
    return;
  }
  if (type == "String") {
    if (const auto* text = std::get_if<std::string>(&value)) {
      varint(out, text->size());
      out += *text;
      return;
    }
    cannot_hold(type);
  }
  if (const auto inside = wrapped(type, "FixedString")) {
    const auto size = static_cast<std::size_t>(number(*inside));
    const auto* text = std::get_if<std::string>(&value);
    if (text == nullptr || text->size() > size) cannot_hold(type);
    out += *text;
    out.append(size - text->size(), '\0');
    return;
  }
  if (type == "UUID") {
    const auto* id = std::get_if<Uuid>(&value);
    if (id == nullptr) cannot_hold(type);
    const std::string text = id->to_string();  // 8-4-4-4-12
    std::string hex;
    for (const char c : text) {
      if (c != '-') hex += c;
    }
    fixed<std::uint64_t>(out, std::stoull(hex.substr(0, 16), nullptr, 16));
    fixed<std::uint64_t>(out, std::stoull(hex.substr(16), nullptr, 16));
    return;
  }
  if (type == "Date" || type == "Date32") {
    const auto* day = std::get_if<Date>(&value);
    if (day == nullptr) cannot_hold(type);
    if (type == "Date") {
      if (day->days() < 0 || day->days() > 65535) cannot_hold(type);
      return fixed<std::uint16_t>(out, static_cast<std::uint16_t>(day->days()));
    }
    return fixed<std::int32_t>(out, static_cast<std::int32_t>(day->days()));
  }
  if (const auto kind = moment_type(type)) {
    std::int64_t micros = 0;
    if (const auto* wall = std::get_if<Timestamp>(&value)) {
      micros = wall->micros();
    } else if (const auto* instant = std::get_if<TimestampTz>(&value)) {
      micros = instant->micros();
    } else {
      cannot_hold(type);
    }
    if (!type.starts_with("DateTime64")) {
      if (micros % 1000000 != 0 || micros < 0 || micros / 1000000 > 4294967295LL) cannot_hold(type);
      return fixed<std::uint32_t>(out, static_cast<std::uint32_t>(micros / 1000000));
    }
    if (kind->precision < 6) {
      const std::int64_t divisor = pow10(6 - kind->precision);
      if (micros % divisor != 0) cannot_hold(type);
      return fixed<std::int64_t>(out, micros / divisor);
    }
    return fixed<std::int64_t>(out, micros * pow10(kind->precision - 6));
  }
  cannot_hold(type);
}

}  // namespace

std::string literal(const Value& value) {
  return std::visit(
      Overloaded{
          [](const Null&) -> std::string { return "NULL"; },
          // Python's str() of a bool, which is how the reference's driver writes one.
          [](bool flag) -> std::string { return flag ? "True" : "False"; },
          [](std::int64_t integer) -> std::string { return std::to_string(integer); },
          [](double real) -> std::string { return python_float_repr(real); },
          [](const Decimal& exact) -> std::string { return exact.to_string(); },
          [](const std::string& text) -> std::string { return quoted(text); },
          [](const Bytes&) -> std::string { refuse_type("bytes"); },
          [](const Uuid& id) -> std::string { return "'" + id.to_string() + "'"; },
          [](const Date& day) -> std::string { return "'" + day.to_string() + "'"; },
          [](const Timestamp& at) -> std::string {
            return "toDateTime64('" + moment(at.micros()) + "', 6, 'UTC')";
          },
          [](const TimestampTz& at) -> std::string {
            return "toDateTime64('" + moment(at.micros()) + "', 6, 'UTC')";
          },
          [](const JsonDocument&) -> std::string { refuse_type("json"); },
      },
      value);
}

Answer decode_answer(std::string_view body) {
  Reader in(body);
  Answer out;
  if (in.done()) return out;
  const std::uint64_t columns = in.varint();
  for (std::uint64_t i = 0; i < columns; ++i) out.names.push_back(in.string());
  for (std::uint64_t i = 0; i < columns; ++i) out.types.push_back(in.string());
  while (!in.done()) {
    std::vector<Value> row;
    for (std::uint64_t i = 0; i < columns; ++i) row.push_back(read_value(in, out.types[i]));
    out.rows.push_back(std::move(row));
  }
  return out;
}

std::string encode_rows(const std::vector<std::string>& names, const std::vector<std::string>& types,
                        const std::vector<std::vector<Value>>& rows) {
  std::string out;
  varint(out, names.size());
  for (const std::string& name : names) {
    varint(out, name.size());
    out += name;
  }
  for (const std::string& type : types) {
    varint(out, type.size());
    out += type;
  }
  for (const std::vector<Value>& row : rows) {
    for (std::size_t i = 0; i < names.size(); ++i) write_value(out, row[i], types[i]);
  }
  return out;
}

}  // namespace sde::detail::clickhouse

#include "python_compat.hpp"

#include <array>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <limits>
#include <system_error>

#include <utf8proc.h>

#include "sde/errors.hpp"
#include "unicode_internal.hpp"

namespace sde::detail {

namespace {

/// Decodes for display: a three-byte surrogate sequence (what the JSON parser keeps for a lone
/// surrogate escape) is its surrogate, as in a Python string; any other ill-formed byte is U+FFFD.
std::vector<std::uint32_t> display_code_points(std::string_view text) {
  std::vector<std::uint32_t> out;
  std::size_t pos = 0;
  while (pos < text.size()) {
    const auto b0 = static_cast<unsigned char>(text[pos]);
    if (b0 == 0xED && pos + 2 < text.size()) {
      const auto b1 = static_cast<unsigned char>(text[pos + 1]);
      const auto b2 = static_cast<unsigned char>(text[pos + 2]);
      if (b1 >= 0xA0 && b1 <= 0xBF && (b2 & 0xC0U) == 0x80U) {
        out.push_back(0xD000U | ((b1 & 0x3FU) << 6U) | (b2 & 0x3FU));
        pos += 3;
        continue;
      }
    }
    const std::size_t length = utf8_sequence_length(text, pos);
    if (length == 0) {
      out.push_back(0xFFFD);
      ++pos;
      continue;
    }
    utf8proc_int32_t code_point = 0;
    utf8proc_iterate(reinterpret_cast<const utf8proc_uint8_t*>(text.data() + pos),
                     static_cast<utf8proc_ssize_t>(length), &code_point);
    out.push_back(static_cast<std::uint32_t>(code_point));
    pos += length;
  }
  return out;
}

/// `str.isprintable()` for one character: not in Other (Cc, Cf, Cs, Co, Cn) or Separator (Zl, Zp,
/// Zs), the ASCII space excepted.
bool printable(std::uint32_t code_point) {
  if (code_point == 0x20) return true;
  if (code_point >= 0xD800 && code_point <= 0xDFFF) return false;
  switch (utf8proc_category(static_cast<utf8proc_int32_t>(code_point))) {
    case UTF8PROC_CATEGORY_CC:
    case UTF8PROC_CATEGORY_CF:
    case UTF8PROC_CATEGORY_CS:
    case UTF8PROC_CATEGORY_CO:
    case UTF8PROC_CATEGORY_CN:
    case UTF8PROC_CATEGORY_ZL:
    case UTF8PROC_CATEGORY_ZP:
    case UTF8PROC_CATEGORY_ZS:
      return false;
    default:
      return true;
  }
}

std::string hex_escape(const char* prefix, int width, std::uint32_t code_point) {
  char buffer[16];
  std::snprintf(buffer, sizeof buffer, "%s%0*x", prefix, width, code_point);
  return buffer;
}

bool integer_lexeme_is_zero(std::string_view lexeme) noexcept {
  for (const char c : lexeme) {
    if (c >= '1' && c <= '9') return false;
  }
  return true;
}

}  // namespace

std::string python_repr(std::string_view text) {
  const std::vector<std::uint32_t> code_points = display_code_points(text);
  bool single = false;
  bool dbl = false;
  for (const std::uint32_t c : code_points) {
    single = single || c == '\'';
    dbl = dbl || c == '"';
  }
  const char quote = (single && !dbl) ? '"' : '\'';
  std::string out(1, quote);
  for (const std::uint32_t c : code_points) {
    if (c == static_cast<std::uint32_t>(quote) || c == '\\') {
      out.push_back('\\');
      out.push_back(static_cast<char>(c));
    } else if (c == '\t') {
      out += "\\t";
    } else if (c == '\n') {
      out += "\\n";
    } else if (c == '\r') {
      out += "\\r";
    } else if (c < 0x20 || c == 0x7F) {
      out += hex_escape("\\x", 2, c);
    } else if (c < 0x7F) {
      out.push_back(static_cast<char>(c));
    } else if (printable(c)) {
      append_code_point_unchecked(out, c);
    } else if (c <= 0xFF) {
      out += hex_escape("\\x", 2, c);
    } else if (c <= 0xFFFF) {
      out += hex_escape("\\u", 4, c);
    } else {
      out += hex_escape("\\U", 8, c);
    }
  }
  out.push_back(quote);
  return out;
}

std::string python_repr(const std::vector<std::string>& texts) {
  std::string out = "[";
  for (std::size_t i = 0; i < texts.size(); ++i) {
    if (i != 0) out += ", ";
    out += python_repr(std::string_view(texts[i]));
  }
  return out + "]";
}

std::string python_repr(const Json& value) {
  switch (value.kind()) {
    case Json::Kind::null:
      return "None";
    case Json::Kind::boolean:
      return value.as_bool() ? "True" : "False";
    case Json::Kind::number: {
      const Json::Number& number = value.as_number();
      if (!number.integer) return python_float_repr(python_float(number.lexeme));
      // A JSON integer has no leading zeros and no plus; only `-0` reads back differently.
      return integer_lexeme_is_zero(number.lexeme) ? std::string("0") : number.lexeme;
    }
    case Json::Kind::string:
      return python_repr(std::string_view(value.as_string()));
    case Json::Kind::array: {
      std::string out = "[";
      bool first = true;
      for (const Json& item : value.as_array()) {
        if (!first) out += ", ";
        first = false;
        out += python_repr(item);
      }
      return out + "]";
    }
    case Json::Kind::object: {
      std::string out = "{";
      bool first = true;
      for (const auto& [key, item] : value.as_object()) {
        if (!first) out += ", ";
        first = false;
        out += python_repr(std::string_view(key));
        out += ": ";
        out += python_repr(item);
      }
      return out + "}";
    }
  }
  return "None";
}

std::string python_type_name(const Json& value) {
  switch (value.kind()) {
    case Json::Kind::null:
      return "NoneType";
    case Json::Kind::boolean:
      return "bool";
    case Json::Kind::number:
      return value.as_number().integer ? "int" : "float";
    case Json::Kind::string:
      return "str";
    case Json::Kind::array:
      return "list";
    case Json::Kind::object:
      return "dict";
  }
  return "NoneType";
}

bool python_truthy(const Json& value) noexcept {
  switch (value.kind()) {
    case Json::Kind::null:
      return false;
    case Json::Kind::boolean:
      return value.as_bool();
    case Json::Kind::number: {
      const Json::Number& number = value.as_number();
      if (number.integer) return !integer_lexeme_is_zero(number.lexeme);
      return python_float(number.lexeme) != 0.0;
    }
    case Json::Kind::string:
      return !value.as_string().empty();
    case Json::Kind::array:
      return !value.as_array().empty();
    case Json::Kind::object:
      return !value.as_object().empty();
  }
  return false;
}

double python_float(std::string_view lexeme) noexcept {
  double value = 0;
  const auto result = std::from_chars(lexeme.data(), lexeme.data() + lexeme.size(), value);
  if (result.ec == std::errc{}) return value;

  // Out of the double range: infinite when the magnitude is above it, zero when below. The decimal
  // exponent of the leading non-zero digit says which, whatever the spelling.
  const bool negative = !lexeme.empty() && lexeme.front() == '-';
  std::string_view digits = negative ? lexeme.substr(1) : lexeme;
  std::string_view exponent;
  if (const auto e = digits.find_first_of("eE"); e != std::string_view::npos) {
    exponent = digits.substr(e + 1);
    digits = digits.substr(0, e);
  }
  const auto point = digits.find('.');
  const std::string_view whole = digits.substr(0, point);
  const std::string_view fraction =
      point == std::string_view::npos ? std::string_view{} : digits.substr(point + 1);
  long long magnitude = 0;
  bool found = false;
  for (std::size_t i = 0; i < whole.size() && !found; ++i) {
    if (whole[i] != '0') {
      magnitude = static_cast<long long>(whole.size() - 1 - i);
      found = true;
    }
  }
  for (std::size_t i = 0; i < fraction.size() && !found; ++i) {
    if (fraction[i] != '0') {
      magnitude = -static_cast<long long>(i + 1);
      found = true;
    }
  }
  long long shift = 0;
  bool exponent_negative = false;
  for (const char c : exponent) {
    if (c == '-') {
      exponent_negative = true;
    } else if (c >= '0' && c <= '9' && shift < 1'000'000'000) {
      shift = shift * 10 + (c - '0');
    }
  }
  const long long total = magnitude + (exponent_negative ? -shift : shift);
  const double result_value = (found && total >= 0) ? std::numeric_limits<double>::infinity() : 0.0;
  return negative ? -result_value : result_value;
}

std::string python_float_repr(double value) {
  if (std::isnan(value)) return "nan";
  if (std::isinf(value)) return value < 0 ? "-inf" : "inf";
  char buffer[64];
  const auto result =
      std::to_chars(buffer, buffer + sizeof buffer, value, std::chars_format::scientific);
  const std::string_view text(buffer, static_cast<std::size_t>(result.ptr - buffer));
  // "-1.2345e+17": sign, the shortest round-trip digits, and the exponent of the first one.
  std::size_t pos = 0;
  std::string sign;
  if (text[pos] == '-') {
    sign = "-";
    ++pos;
  }
  const std::size_t e = text.find('e', pos);
  std::string digits;
  for (std::size_t i = pos; i < e; ++i) {
    if (text[i] != '.') digits.push_back(text[i]);
  }
  int exponent = 0;
  std::from_chars(text.data() + e + 1 + (text[e + 1] == '+' ? 1 : 0), text.data() + text.size(),
                  exponent);
  const int point = exponent + 1;  // digits before the decimal point, in fixed notation
  const int count = static_cast<int>(digits.size());
  if (point <= -4 || point > 16) {
    std::string out = sign + digits.substr(0, 1);
    if (count > 1) out += "." + digits.substr(1);
    out += exponent < 0 ? "e-" : "e+";
    const int magnitude = exponent < 0 ? -exponent : exponent;
    if (magnitude < 10) out.push_back('0');
    return out + std::to_string(magnitude);
  }
  if (point <= 0) return sign + "0." + std::string(static_cast<std::size_t>(-point), '0') + digits;
  if (point >= count) {
    return sign + digits + std::string(static_cast<std::size_t>(point - count), '0') + ".0";
  }
  return sign + digits.substr(0, static_cast<std::size_t>(point)) + "." +
         digits.substr(static_cast<std::size_t>(point));
}

std::string integral_double_digits(double value) {
  const double magnitude = std::fabs(value);
  if (magnitude < 9223372036854775808.0) {  // 2^63, exactly representable
    return std::to_string(static_cast<long long>(value));
  }
  // value = mantissa * 2^shift exactly, with a 53-bit mantissa and a positive shift.
  int exponent = 0;
  const double fraction = std::frexp(magnitude, &exponent);
  auto mantissa = static_cast<std::uint64_t>(std::ldexp(fraction, 53));
  const int shift = exponent - 53;
  std::vector<std::uint32_t> limbs;  // base 10^9, least significant first
  while (mantissa != 0) {
    limbs.push_back(static_cast<std::uint32_t>(mantissa % 1'000'000'000U));
    mantissa /= 1'000'000'000U;
  }
  for (int i = 0; i < shift; ++i) {
    std::uint32_t carry = 0;
    for (std::uint32_t& limb : limbs) {
      const std::uint64_t doubled = static_cast<std::uint64_t>(limb) * 2U + carry;
      limb = static_cast<std::uint32_t>(doubled % 1'000'000'000U);
      carry = static_cast<std::uint32_t>(doubled / 1'000'000'000U);
    }
    if (carry != 0) limbs.push_back(carry);
  }
  std::string out = value < 0 ? "-" : "";
  out += std::to_string(limbs.back());
  for (std::size_t i = limbs.size() - 1; i-- > 0;) {
    char buffer[16];
    std::snprintf(buffer, sizeof buffer, "%09u", limbs[i]);
    out += buffer;
  }
  return out;
}

Json integral_numbers(const Json& value) {
  switch (value.kind()) {
    case Json::Kind::number: {
      const Json::Number& number = value.as_number();
      if (number.integer) return value;
      const double parsed = python_float(number.lexeme);
      if (std::isfinite(parsed) && parsed == std::trunc(parsed)) {
        return Json::from_lexeme(integral_double_digits(parsed));
      }
      return value;
    }
    case Json::Kind::array: {
      Json::Array items;
      items.reserve(value.as_array().size());
      for (const Json& item : value.as_array()) items.push_back(integral_numbers(item));
      return Json(std::move(items));
    }
    case Json::Kind::object: {
      Json::Object members;
      members.reserve(value.as_object().size());
      for (const auto& [key, item] : value.as_object()) {
        members.emplace_back(key, integral_numbers(item));
      }
      return Json(std::move(members));
    }
    default:
      return value;
  }
}

bool python_space(char32_t code_point) noexcept {
  switch (code_point) {
    case 0x09: case 0x0A: case 0x0B: case 0x0C: case 0x0D:
    case 0x1C: case 0x1D: case 0x1E: case 0x1F: case 0x20:
    case 0x85: case 0xA0: case 0x1680:
    case 0x2000: case 0x2001: case 0x2002: case 0x2003: case 0x2004: case 0x2005:
    case 0x2006: case 0x2007: case 0x2008: case 0x2009: case 0x200A:
    case 0x2028: case 0x2029: case 0x202F: case 0x205F: case 0x3000:
      return true;
    default:
      return false;
  }
}

std::size_t python_space_at(std::string_view text, std::size_t pos) noexcept {
  const std::size_t length = utf8_sequence_length(text, pos);
  if (length == 0 || !python_space(decode_sequence(text, pos, length))) return 0;
  return length;
}

std::string_view python_strip(std::string_view text) noexcept {
  std::size_t begin = 0;
  while (begin < text.size()) {
    const std::size_t length = python_space_at(text, begin);
    if (length == 0) break;
    begin += length;
  }
  std::size_t end = text.size();
  while (end > begin) {
    // The last sequence starts at the last byte that is not a continuation byte.
    std::size_t start = end - 1;
    while (start > begin && end - start < 4 &&
           (static_cast<unsigned char>(text[start]) & 0xC0U) == 0x80U) {
      --start;
    }
    if (python_space_at(text, start) != end - start) break;
    end = start;
  }
  return text.substr(begin, end - begin);
}


bool python_printable(char32_t code_point) noexcept {
  return printable(static_cast<std::uint32_t>(code_point));
}

std::string python_bytes_repr(std::string_view bytes) {
  const bool single = bytes.find('\'') != std::string_view::npos;
  const bool dbl = bytes.find('"') != std::string_view::npos;
  const char quote = (single && !dbl) ? '"' : '\'';
  std::string out = "b";
  out.push_back(quote);
  for (const char c : bytes) {
    const auto byte = static_cast<unsigned char>(c);
    if (c == quote || c == '\\') {
      out.push_back('\\');
      out.push_back(c);
    } else if (c == '\t') {
      out += "\\t";
    } else if (c == '\n') {
      out += "\\n";
    } else if (c == '\r') {
      out += "\\r";
    } else if (byte < 0x20 || byte >= 0x7F) {
      out += hex_escape("\\x", 2, byte);
    } else {
      out.push_back(c);
    }
  }
  out.push_back(quote);
  return out;
}

namespace {

/// A moment's fields, from the six-digit text the value types write.
struct Moment {
  long long year = 0;
  long long month = 0;
  long long day = 0;
  long long hour = 0;
  long long minute = 0;
  long long second = 0;
  long long micro = 0;
};

Moment moment_of(const std::string& iso) {
  // YYYY-MM-DDTHH:MM:SS.ffffff, then nothing or a Z.
  Moment out;
  const auto field = [&](std::size_t at, std::size_t length) {
    long long value = 0;
    std::from_chars(iso.data() + at, iso.data() + at + length, value);
    return value;
  };
  out.year = field(0, 4);
  out.month = field(5, 2);
  out.day = field(8, 2);
  out.hour = field(11, 2);
  out.minute = field(14, 2);
  out.second = field(17, 2);
  out.micro = field(20, 6);
  return out;
}

/// `datetime.__repr__`: every field, the microsecond and then the second left out when zero.
std::string moment_repr(const Moment& m, bool utc) {
  std::vector<long long> fields = {m.year, m.month, m.day, m.hour, m.minute, m.second, m.micro};
  if (fields.back() == 0) fields.pop_back();
  if (fields.back() == 0) fields.pop_back();
  std::string out = "datetime.datetime(";
  for (std::size_t i = 0; i < fields.size(); ++i) {
    if (i != 0) out += ", ";
    out += std::to_string(fields[i]);
  }
  return out + (utc ? ", tzinfo=datetime.timezone.utc)" : ")");
}

/// `datetime.isoformat(sep=' ')`: the microsecond only when it is not zero.
std::string moment_str(const std::string& iso, const Moment& m, bool utc) {
  std::string out = iso.substr(0, 10) + " " + iso.substr(11, 8);
  if (m.micro != 0) out += iso.substr(19, 7);
  return out + (utc ? "+00:00" : "");
}

/// `Decimal.__str__`: plain while the exponent is not positive and the adjusted exponent is at
/// least -6, scientific otherwise. A value here never has a positive exponent.
std::string decimal_str(const Decimal& value) {
  const std::string& digits = value.coefficient();
  const long long exponent = -static_cast<long long>(value.scale());
  const long long left = exponent + static_cast<long long>(digits.size());
  const long long dot = left > -6 ? left : 1;
  std::string whole;
  std::string fraction;
  if (dot <= 0) {
    whole = "0";
    fraction = "." + std::string(static_cast<std::size_t>(-dot), '0') + digits;
  } else if (dot >= static_cast<long long>(digits.size())) {
    whole = digits + std::string(static_cast<std::size_t>(dot - static_cast<long long>(digits.size())), '0');
  } else {
    whole = digits.substr(0, static_cast<std::size_t>(dot));
    fraction = "." + digits.substr(static_cast<std::size_t>(dot));
  }
  std::string out = (value.negative() ? "-" : "") + whole + fraction;
  if (left != dot) {
    const long long shown = left - dot;
    out += shown < 0 ? "E-" : "E+";
    out += std::to_string(shown < 0 ? -shown : shown);
  }
  return out;
}

/// The value as the object Python has for it: a document's JSON, or nothing for every other kind.
const Json* document(const Value& value) {
  const auto* wrapped = std::get_if<JsonDocument>(&value);
  return wrapped == nullptr ? nullptr : &wrapped->document;
}

/// The `TypeError` of `int()`, which names the type as its C structure does (`tp_name`).
[[noreturn]] void type_error(const std::string& type) {
  throw EngineError("int() argument must be a string, a bytes-like object or a real number, not '" +
                    type + "'");
}

/// `int(text)` with CPython's messages: the repr cut to 200 characters, and the digit limit.
python_url::PythonInt int_of_text(std::string_view text, const std::string& shown) {
  try {
    return python_url::int_of(text);
  } catch (const python_url::PythonValueError&) {
    const std::vector<std::uint32_t> code_points = display_code_points(shown);
    std::string cut;
    for (std::size_t i = 0; i < code_points.size() && i < 200; ++i) {
      append_code_point_unchecked(cut, code_points[i]);
    }
    throw EngineError("invalid literal for int() with base 10: " + cut);
  } catch (const python_url::PythonIntLimit& limit) {
    throw EngineError("Exceeds the limit (4300 digits) for integer string conversion: value has " +
                      std::to_string(limit.digits) +
                      " digits; use sys.set_int_max_str_digits() to increase the limit");
  }
}

python_url::PythonInt int_of_double(double value) {
  if (std::isnan(value)) throw EngineError("cannot convert float NaN to integer");
  if (std::isinf(value)) throw EngineError("cannot convert float infinity to integer");
  const double whole = std::trunc(value);
  python_url::PythonInt out;
  out.negative = whole < 0;
  out.digits = integral_double_digits(std::fabs(whole));
  if (out.digits == "0") out.negative = false;
  return out;
}

python_url::PythonInt int_of_digits(bool negative, std::string digits) {
  const std::size_t first = digits.find_first_not_of('0');
  python_url::PythonInt out;
  out.digits = first == std::string::npos ? "0" : digits.substr(first);
  out.negative = negative && out.digits != "0";
  return out;
}

}  // namespace

std::string python_value_repr(const Value& value) {
  if (const Json* json = document(value)) return python_repr(*json);
  return std::visit(
      [](const auto& item) -> std::string {
        using T = std::decay_t<decltype(item)>;
        if constexpr (std::is_same_v<T, Null>) {
          return "None";
        } else if constexpr (std::is_same_v<T, bool>) {
          return item ? "True" : "False";
        } else if constexpr (std::is_same_v<T, std::int64_t>) {
          return std::to_string(item);
        } else if constexpr (std::is_same_v<T, double>) {
          return python_float_repr(item);
        } else if constexpr (std::is_same_v<T, Decimal>) {
          return "Decimal('" + decimal_str(item) + "')";
        } else if constexpr (std::is_same_v<T, std::string>) {
          return python_repr(std::string_view(item));
        } else if constexpr (std::is_same_v<T, Bytes>) {
          return python_bytes_repr(std::string_view(
              reinterpret_cast<const char*>(item.data.data()), item.data.size()));
        } else if constexpr (std::is_same_v<T, Uuid>) {
          return "UUID('" + item.to_string() + "')";
        } else if constexpr (std::is_same_v<T, Date>) {
          const Moment m = moment_of(item.to_string() + "T00:00:00.000000");
          return "datetime.date(" + std::to_string(m.year) + ", " + std::to_string(m.month) + ", " +
                 std::to_string(m.day) + ")";
        } else if constexpr (std::is_same_v<T, Timestamp>) {
          return moment_repr(moment_of(item.to_string()), false);
        } else if constexpr (std::is_same_v<T, TimestampTz>) {
          return moment_repr(moment_of(item.to_string()), true);
        } else {
          return python_repr(item.document);
        }
      },
      value);
}

std::string python_value_type_name(const Value& value) {
  if (const Json* json = document(value)) return python_type_name(*json);
  static constexpr const char* kNames[] = {"NoneType", "bool", "int",   "float", "Decimal",
                                           "str",      "bytes", "UUID", "date",  "datetime",
                                           "datetime", "dict"};
  static_assert(std::size(kNames) == std::variant_size_v<Value>);
  return kNames[value.index()];
}

std::string python_value_str(const Value& value) {
  if (const Json* json = document(value)) {
    if (json->kind() == Json::Kind::string) return json->as_string();
    if (json->kind() == Json::Kind::number && json->as_number().integer) {
      const std::string& lexeme = json->as_number().lexeme;
      const python_url::PythonInt integer =
          int_of_digits(lexeme.starts_with('-'), lexeme.substr(lexeme.starts_with('-') ? 1 : 0));
      return (integer.negative ? "-" : "") + integer.digits;
    }
    return python_repr(*json);
  }
  if (const auto* text = std::get_if<std::string>(&value)) return *text;
  if (const auto* number = std::get_if<Decimal>(&value)) return decimal_str(*number);
  if (const auto* uuid = std::get_if<Uuid>(&value)) return uuid->to_string();
  if (const auto* date = std::get_if<Date>(&value)) return date->to_string();
  if (const auto* stamp = std::get_if<Timestamp>(&value)) {
    const std::string iso = stamp->to_string();
    return moment_str(iso, moment_of(iso), false);
  }
  if (const auto* instant = std::get_if<TimestampTz>(&value)) {
    const std::string iso = instant->to_string();
    return moment_str(iso, moment_of(iso), true);
  }
  return python_value_repr(value);
}

python_url::PythonInt python_value_int(const Value& value) {
  if (const Json* json = document(value)) {
    switch (json->kind()) {
      case Json::Kind::null:
        type_error("NoneType");
      case Json::Kind::boolean:
        return int_of_digits(false, json->as_bool() ? "1" : "0");
      case Json::Kind::number: {
        const Json::Number& number = json->as_number();
        if (!number.integer) return int_of_double(python_float(number.lexeme));
        const bool negative = number.lexeme.starts_with('-');
        return int_of_digits(negative, number.lexeme.substr(negative ? 1 : 0));
      }
      case Json::Kind::string:
        return int_of_text(json->as_string(), python_repr(std::string_view(json->as_string())));
      case Json::Kind::array:
        type_error("list");
      case Json::Kind::object:
        type_error("dict");
    }
  }
  if (std::holds_alternative<Null>(value)) type_error("NoneType");
  if (const auto* flag = std::get_if<bool>(&value)) return int_of_digits(false, *flag ? "1" : "0");
  if (const auto* integer = std::get_if<std::int64_t>(&value)) {
    const bool negative = *integer < 0;
    const unsigned long long magnitude =
        negative ? 0ULL - static_cast<unsigned long long>(*integer)
                 : static_cast<unsigned long long>(*integer);
    return int_of_digits(negative, std::to_string(magnitude));
  }
  if (const auto* real = std::get_if<double>(&value)) return int_of_double(*real);
  if (const auto* number = std::get_if<Decimal>(&value)) {
    const std::string& digits = number->coefficient();
    const auto scale = static_cast<std::size_t>(number->scale());
    return int_of_digits(number->negative(),
                         scale >= digits.size() ? "0" : digits.substr(0, digits.size() - scale));
  }
  if (const auto* text = std::get_if<std::string>(&value)) {
    return int_of_text(*text, python_repr(std::string_view(*text)));
  }
  if (const auto* bytes = std::get_if<Bytes>(&value)) {
    const std::string_view raw(reinterpret_cast<const char*>(bytes->data.data()),
                               bytes->data.size());
    const std::string shown = python_bytes_repr(raw);
    // `int(bytes)` reads ASCII only: no Unicode digit or space is a byte.
    for (const char c : raw) {
      if (static_cast<unsigned char>(c) >= 0x80) (void)int_of_text("x", shown);
    }
    return int_of_text(raw, shown);
  }
  if (const auto* uuid = std::get_if<Uuid>(&value)) {
    // `UUID.__int__`: the sixteen bytes as one big-endian number, divided down by ten.
    std::array<std::uint8_t, 16> number = uuid->bytes();
    std::string digits;
    bool zero = false;
    while (!zero) {
      unsigned remainder = 0;
      zero = true;
      for (std::uint8_t& byte : number) {
        const unsigned current = (remainder << 8U) | byte;
        byte = static_cast<std::uint8_t>(current / 10U);
        remainder = current % 10U;
        zero = zero && byte == 0;
      }
      digits.insert(digits.begin(), static_cast<char>('0' + remainder));
    }
    return int_of_digits(false, digits);
  }
  // What is left is a date or a moment, which `int()` names by the C type's own full name.
  type_error(std::holds_alternative<Date>(value) ? "datetime.date" : "datetime.datetime");
}

std::optional<std::int64_t> to_int64(const python_url::PythonInt& value) noexcept {
  if (value.digits.size() > 19) return std::nullopt;
  unsigned long long magnitude = 0;
  for (const char c : value.digits) magnitude = magnitude * 10 + static_cast<unsigned>(c - '0');
  if (value.negative) {
    if (magnitude > 9223372036854775808ULL) return std::nullopt;
    return static_cast<std::int64_t>(0ULL - magnitude);
  }
  if (magnitude > 9223372036854775807ULL) return std::nullopt;
  return static_cast<std::int64_t>(magnitude);
}

}  // namespace sde::detail

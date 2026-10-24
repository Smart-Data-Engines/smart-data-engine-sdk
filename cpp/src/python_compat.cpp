#include "python_compat.hpp"

#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <limits>
#include <system_error>

#include <utf8proc.h>

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

}  // namespace sde::detail

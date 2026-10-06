#include "sde/json.hpp"

#include <algorithm>
#include <array>
#include <charconv>
#include <cmath>
#include <limits>
#include <system_error>

#include "unicode_internal.hpp"

namespace sde {

JsonError::~JsonError() = default;

namespace {

/// Nesting deeper than this is refused rather than recursed into: every document of the contract is
/// a few levels deep, and a parser that follows a hostile document's depth exhausts the stack.
constexpr int kMaxDepth = 512;

[[noreturn]] void fail(std::size_t offset, const std::string& what) {
  throw JsonError("not JSON at byte " + std::to_string(offset) + ": " + what);
}

bool is_json_number(std::string_view text) {
  std::size_t i = 0;
  const std::size_t n = text.size();
  if (i < n && text[i] == '-') ++i;
  if (i >= n) return false;
  if (text[i] == '0') {
    ++i;
  } else if (text[i] >= '1' && text[i] <= '9') {
    while (i < n && text[i] >= '0' && text[i] <= '9') ++i;
  } else {
    return false;
  }
  if (i < n && text[i] == '.') {
    ++i;
    const std::size_t digits = i;
    while (i < n && text[i] >= '0' && text[i] <= '9') ++i;
    if (i == digits) return false;
  }
  if (i < n && (text[i] == 'e' || text[i] == 'E')) {
    ++i;
    if (i < n && (text[i] == '+' || text[i] == '-')) ++i;
    const std::size_t digits = i;
    while (i < n && text[i] >= '0' && text[i] <= '9') ++i;
    if (i == digits) return false;
  }
  return i == n;
}

class Parser {
 public:
  explicit Parser(std::string_view text) : text_(text) {}

  Json document() {
    if (text_.size() >= 3 && static_cast<unsigned char>(text_[0]) == 0xEF &&
        static_cast<unsigned char>(text_[1]) == 0xBB && static_cast<unsigned char>(text_[2]) == 0xBF) {
      fail(0, "a byte order mark; the contract's documents are UTF-8 without one");
    }
    skip_whitespace();
    Json value = parse_value(0);
    skip_whitespace();
    if (pos_ != text_.size()) fail(pos_, "extra data after the value");
    return value;
  }

 private:
  std::string_view text_;
  std::size_t pos_ = 0;

  [[nodiscard]] bool at_end() const { return pos_ >= text_.size(); }
  [[nodiscard]] char peek() const { return text_[pos_]; }

  void skip_whitespace() {
    while (!at_end()) {
      const char c = peek();
      if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
        ++pos_;
      } else {
        break;
      }
    }
  }

  void expect_literal(std::string_view literal) {
    if (text_.substr(pos_, literal.size()) != literal) fail(pos_, "an unexpected character");
    pos_ += literal.size();
  }

  Json parse_value(int depth) {
    if (depth > kMaxDepth) fail(pos_, "nested more deeply than " + std::to_string(kMaxDepth));
    if (at_end()) fail(pos_, "the text ends where a value should be");
    switch (peek()) {
      case '{':
        return parse_object(depth);
      case '[':
        return parse_array(depth);
      case '"':
        return Json(parse_string());
      case 't':
        expect_literal("true");
        return Json(true);
      case 'f':
        expect_literal("false");
        return Json(false);
      case 'n':
        expect_literal("null");
        return Json(nullptr);
      default:
        return parse_number();
    }
  }

  Json parse_object(int depth) {
    ++pos_;  // '{'
    Json::Object members;
    skip_whitespace();
    if (!at_end() && peek() == '}') {
      ++pos_;
      return Json(std::move(members));
    }
    while (true) {
      skip_whitespace();
      if (at_end() || peek() != '"') fail(pos_, "an object member must begin with a string key");
      std::string key = parse_string();
      skip_whitespace();
      if (at_end() || peek() != ':') fail(pos_, "a ':' must follow an object key");
      ++pos_;
      skip_whitespace();
      Json value = parse_value(depth + 1);
      // A byte-identical duplicate keeps the last value at the first position, as Python's dict
      // and JavaScript objects do; keys that differ only in composition stay two members.
      auto existing = std::find_if(members.begin(), members.end(),
                                   [&](const Json::Member& member) { return member.first == key; });
      if (existing != members.end()) {
        existing->second = std::move(value);
      } else {
        members.emplace_back(std::move(key), std::move(value));
      }
      skip_whitespace();
      if (at_end()) fail(pos_, "the text ends inside an object");
      if (peek() == ',') {
        ++pos_;
        continue;
      }
      if (peek() == '}') {
        ++pos_;
        return Json(std::move(members));
      }
      fail(pos_, "a ',' or '}' must follow an object member");
    }
  }

  Json parse_array(int depth) {
    ++pos_;  // '['
    Json::Array items;
    skip_whitespace();
    if (!at_end() && peek() == ']') {
      ++pos_;
      return Json(std::move(items));
    }
    while (true) {
      skip_whitespace();
      items.push_back(parse_value(depth + 1));
      skip_whitespace();
      if (at_end()) fail(pos_, "the text ends inside an array");
      if (peek() == ',') {
        ++pos_;
        continue;
      }
      if (peek() == ']') {
        ++pos_;
        return Json(std::move(items));
      }
      fail(pos_, "a ',' or ']' must follow an array element");
    }
  }

  unsigned hex4() {
    if (pos_ + 4 > text_.size()) fail(pos_, "a \\u escape needs four hexadecimal digits");
    unsigned value = 0;
    for (int i = 0; i < 4; ++i) {
      const char c = text_[pos_++];
      value <<= 4U;
      if (c >= '0' && c <= '9') {
        value |= static_cast<unsigned>(c - '0');
      } else if (c >= 'a' && c <= 'f') {
        value |= static_cast<unsigned>(c - 'a' + 10);
      } else if (c >= 'A' && c <= 'F') {
        value |= static_cast<unsigned>(c - 'A' + 10);
      } else {
        fail(pos_ - 1, "a \\u escape needs four hexadecimal digits");
      }
    }
    return value;
  }

  std::string parse_string() {
    ++pos_;  // opening quote
    std::string out;
    while (true) {
      if (at_end()) fail(pos_, "the text ends inside a string");
      const auto byte = static_cast<unsigned char>(peek());
      if (byte == '"') {
        ++pos_;
        return out;
      }
      if (byte < 0x20) fail(pos_, "a control character inside a string must be escaped");
      if (byte == '\\') {
        ++pos_;
        if (at_end()) fail(pos_, "the text ends inside an escape");
        const char escape = text_[pos_++];
        switch (escape) {
          case '"': out.push_back('"'); break;
          case '\\': out.push_back('\\'); break;
          case '/': out.push_back('/'); break;
          case 'b': out.push_back('\b'); break;
          case 'f': out.push_back('\f'); break;
          case 'n': out.push_back('\n'); break;
          case 'r': out.push_back('\r'); break;
          case 't': out.push_back('\t'); break;
          case 'u': {
            std::uint32_t code = hex4();
            if (code >= 0xD800 && code <= 0xDBFF && pos_ + 6 <= text_.size() &&
                text_[pos_] == '\\' && text_[pos_ + 1] == 'u') {
              const std::size_t restore = pos_;
              pos_ += 2;
              const std::uint32_t low = hex4();
              if (low >= 0xDC00 && low <= 0xDFFF) {
                code = 0x10000 + ((code - 0xD800) << 10U) + (low - 0xDC00);
              } else {
                pos_ = restore;  // a lone high surrogate; the next escape is read on its own
              }
            }
            // A lone surrogate is kept as its three-byte sequence, which is not UTF-8: the
            // placement-map loader refuses it by name (contract section 7f) instead of the parser.
            detail::append_code_point_unchecked(out, code);
            break;
          }
          default:
            fail(pos_ - 1, "an unknown escape");
        }
        continue;
      }
      // Raw bytes must be UTF-8: a document whose bytes are not text is refused here.
      const std::size_t length = detail::utf8_sequence_length(text_, pos_);
      if (length == 0) fail(pos_, "bytes that are not UTF-8");
      out.append(text_.substr(pos_, length));
      pos_ += length;
    }
  }

  Json parse_number() {
    const std::size_t start = pos_;
    while (!at_end()) {
      const char c = peek();
      if ((c >= '0' && c <= '9') || c == '-' || c == '+' || c == '.' || c == 'e' || c == 'E') {
        ++pos_;
      } else {
        break;
      }
    }
    const std::string_view lexeme = text_.substr(start, pos_ - start);
    if (lexeme.empty()) fail(start, "an unexpected character");
    if (!is_json_number(lexeme)) fail(start, "a malformed number");
    return Json::from_lexeme(std::string(lexeme));
  }
};

void escape_into(std::string& out, std::string_view text) {
  out.push_back('"');
  for (const char c : text) {
    const auto byte = static_cast<unsigned char>(c);
    switch (byte) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case 0x08: out += "\\b"; break;
      case 0x09: out += "\\t"; break;
      case 0x0A: out += "\\n"; break;
      case 0x0C: out += "\\f"; break;
      case 0x0D: out += "\\r"; break;
      default:
        if (byte < 0x20) {
          static constexpr std::array<char, 16> kHex{'0', '1', '2', '3', '4', '5', '6', '7',
                                                     '8', '9', 'a', 'b', 'c', 'd', 'e', 'f'};
          out += "\\u00";
          out.push_back(kHex[byte >> 4U]);
          out.push_back(kHex[byte & 0x0FU]);
        } else {
          out.push_back(c);
        }
    }
  }
  out.push_back('"');
}

void dump_into(std::string& out, const Json& value) {
  switch (value.kind()) {
    case Json::Kind::null:
      out += "null";
      return;
    case Json::Kind::boolean:
      out += value.as_bool() ? "true" : "false";
      return;
    case Json::Kind::number:
      out += value.as_number().lexeme;
      return;
    case Json::Kind::string:
      escape_into(out, value.as_string());
      return;
    case Json::Kind::array: {
      out.push_back('[');
      bool first = true;
      for (const Json& item : value.as_array()) {
        if (!first) out.push_back(',');
        first = false;
        dump_into(out, item);
      }
      out.push_back(']');
      return;
    }
    case Json::Kind::object: {
      out.push_back('{');
      bool first = true;
      for (const auto& [key, item] : value.as_object()) {
        if (!first) out.push_back(',');
        first = false;
        escape_into(out, key);
        out.push_back(':');
        dump_into(out, item);
      }
      out.push_back('}');
      return;
    }
  }
}

[[noreturn]] void wrong_kind(const char* wanted) {
  throw JsonError(std::string("a JSON value is not ") + wanted);
}

}  // namespace

Json::Json(double value) {
  if (!std::isfinite(value)) {
    throw JsonError("a non-finite number has no JSON form");
  }
  std::array<char, 64> buffer{};
  const auto result = std::to_chars(buffer.data(), buffer.data() + buffer.size(), value);
  if (result.ec != std::errc{}) throw JsonError("a number could not be written");
  value_ = Number{std::string(buffer.data(), result.ptr), false};
}

Json Json::from_lexeme(std::string lexeme) {
  if (!is_json_number(lexeme)) throw JsonError("not a JSON number: " + lexeme);
  const bool integer = lexeme.find_first_of(".eE") == std::string::npos;
  Json result;
  result.value_ = Number{std::move(lexeme), integer};
  return result;
}

bool Json::as_bool() const {
  if (const auto* value = std::get_if<bool>(&value_)) return *value;
  wrong_kind("a boolean");
}

const Json::Number& Json::as_number() const {
  if (const auto* value = std::get_if<Number>(&value_)) return *value;
  wrong_kind("a number");
}

const std::string& Json::as_string() const {
  if (const auto* value = std::get_if<std::string>(&value_)) return *value;
  wrong_kind("a string");
}

const Json::Array& Json::as_array() const {
  if (const auto* value = std::get_if<Array>(&value_)) return *value;
  wrong_kind("an array");
}

Json::Array& Json::as_array() {
  if (auto* value = std::get_if<Array>(&value_)) return *value;
  wrong_kind("an array");
}

const Json::Object& Json::as_object() const {
  if (const auto* value = std::get_if<Object>(&value_)) return *value;
  wrong_kind("an object");
}

Json::Object& Json::as_object() {
  if (auto* value = std::get_if<Object>(&value_)) return *value;
  wrong_kind("an object");
}

const Json* Json::find(std::string_view key) const noexcept {
  const auto* members = std::get_if<Object>(&value_);
  if (members == nullptr) return nullptr;
  for (const auto& [name, value] : *members) {
    if (name == key) return &value;
  }
  return nullptr;
}

Json* Json::find(std::string_view key) noexcept {
  auto* members = std::get_if<Object>(&value_);
  if (members == nullptr) return nullptr;
  for (auto& [name, value] : *members) {
    if (name == key) return &value;
  }
  return nullptr;
}

void Json::set(std::string key, Json value) {
  Object& members = as_object();
  for (auto& [name, existing] : members) {
    if (name == key) {
      existing = std::move(value);
      return;
    }
  }
  members.emplace_back(std::move(key), std::move(value));
}

bool Json::erase(std::string_view key) {
  Object& members = as_object();
  const auto found = std::find_if(members.begin(), members.end(),
                                  [&](const Member& member) { return member.first == key; });
  if (found == members.end()) return false;
  members.erase(found);
  return true;
}

std::optional<std::int64_t> Json::to_int64() const noexcept {
  const auto* number = std::get_if<Number>(&value_);
  if (number == nullptr || !number->integer) return std::nullopt;
  std::int64_t value = 0;
  const char* first = number->lexeme.data();
  const char* last = first + number->lexeme.size();
  const auto result = std::from_chars(first, last, value);
  if (result.ec != std::errc{} || result.ptr != last) return std::nullopt;
  return value;
}

std::optional<double> Json::to_double() const noexcept {
  const auto* number = std::get_if<Number>(&value_);
  if (number == nullptr) return std::nullopt;
  double value = 0;
  const char* first = number->lexeme.data();
  const char* last = first + number->lexeme.size();
  const auto result = std::from_chars(first, last, value);
  if (result.ec != std::errc{} || result.ptr != last) return std::nullopt;
  return value;
}

bool operator==(const Json& left, const Json& right) {
  if (left.kind() != right.kind()) return false;
  switch (left.kind()) {
    case Json::Kind::null:
      return true;
    case Json::Kind::boolean:
      return left.as_bool() == right.as_bool();
    case Json::Kind::number: {
      const auto& a = left.as_number();
      const auto& b = right.as_number();
      if (a.integer && b.integer) {
        const auto x = left.to_int64();
        const auto y = right.to_int64();
        if (x && y) return *x == *y;
        return a.lexeme == b.lexeme;
      }
      return left.to_double() == right.to_double();
    }
    case Json::Kind::string:
      return left.as_string() == right.as_string();
    case Json::Kind::array:
      return left.as_array() == right.as_array();
    case Json::Kind::object: {
      const auto& a = left.as_object();
      const auto& b = right.as_object();
      if (a.size() != b.size()) return false;
      for (const auto& [key, value] : a) {
        const Json* other = right.find(key);
        if (other == nullptr || !(*other == value)) return false;
      }
      return true;
    }
  }
  return false;
}

Json parse_json(std::string_view text) { return Parser(text).document(); }

std::string dump_json(const Json& value) {
  std::string out;
  dump_into(out, value);
  return out;
}

}  // namespace sde

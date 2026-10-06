#include "sde/canonical.hpp"

#include <algorithm>
#include <array>
#include <cstdint>
#include <utility>
#include <vector>

#include "crypto.hpp"
#include "sde/errors.hpp"
#include "sde/unicode.hpp"

namespace sde {

namespace {

void escape_into(std::string& out, std::string_view text) {
  static constexpr std::array<char, 16> kHex{'0', '1', '2', '3', '4', '5', '6', '7',
                                             '8', '9', 'a', 'b', 'c', 'd', 'e', 'f'};
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
          out += "\\u00";
          out.push_back(kHex[byte >> 4U]);
          out.push_back(kHex[byte & 0x0FU]);
        } else {
          // Everything else is raw UTF-8: no escape for non-ASCII, '/', U+2028 or U+2029, any of
          // which a general JSON writer might emit and every one of which changes the hash.
          out.push_back(c);
        }
    }
  }
  out.push_back('"');
}

std::string normalised(std::string_view text, const std::string& path) {
  if (!is_scalar_text(text)) {
    throw CanonicalError("text at " + path +
                         " is not well-formed UTF-8 of Unicode scalar values (an unpaired "
                         "surrogate, perhaps); it has no canonical form");
  }
  return nfc(text);
}

void encode(std::string& out, const Json& value, const std::string& path) {
  switch (value.kind()) {
    case Json::Kind::null:
      out += "null";
      return;
    case Json::Kind::boolean:
      out += value.as_bool() ? "true" : "false";
      return;
    case Json::Kind::number: {
      if (!value.as_number().integer) {
        throw CanonicalError(
            "float at " + path +
            ": floating point is not representable in canonical form, because its textual form "
            "differs between languages. Use an integer, or a decimal string, or the name of a "
            "float type if you meant to describe a type.");
      }
      const auto integer = value.to_int64();
      if (!integer) {
        throw CanonicalError("integer at " + path +
                             " is outside the 64-bit range this library represents; refused "
                             "rather than truncated");
      }
      out += std::to_string(*integer);  // the shortest form: no '+', no leading zeros, -0 is 0
      return;
    }
    case Json::Kind::string:
      escape_into(out, normalised(value.as_string(), path));
      return;
    case Json::Kind::array: {
      out.push_back('[');
      std::size_t index = 0;
      for (const Json& item : value.as_array()) {
        if (index != 0) out.push_back(',');
        encode(out, item, path + "[" + std::to_string(index) + "]");
        ++index;
      }
      out.push_back(']');
      return;
    }
    case Json::Kind::object: {
      // NFC first, then code point order: sorting first would put "e" + U+0301 under "e" and the
      // composed character at U+00E9, two positions for what must be one key.
      std::vector<std::pair<std::string, const Json*>> members;
      members.reserve(value.as_object().size());
      for (const auto& [key, item] : value.as_object()) {
        members.emplace_back(normalised(key, path), &item);
      }
      std::sort(members.begin(), members.end(), [](const auto& left, const auto& right) {
        return compare_code_points(left.first, right.first) < 0;
      });
      out.push_back('{');
      for (std::size_t i = 0; i < members.size(); ++i) {
        if (i != 0) {
          if (members[i].first == members[i - 1].first) {
            throw CanonicalError("duplicate key \"" + members[i].first + "\" at " + path +
                                 " after NFC normalisation: two keys that differ only in Unicode "
                                 "composition are the same key here");
          }
          out.push_back(',');
        }
        escape_into(out, members[i].first);
        out.push_back(':');
        encode(out, *members[i].second, path + "." + members[i].first);
      }
      out.push_back('}');
      return;
    }
  }
}

}  // namespace

std::string canonical_bytes(const Json& value) {
  std::string out;
  encode(out, value, "$");
  return out;
}

std::string sha256_hex(std::string_view bytes) {
  const auto digest = detail::sha256(bytes);
  return detail::to_hex(digest);
}

std::string digest16(std::string_view bytes) { return sha256_hex(bytes).substr(0, 16); }

}  // namespace sde

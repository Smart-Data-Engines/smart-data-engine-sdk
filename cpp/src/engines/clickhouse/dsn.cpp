#include "engines/clickhouse/dsn.hpp"

#include <algorithm>
#include <array>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <map>
#include <string>
#include <system_error>
#include <vector>

#include "sde/errors.hpp"
#include "sde/unicode.hpp"
#include "python_url.hpp"
#include "unicode_internal.hpp"

namespace sde::detail::clickhouse {

namespace {

constexpr double kMinTimeout = 0.001;
constexpr double kMaxTimeout = 2147483.647;

/// What the reference reports for anything its own rules do not name - an encoding, an authority
/// or a setting Python's parsers refuse with their own error.
constexpr const char* kMalformed = "Invalid ClickHouse URI encoding, authority or connection setting";

/// A refusal of Python's parsers rather than of a rule: reported as `kMalformed`.
using Malformed = python_url::PythonValueError;
using python_url::ascii;
using python_url::Errors;
using python_url::hex_digit;
using python_url::hostname;
using python_url::ipv4;
using python_url::ipv6;
using python_url::ipv6_format;
using python_url::lower;
using python_url::Split;
using python_url::urlsplit;

void require(bool condition, const char* message) {
  if (!condition) throw EngineError(message);
}

/// Python's `str.isspace()`: Unicode's White_Space and U+001C to U+001F.
bool python_space(char32_t code_point) noexcept {
  return is_white_space(code_point) || (code_point >= 0x1C && code_point <= 0x1F);
}

/// `_no_controls`: nothing below U+0020, and not U+007F.
void no_controls(const std::u32string& text) {
  for (const char32_t c : text) {
    require(c >= 32 && c != 127, "ClickHouse connection fields cannot contain control characters");
  }
}

std::u32string decoded(std::string_view text) {
  std::optional<std::u32string> code_points = decode_utf8(text);
  if (!code_points) throw Malformed{};
  return *code_points;
}

/// `_decode`: the bytes, as strict UTF-8, without control characters.
std::string decode_field(std::string_view text) {
  const std::string bytes = python_url::unquote_to_bytes(text);
  no_controls(decoded(bytes));
  return bytes;
}

/// `_host`: a DNS name or an IP address, normalised as `ipaddress` writes it.
std::string host_of(std::string_view value) {
  require(ascii(value) && value.find('%') == std::string_view::npos,
          "ClickHouse host must be ASCII DNS or an IP address");
  const std::string host = lower(value);
  if (ipv4(host)) return host;
  if (const auto words = ipv6(host)) return ipv6_format(*words);
  // (?:[0-9]+|0x[0-9a-f]+)(?:\.(?:[0-9]+|0x[0-9a-f]+))*\.?
  const auto numeric = [](std::string_view text) {
    if (!text.empty() && text.back() == '.') text.remove_suffix(1);
    if (text.empty()) return false;
    std::size_t start = 0;
    while (true) {
      const std::size_t dot = text.find('.', start);
      const std::string_view piece =
          text.substr(start, dot == std::string_view::npos ? std::string_view::npos : dot - start);
      bool decimal = !piece.empty();
      for (const char c : piece) decimal = decimal && c >= '0' && c <= '9';
      bool hexadecimal = piece.size() > 2 && piece[0] == '0' && piece[1] == 'x';
      for (std::size_t i = 2; hexadecimal && i < piece.size(); ++i) {
        hexadecimal = (piece[i] >= '0' && piece[i] <= '9') || (piece[i] >= 'a' && piece[i] <= 'f');
      }
      if (!decimal && !hexadecimal) return false;
      if (dot == std::string_view::npos) return true;
      start = dot + 1;
    }
  };
  require(!numeric(host), "Legacy numeric IPv4 forms are not supported");
  std::string_view labels = host;
  if (!labels.empty() && labels.back() == '.') labels.remove_suffix(1);
  bool valid = host.size() <= 253;
  std::size_t start = 0;
  while (valid) {
    const std::size_t dot = labels.find('.', start);
    const std::string_view label =
        labels.substr(start, dot == std::string_view::npos ? std::string_view::npos : dot - start);
    // [a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?
    const auto alnum = [](char c) { return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9'); };
    valid = !label.empty() && label.size() <= 63 && alnum(label.front()) && alnum(label.back());
    for (const char c : label) valid = valid && (alnum(c) || c == '-');
    if (dot == std::string_view::npos) break;
    start = dot + 1;
  }
  require(valid, "ClickHouse host is not a valid DNS name or IP address");
  return host;
}

/// `parse_qsl(keep_blank_values=True, strict_parsing=True, errors="strict")`.
std::vector<std::pair<std::string, std::string>> parse_qsl(std::string_view query) {
  return python_url::parse_qsl(query, true, true, Errors::strict);
}

/// `_seconds`: positive finite seconds between a millisecond and the largest the server takes.
double seconds(const std::optional<std::string>& value, double fallback) {
  if (!value) return fallback;
  // (?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?
  const std::string& text = *value;
  std::size_t i = 0;
  const auto digits = [&] {
    const std::size_t from = i;
    while (i < text.size() && text[i] >= '0' && text[i] <= '9') ++i;
    return i - from;
  };
  bool valid = true;
  const std::size_t whole = digits();
  if (i < text.size() && text[i] == '.') {
    ++i;
    const std::size_t fraction = digits();
    valid = whole > 0 || fraction > 0;
  } else {
    valid = whole > 0;
  }
  if (valid && i < text.size() && (text[i] == 'e' || text[i] == 'E')) {
    ++i;
    if (i < text.size() && (text[i] == '+' || text[i] == '-')) ++i;
    valid = digits() > 0;
  }
  valid = valid && i == text.size();
  require(valid, "ClickHouse timeout must be positive finite seconds");
  double result = 0;
  const auto [end, problem] = std::from_chars(text.data(), text.data() + text.size(), result);
  if (problem == std::errc::result_out_of_range) {
    // Python's float() reads an overflow as infinity and an underflow as zero; both are refused.
    result = 0;
  }
  require(std::isfinite(result) && result >= kMinTimeout && result <= kMaxTimeout,
          "ClickHouse timeout must be positive finite seconds");
  return result;
}

Target parse(std::string_view dsn) {
  require(!dsn.empty(), "Provide a ClickHouse connection URI");
  const std::u32string text = decoded(dsn);
  for (const char32_t c : text) require(!python_space(c), "Encode whitespace in connection URIs");
  no_controls(text);
  bool malformed_percent = false;
  for (std::size_t i = 0; i < dsn.size(); ++i) {
    if (dsn[i] == '%' && !(i + 2 < dsn.size() && hex_digit(dsn[i + 1]) && hex_digit(dsn[i + 2]))) {
      malformed_percent = true;
    }
  }
  require(dsn.find('#') == std::string_view::npos && dsn.find('\\') == std::string_view::npos &&
              !malformed_percent,
          "ClickHouse URI fragments, raw backslashes and malformed percent escapes are refused");
  const Split parsed = urlsplit(dsn);
  const std::string& scheme = parsed.scheme;
  require((scheme == "clickhouse" || scheme == "clickhouses" || scheme == "http" ||
           scheme == "https") &&
              !parsed.netloc.empty(),
          "Unsupported ClickHouse URI scheme or authority");
  const std::optional<std::string> name = hostname(parsed.netloc);
  require(name.has_value() &&
              std::count(parsed.netloc.begin(), parsed.netloc.end(), '@') <= 1,
          "ClickHouse URI needs one unambiguous host");
  const std::size_t at = parsed.netloc.rfind('@');
  const std::string authority = at == std::string::npos ? parsed.netloc : parsed.netloc.substr(at + 1);
  std::optional<std::string> declared_port;
  if (!authority.empty() && authority[0] == '[') {
    const std::size_t closing = authority.find(']');
    require(closing != std::string::npos && closing > 0 &&
                (authority.size() == closing + 1 || authority[closing + 1] == ':'),
            "ClickHouse IPv6 authority is malformed");
    if (authority.size() > closing + 1) declared_port = authority.substr(closing + 2);
  } else {
    require(std::count(authority.begin(), authority.end(), ':') <= 1,
            "Enclose IPv6 hosts in URI brackets");
    if (const std::size_t colon = authority.rfind(':'); colon != std::string::npos) {
      declared_port = authority.substr(colon + 1);
    }
  }
  std::optional<int> port;
  if (declared_port) {
    bool digits = !declared_port->empty();
    for (const char c : *declared_port) digits = digits && c >= '0' && c <= '9';
    require(digits, "ClickHouse port must be an explicit positive integer");
    std::size_t first = declared_port->find_first_not_of('0');
    const std::string significant = first == std::string::npos ? "0" : declared_port->substr(first);
    require(significant.size() <= 5 && std::stoi(significant) > 0 && std::stoi(significant) <= 65535,
            "ClickHouse port is outside the valid range");
    port = std::stoi(significant);
  }
  Target out;
  out.host = host_of(*name);
  std::optional<std::string> raw_username;
  std::optional<std::string> raw_password;
  if (at != std::string::npos) {
    const std::string userinfo = parsed.netloc.substr(0, at);
    const std::size_t separator = userinfo.find(':');
    raw_username = userinfo.substr(0, separator);
    if (separator != std::string::npos) raw_password = userinfo.substr(separator + 1);
  }
  out.username = raw_username ? decode_field(*raw_username) : std::string("default");
  out.password = raw_password ? decode_field(*raw_password) : std::string();
  require(!out.username.empty() && out.username.find(':') == std::string::npos,
          "ClickHouse username must be nonempty and contain no colon");
  const std::string& path = parsed.path;
  require(!path.empty() && path[0] == '/' && std::count(path.begin(), path.end(), '/') == 1 &&
              path.find('\\') == std::string::npos,
          "ClickHouse URI needs one explicit database path segment");
  out.database = decode_field(std::string_view(path).substr(1));
  require(!out.database.empty() && out.database.find('/') == std::string::npos &&
              out.database != "." && out.database != "..",
          "ClickHouse database must be a nonempty single segment");
  std::map<std::string, std::string> options;
  for (const auto& [key, value] : parse_qsl(parsed.query)) {
    no_controls(decoded(key));
    no_controls(decoded(value));
    require((key == "secure" || key == "verify" || key == "ca_cert" || key == "connect_timeout" ||
             key == "send_receive_timeout") &&
                !options.contains(key),
            "ClickHouse query parameters must be known and appear once");
    options[key] = value;
  }
  const auto option = [&](const std::string& key) -> std::optional<std::string> {
    const auto found = options.find(key);
    if (found == options.end()) return std::nullopt;
    return found->second;
  };
  const std::optional<std::string> requested = option("secure");
  require(!requested || *requested == "true" || *requested == "false",
          "ClickHouse secure must be literal true or false");
  bool secure = scheme == "https" || scheme == "clickhouses";
  if (scheme == "clickhouse") {
    require(requested.has_value() || !port || (*port != 443 && *port != 8443),
            "Select TLS or explicit plain transport for an ambiguous ClickHouse port");
    secure = requested == "true";
  } else if (requested) {
    require((*requested == "true") == secure, "ClickHouse scheme and secure setting disagree");
  }
  require(!options.contains("verify") || options.at("verify") == "true",
          "ClickHouse verification must remain enabled; use literal true or omit verify");
  require(secure || (!options.contains("verify") && !options.contains("ca_cert")),
          "ClickHouse TLS options cannot apply to plain HTTP");
  const std::optional<std::string> ca_cert = option("ca_cert");
  require(!ca_cert || (!ca_cert->empty() && (*ca_cert)[0] == '/'),
          "ClickHouse CA must be an absolute local file path");
  out.port = port ? *port : scheme == "http" ? 80 : scheme == "https" ? 443 : secure ? 8443 : 8123;
  out.secure = secure;
  out.ca_cert = ca_cert;
  out.connect_timeout = seconds(option("connect_timeout"), kConnectTimeoutSeconds);
  out.send_receive_timeout = seconds(option("send_receive_timeout"), kHandshakeTimeoutSeconds);
  return out;
}

}  // namespace

std::optional<std::string> ipv6_text(std::string_view text) {
  const auto words = ipv6(text);
  if (!words) return std::nullopt;
  return ipv6_format(*words);
}

Target parse_dsn(std::string_view dsn) {
  try {
    return parse(dsn);
  } catch (const Malformed&) {
    throw EngineError(kMalformed);
  }
}

}  // namespace sde::detail::clickhouse

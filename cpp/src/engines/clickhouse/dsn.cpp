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
#include "unicode_internal.hpp"

namespace sde::detail::clickhouse {

namespace {

constexpr double kMinTimeout = 0.001;
constexpr double kMaxTimeout = 2147483.647;

/// What the reference reports for anything its own rules do not name - an encoding, an authority
/// or a setting Python's parsers refuse with their own error.
constexpr const char* kMalformed = "Invalid ClickHouse URI encoding, authority or connection setting";

/// A refusal of Python's parsers rather than of a rule: reported as `kMalformed`.
struct Malformed {};

void require(bool condition, const char* message) {
  if (!condition) throw EngineError(message);
}

bool hex_digit(char c) noexcept {
  return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
}

int hex_value(char c) noexcept {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return c - 'A' + 10;
}

bool ascii(std::string_view text) noexcept {
  for (const char c : text) {
    if (static_cast<unsigned char>(c) > 0x7F) return false;
  }
  return true;
}

std::string lower(std::string_view text) {
  std::string out(text);
  for (char& c : out) {
    if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
  }
  return out;
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

/// `unquote_to_bytes`: each `%XX` its byte, everything else as it is.
std::string unquote_to_bytes(std::string_view text) {
  std::string out;
  for (std::size_t i = 0; i < text.size(); ++i) {
    if (text[i] == '%' && i + 2 < text.size() && hex_digit(text[i + 1]) && hex_digit(text[i + 2])) {
      out += static_cast<char>(hex_value(text[i + 1]) * 16 + hex_value(text[i + 2]));
      i += 2;
    } else {
      out += text[i];
    }
  }
  return out;
}

/// `_decode`: the bytes, as strict UTF-8, without control characters.
std::string decode_field(std::string_view text) {
  const std::string bytes = unquote_to_bytes(text);
  no_controls(decoded(bytes));
  return bytes;
}

/// Python's `unquote(errors="strict")`, which decodes each run of ASCII characters - escapes
/// included - on its own, so an escape cannot borrow the bytes of a character written raw.
std::string unquote(std::string_view text) {
  std::string out;
  std::size_t i = 0;
  while (i < text.size()) {
    std::size_t j = i;
    const bool run_ascii = static_cast<unsigned char>(text[i]) <= 0x7F;
    while (j < text.size() && (static_cast<unsigned char>(text[j]) <= 0x7F) == run_ascii) ++j;
    const std::string_view run = text.substr(i, j - i);
    if (run_ascii) {
      const std::string bytes = unquote_to_bytes(run);
      (void)decoded(bytes);
      out += bytes;
    } else {
      out += run;
    }
    i = j;
  }
  return out;
}

// --- Python's ipaddress -------------------------------------------------------------------------

/// `IPv4Address`: four decimal octets, each 0 to 255, without a leading zero.
std::optional<std::uint32_t> ipv4(std::string_view text) {
  std::uint32_t value = 0;
  int octets = 0;
  std::size_t start = 0;
  while (true) {
    const std::size_t dot = text.find('.', start);
    const std::string_view octet =
        text.substr(start, dot == std::string_view::npos ? std::string_view::npos : dot - start);
    if (octet.empty() || octet.size() > 3) return std::nullopt;
    for (const char c : octet) {
      if (c < '0' || c > '9') return std::nullopt;
    }
    if (octet.size() > 1 && octet[0] == '0') return std::nullopt;
    int number = 0;
    for (const char c : octet) number = number * 10 + (c - '0');
    if (number > 255) return std::nullopt;
    value = (value << 8U) | static_cast<std::uint32_t>(number);
    ++octets;
    if (dot == std::string_view::npos) break;
    start = dot + 1;
  }
  if (octets != 4) return std::nullopt;
  return value;
}

/// `IPv6Address` without a scope: Python's own algorithm, which is what decides an edge.
std::optional<std::array<std::uint16_t, 8>> ipv6(std::string_view text) {
  std::vector<std::string> parts;
  std::size_t start = 0;
  while (true) {
    const std::size_t colon = text.find(':', start);
    parts.emplace_back(
        text.substr(start, colon == std::string_view::npos ? std::string_view::npos : colon - start));
    if (colon == std::string_view::npos) break;
    start = colon + 1;
  }
  if (parts.size() < 3) return std::nullopt;
  if (parts.back().find('.') != std::string::npos) {
    const std::optional<std::uint32_t> tail = ipv4(parts.back());
    if (!tail) return std::nullopt;
    parts.pop_back();
    char high[8];
    char low[8];
    std::snprintf(high, sizeof high, "%x", static_cast<unsigned>((*tail >> 16U) & 0xFFFFU));
    std::snprintf(low, sizeof low, "%x", static_cast<unsigned>(*tail & 0xFFFFU));
    parts.emplace_back(high);
    parts.emplace_back(low);
  }
  if (parts.size() > 9) return std::nullopt;
  std::optional<std::size_t> skip;
  for (std::size_t i = 1; i + 1 < parts.size(); ++i) {
    if (parts[i].empty()) {
      if (skip) return std::nullopt;
      skip = i;
    }
  }
  std::size_t high_count = 0;
  std::size_t low_count = 0;
  std::size_t skipped = 0;
  if (skip) {
    high_count = *skip;
    low_count = parts.size() - *skip - 1;
    if (parts.front().empty()) {
      if (high_count == 0) return std::nullopt;
      --high_count;
      if (high_count != 0) return std::nullopt;
    }
    if (parts.back().empty()) {
      if (low_count == 0) return std::nullopt;
      --low_count;
      if (low_count != 0) return std::nullopt;
    }
    if (high_count + low_count >= 8) return std::nullopt;
    skipped = 8 - (high_count + low_count);
  } else {
    if (parts.size() != 8) return std::nullopt;
    if (parts.front().empty() || parts.back().empty()) return std::nullopt;
    high_count = parts.size();
  }
  const auto hextet = [](const std::string& part) -> std::optional<std::uint16_t> {
    if (part.empty() || part.size() > 4) return std::nullopt;
    unsigned value = 0;
    for (const char c : part) {
      if (!hex_digit(c)) return std::nullopt;
      value = value * 16 + static_cast<unsigned>(hex_value(c));
    }
    return static_cast<std::uint16_t>(value);
  };
  std::array<std::uint16_t, 8> words{};
  std::size_t at = 0;
  for (std::size_t i = 0; i < high_count; ++i) {
    const auto word = hextet(parts[i]);
    if (!word) return std::nullopt;
    words[at++] = *word;
  }
  at += skipped;
  for (std::size_t i = parts.size() - low_count; i < parts.size(); ++i) {
    const auto word = hextet(parts[i]);
    if (!word) return std::nullopt;
    words[at++] = *word;
  }
  return words;
}

std::string ipv6_format(const std::array<std::uint16_t, 8>& words) {
  std::vector<std::string> hextets;
  for (const std::uint16_t word : words) {
    char text[8];
    std::snprintf(text, sizeof text, "%x", static_cast<unsigned>(word));
    hextets.emplace_back(text);
  }
  int best_start = -1;
  int best_length = 0;
  int current_start = -1;
  int current_length = 0;
  for (int index = 0; index < 8; ++index) {
    if (hextets[static_cast<std::size_t>(index)] == "0") {
      ++current_length;
      if (current_start == -1) current_start = index;
      if (current_length > best_length) {
        best_length = current_length;
        best_start = current_start;
      }
    } else {
      current_length = 0;
      current_start = -1;
    }
  }
  if (best_length > 1) {
    const auto begin = hextets.begin() + best_start;
    const bool at_end = best_start + best_length == 8;
    hextets.erase(begin, begin + best_length);
    hextets.insert(hextets.begin() + best_start, "");
    if (at_end) hextets.emplace_back("");
    if (best_start == 0) hextets.insert(hextets.begin(), "");
  }
  std::string out;
  for (std::size_t i = 0; i < hextets.size(); ++i) {
    if (i != 0) out += ':';
    out += hextets[i];
  }
  return out;
}

/// `ipaddress.ip_address`, with a scope as `IPv6Address` takes one.
enum class Address { none, v4, v6 };
Address ip_address(std::string_view text) {
  if (ipv4(text)) return Address::v4;
  const std::size_t percent = text.find('%');
  if (percent != std::string_view::npos) {
    const std::string_view scope = text.substr(percent + 1);
    if (scope.empty() || scope.find('%') != std::string_view::npos) return Address::none;
    text = text.substr(0, percent);
  }
  return ipv6(text) ? Address::v6 : Address::none;
}

// --- Python's urlsplit --------------------------------------------------------------------------

struct Split {
  std::string scheme;
  std::string netloc;
  std::string path;
  std::string query;
};

/// `_check_bracketed_host`: an IPvFuture literal, or an IPv6 address - never an IPv4 one.
void check_bracketed_host(std::string_view host) {
  if (!host.empty() && host[0] == 'v') {
    // \Av[a-fA-F0-9]+\..+\Z
    std::size_t i = 1;
    while (i < host.size() && hex_digit(host[i])) ++i;
    if (i == 1 || i >= host.size() || host[i] != '.' || i + 1 >= host.size()) throw Malformed{};
    return;
  }
  if (ip_address(host) != Address::v6) throw Malformed{};
}

/// `_check_bracketed_netloc`: nothing before the bracket, nothing between it and the port, and the
/// host a bracketed one - mirroring how `hostname` splits the authority.
void check_bracketed_netloc(std::string_view netloc) {
  const std::size_t at = netloc.rfind('@');
  const std::string_view authority = at == std::string_view::npos ? netloc : netloc.substr(at + 1);
  const std::size_t open = authority.find('[');
  std::string_view host;
  if (open != std::string_view::npos) {
    if (open != 0) throw Malformed{};
    const std::string_view bracketed = authority.substr(open + 1);
    const std::size_t close = bracketed.find(']');
    host = bracketed.substr(0, close);
    const std::string_view port =
        close == std::string_view::npos ? std::string_view{} : bracketed.substr(close + 1);
    if (!port.empty() && port[0] != ':') throw Malformed{};
  } else {
    host = authority.substr(0, authority.find(':'));
  }
  check_bracketed_host(host);
}

/// `_checknetloc`: a host whose NFKC form spells a delimiter (`\u2100` is `a/c`) is refused.
void check_netloc(std::string_view netloc) {
  if (netloc.empty() || ascii(netloc)) return;
  std::string kept;
  for (const char c : netloc) {
    if (c != '@' && c != ':' && c != '#' && c != '?') kept += c;
  }
  const std::string normalised = nfkc(kept);
  if (normalised == kept) return;
  if (normalised.find_first_of("/?#@:") != std::string::npos) throw Malformed{};
}

bool scheme_character(char c) noexcept {
  return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '+' ||
         c == '-' || c == '.';
}

Split urlsplit(std::string_view url) {
  Split out;
  const std::size_t colon = url.find(':');
  if (colon != std::string_view::npos && colon > 0 &&
      ((url[0] >= 'a' && url[0] <= 'z') || (url[0] >= 'A' && url[0] <= 'Z'))) {
    bool valid = true;
    for (std::size_t i = 0; i < colon; ++i) valid = valid && scheme_character(url[i]);
    if (valid) {
      out.scheme = lower(url.substr(0, colon));
      url.remove_prefix(colon + 1);
    }
  }
  if (url.substr(0, 2) == "//") {
    std::size_t end = url.size();
    for (const char delimiter : {'/', '?', '#'}) {
      const std::size_t at = url.find(delimiter, 2);
      if (at != std::string_view::npos) end = std::min(end, at);
    }
    out.netloc = std::string(url.substr(2, end - 2));
    url.remove_prefix(end);
    const bool open = out.netloc.find('[') != std::string::npos;
    const bool close = out.netloc.find(']') != std::string::npos;
    if (open != close) throw Malformed{};
    if (open && close) check_bracketed_netloc(out.netloc);
  }
  const std::size_t question = url.find('?');
  if (question != std::string_view::npos) {
    out.query = std::string(url.substr(question + 1));
    url = url.substr(0, question);
  }
  out.path = std::string(url);
  check_netloc(out.netloc);
  return out;
}

/// `SplitResult.hostname`: lowercase, the zone of a scoped address kept as written.
std::optional<std::string> hostname(const std::string& netloc) {
  const std::size_t at = netloc.rfind('@');
  const std::string hostinfo = at == std::string::npos ? netloc : netloc.substr(at + 1);
  std::string name;
  if (const std::size_t open = hostinfo.find('['); open != std::string::npos) {
    const std::string after = hostinfo.substr(open + 1);
    name = after.substr(0, after.find(']'));
  } else {
    name = hostinfo.substr(0, hostinfo.find(':'));
  }
  if (name.empty()) return std::nullopt;
  // Python's full lowercase, not ASCII's: the Kelvin sign is a `k` to it, and so a host.
  const std::size_t percent = name.find('%');
  if (percent == std::string::npos) return to_lower(name);
  return to_lower(name.substr(0, percent)) + name.substr(percent);
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
  std::vector<std::pair<std::string, std::string>> out;
  if (query.empty()) return out;
  std::size_t start = 0;
  while (true) {
    const std::size_t amp = query.find('&', start);
    const std::string_view field =
        query.substr(start, amp == std::string_view::npos ? std::string_view::npos : amp - start);
    const std::size_t equals = field.find('=');
    if (field.empty() || equals == std::string_view::npos) throw Malformed{};
    const auto plus_unquoted = [](std::string_view text) {
      std::string spaced(text);
      for (char& c : spaced) {
        if (c == '+') c = ' ';
      }
      return unquote(spaced);
    };
    out.emplace_back(plus_unquoted(field.substr(0, equals)), plus_unquoted(field.substr(equals + 1)));
    if (amp == std::string_view::npos) break;
    start = amp + 1;
  }
  return out;
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

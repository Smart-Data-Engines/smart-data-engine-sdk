#include "engines/orderbook/dsn.hpp"

#include <algorithm>
#include <charconv>
#include <cstddef>
#include <functional>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <system_error>
#include <utility>
#include <vector>

#include "sde/errors.hpp"
#include "unicode_internal.hpp"

namespace sde::detail::orderbook {

namespace {

constexpr std::string_view kPrefix = "orderbook://";
constexpr std::size_t kNone = std::string_view::npos;

// The refusals, word for word as the contract's rules give them (section 7n).
constexpr const char* kPrintable =
    "an orderbook DSN is printable ASCII: a space, a control character or a character beyond ASCII "
    "is written percent-encoded";
constexpr const char* kScheme = "an orderbook DSN starts with orderbook://";
constexpr const char* kFragment =
    "an orderbook DSN has no fragment: a '#' in a secret is written %23";
constexpr const char* kCredentials =
    "an orderbook DSN's credentials are identity:secret, and neither is empty";
constexpr const char* kIdentity =
    "an orderbook DSN's identity is 1 to 64 characters of [A-Za-z0-9_.:-], as the server's "
    "--auth-secret-file takes them";
constexpr const char* kSecret = "an orderbook DSN's secret is percent-encoded UTF-8";
constexpr const char* kHostAndPort =
    "an orderbook DSN names a host and a port: orderbook://host:port";
constexpr const char* kHost =
    "an orderbook DSN's host is a name of letters, digits, '.', '-' and '_', or an IPv6 address in "
    "brackets";
constexpr const char* kPort = "an orderbook DSN's port is a number from 1 to 65535";
constexpr const char* kPath = "an orderbook DSN has no path; local mode takes a data directory";
constexpr const char* kCa =
    "the orderbook DSN parameter 'ca' names a file: it is not empty and holds no NUL";
constexpr const char* kTimeout =
    "the orderbook DSN parameter 'timeout' is seconds in decimal digits, from 0.001 to 2147483.647";
constexpr const char* kCaWithoutTls =
    "the orderbook DSN's ca needs tls=on: without TLS it verifies nothing";
constexpr const char* kVerifyWithoutTls =
    "the orderbook DSN's verify=off needs tls=on: without TLS there is no certificate to decline "
    "to check";
constexpr const char* kCaWithoutVerify =
    "the orderbook DSN's ca with verify=off is a trust anchor nothing consults";

// Character classes of ASCII alone, never of a locale: <cctype> asks the current C locale, which
// may answer otherwise.
bool digit(char c) { return c >= '0' && c <= '9'; }
bool letter(char c) { return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z'); }
bool hex_digit(char c) { return digit(c) || (c >= 'A' && c <= 'F') || (c >= 'a' && c <= 'f'); }
bool name_character(char c) { return letter(c) || digit(c) || c == '.' || c == '-' || c == '_'; }
bool identity_character(char c) {
  return letter(c) || digit(c) || c == '_' || c == '.' || c == ':' || c == '-';
}

int hex_value(char c) {
  if (digit(c)) return c - '0';
  return (c >= 'a' ? c - 'a' : c - 'A') + 10;
}

bool all_digits(std::string_view text) {
  return !text.empty() && std::all_of(text.begin(), text.end(), digit);
}

std::string lower(std::string_view text) {
  std::string out(text);
  for (char& c : out) {
    if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
  }
  return out;
}

/// The identities the server's `--auth-secret-file` takes (the engine's `valid_identity`). One
/// outside them can never authenticate, and it is sent on the wire, where a space would end it.
bool server_identity(std::string_view identity) {
  return !identity.empty() && identity.size() <= 64 &&
         std::all_of(identity.begin(), identity.end(), identity_character);
}

/// Percent-decoded UTF-8, or nothing: a `%` without two hexadecimal digits, or bytes that are not
/// UTF-8 (strictly: an overlong form, an encoded surrogate and a truncated sequence are refused).
/// In a form - a parameter - a `+` is a space; a `%2B` is still a `+`.
std::optional<std::string> decoded(std::string_view written, bool form) {
  std::string out;
  out.reserve(written.size());
  for (std::size_t index = 0; index < written.size();) {
    const char c = written[index];
    if (c != '%') {
      out += form && c == '+' ? ' ' : c;
      ++index;
      continue;
    }
    if (index + 2 >= written.size() || !hex_digit(written[index + 1]) ||
        !hex_digit(written[index + 2])) {
      return std::nullopt;
    }
    out += static_cast<char>(hex_value(written[index + 1]) * 16 + hex_value(written[index + 2]));
    index += 3;
  }
  if (!decode_utf8(out)) return std::nullopt;
  return out;
}

/// The pieces of `text` between `separator`s, empty ones too.
std::vector<std::string_view> split(std::string_view text, char separator) {
  std::vector<std::string_view> out;
  std::size_t begin = 0;
  while (true) {
    const std::size_t found = text.find(separator, begin);
    out.push_back(text.substr(begin, found == kNone ? kNone : found - begin));
    if (found == kNone) return out;
    begin = found + 1;
  }
}

bool ipv4(std::string_view text) {
  const std::vector<std::string_view> octets = split(text, '.');
  return octets.size() == 4 && std::all_of(octets.begin(), octets.end(), [](std::string_view octet) {
           // 0, or one to three digits without a leading zero, at most 255.
           if (!all_digits(octet) || octet.size() > 3 || (octet.size() > 1 && octet.front() == '0')) {
             return false;
           }
           int value = 0;
           for (const char c : octet) value = value * 10 + (c - '0');
           return value <= 255;
         });
}

/// RFC 3986's `IPv6address`: groups of one to four hexadecimal digits, at most one `::` standing
/// for at least one group, and an IPv4 address allowed as the last 32 bits.
bool ipv6(std::string_view text) {
  const std::size_t at = text.find("::");
  const bool doubled = at != kNone;
  const std::string_view head = doubled ? text.substr(0, at) : text;
  const std::string_view tail = doubled ? text.substr(at + 2) : std::string_view{};
  if (doubled && tail.find("::") != kNone) return false;
  std::vector<std::string_view> groups =
      head.empty() ? std::vector<std::string_view>{} : split(head, ':');
  const std::vector<std::string_view> right =
      doubled && !tail.empty() ? split(tail, ':') : std::vector<std::string_view>{};
  groups.insert(groups.end(), right.begin(), right.end());
  if (groups.empty() && !doubled) return false;
  int count = 0;
  for (std::size_t index = 0; index < groups.size(); ++index) {
    const std::string_view group = groups[index];
    // Only the address's last group may be an IPv4 address: not one before a closing "::".
    const bool last = index + 1 == groups.size() && (!doubled || !right.empty());
    if (last && group.find('.') != kNone) {
      if (!ipv4(group)) return false;
      count += 2;
    } else if (!group.empty() && group.size() <= 4 &&
               std::all_of(group.begin(), group.end(), hex_digit)) {
      count += 1;
    } else {
      return false;
    }
  }
  return doubled ? count <= 7 : count == 8;
}

int port_of(std::string_view written) {
  if (written.empty()) throw EngineError(kHostAndPort);
  if (!all_digits(written)) throw EngineError(kPort);
  // Without the zeros that lead, five digits at most: no port has more, and more would overflow.
  const std::size_t first = written.find_first_not_of('0');
  const std::string_view digits = first == kNone ? std::string_view{} : written.substr(first);
  int port = 0;
  if (digits.size() <= 5) {
    for (const char c : digits) port = port * 10 + (c - '0');
  }
  if (port < 1 || port > 65535) throw EngineError(kPort);
  return port;
}

std::optional<bool> switch_of(std::string_view value) {
  if (value == "on") return true;
  if (value == "off") return false;
  return std::nullopt;
}

bool known_parameter(std::string_view name) {
  return name == "ca" || name == "timeout" || name == "tls" || name == "verify";
}

/// A timeout's seconds in decimal digits, or nothing: its nearest double, within the range.
std::optional<double> seconds_of(std::string_view value) {
  const std::size_t point = value.find('.');
  if (!all_digits(value.substr(0, point))) return std::nullopt;
  if (point != kNone && !all_digits(value.substr(point + 1))) return std::nullopt;
  // from_chars rounds to the nearest double at any length, and says so past the double range or
  // below it, which the range refuses either way.
  double seconds = 0;
  const auto [end, error] = std::from_chars(value.data(), value.data() + value.size(), seconds);
  if (error != std::errc() || end != value.data() + value.size()) return std::nullopt;
  if (!(seconds >= kShortestTimeoutSeconds && seconds <= kLongestTimeoutSeconds)) {
    return std::nullopt;
  }
  return seconds;
}

}  // namespace

void check_target(const Target& target) {
  if (target.host.empty()) {
    throw EngineError(
        "give either a data directory, for in-process access through the shared library, or a "
        "host and port, for a running ob_tcp_server. Not both and not neither: the two are "
        "different deployments with different durability, and defaulting to one of them would "
        "pick a durability guarantee on the client's behalf.");
  }
  if (target.port < 1 || target.port > 65535) {
    throw EngineError(
        "a host needs a port from 1 to 65535; this engine has no default port worth guessing");
  }
  if (target.auth && (!server_identity(target.auth->first) || target.auth->second.empty())) {
    throw EngineError(
        "auth is (identity, secret): an identity of 1 to 64 characters of [A-Za-z0-9_.:-], as the "
        "server's --auth-secret-file takes them, and a non-empty secret");
  }
  if (target.ca && !target.tls) {
    throw EngineError(
        "tls_ca_file without tls=True verifies nothing: the connection would be plain text");
  }
  if (!target.verify && !target.tls) {
    throw EngineError(
        "tls_verify=False without tls=True: there is no certificate to decline to check");
  }
  if (target.ca && !target.verify) {
    throw EngineError("tls_ca_file with tls_verify=False is a trust anchor nothing consults");
  }
  if (!(target.timeout >= kShortestTimeoutSeconds && target.timeout <= kLongestTimeoutSeconds)) {
    throw EngineError("timeout is a number of seconds from 0.001 to 2147483.647");
  }
}

Target parse_dsn(std::string_view dsn) {
  // Rule 1, by byte: a character beyond ASCII is bytes above 0x7E.
  for (const char c : dsn) {
    const auto byte = static_cast<unsigned char>(c);
    if (byte < 0x21 || byte > 0x7E) throw EngineError(kPrintable);
  }
  // Rules 2 and 3.
  if (dsn.size() < kPrefix.size() || lower(dsn.substr(0, kPrefix.size())) != kPrefix) {
    throw EngineError(kScheme);
  }
  std::string_view rest = dsn.substr(kPrefix.size());
  if (rest.find('#') != kNone) throw EngineError(kFragment);
  // Rule 4.
  const std::size_t end = std::min(rest.find('/'), rest.find('?'));
  const std::string_view authority = rest.substr(0, end);
  rest = end == kNone ? std::string_view{} : rest.substr(end);
  const std::size_t mark = rest.find('?');
  const std::string_view path = rest.substr(0, mark);
  const std::string_view query = mark == kNone ? std::string_view{} : rest.substr(mark + 1);
  Target out;
  // Rule 5: what comes before the last @ is identity:secret, split at the first :.
  std::string_view hostport = authority;
  if (const std::size_t at = authority.rfind('@'); at != kNone) {
    const std::string_view userinfo = authority.substr(0, at);
    hostport = authority.substr(at + 1);
    const std::size_t colon = userinfo.find(':');
    const std::string_view written_identity = userinfo.substr(0, colon);
    const std::string_view written_secret =
        colon == kNone ? std::string_view{} : userinfo.substr(colon + 1);
    if (colon == kNone || written_identity.empty() || written_secret.empty()) {
      throw EngineError(kCredentials);
    }
    std::optional<std::string> identity = decoded(written_identity, false);
    if (!identity || !server_identity(*identity)) throw EngineError(kIdentity);
    std::optional<std::string> secret = decoded(written_secret, false);
    if (!secret) throw EngineError(kSecret);
    out.auth = std::make_pair(std::move(*identity), std::move(*secret));
  }
  // Rules 6 and 7.
  std::string_view written_port;
  if (!hostport.empty() && hostport.front() == '[') {
    const std::size_t close = hostport.find(']');
    if (close == kNone || !ipv6(hostport.substr(1, close - 1))) throw EngineError(kHost);
    const std::string_view after = hostport.substr(close + 1);
    if (after.empty()) throw EngineError(kHostAndPort);
    if (after.front() != ':') throw EngineError(kHost);
    out.host = lower(hostport.substr(1, close - 1));
    written_port = after.substr(1);
  } else {
    const std::size_t colon = hostport.find(':');
    const std::string_view name = hostport.substr(0, colon);
    if (name.empty()) throw EngineError(kHostAndPort);
    if (!std::all_of(name.begin(), name.end(), name_character)) throw EngineError(kHost);
    out.host = lower(name);
    written_port = colon == kNone ? std::string_view{} : hostport.substr(colon + 1);
  }
  out.port = port_of(written_port);
  // Rule 8.
  if (!path.empty() && path != "/") throw EngineError(kPath);
  // Rule 9, piece by piece, left to right.
  std::map<std::string, std::string, std::less<>> options;
  for (const std::string_view piece : split(query, '&')) {
    if (piece.empty()) continue;
    const std::size_t equals = piece.find('=');
    const std::string written_name(piece.substr(0, equals));
    const std::string_view written_value =
        equals == kNone ? std::string_view{} : piece.substr(equals + 1);
    const std::optional<std::string> name = decoded(written_name, true);
    if (!name) {
      throw EngineError("the orderbook DSN parameter '" + written_name +
                        "' is not percent-encoded UTF-8");
    }
    if (!known_parameter(*name)) {
      throw EngineError("the orderbook DSN parameter '" + written_name +
                        "' is unknown; it takes ca, timeout, tls and verify");
    }
    if (options.contains(*name)) throw EngineError("the orderbook DSN gives '" + *name + "' twice");
    std::optional<std::string> value = decoded(written_value, true);
    if (!value) {
      throw EngineError("the orderbook DSN parameter '" + *name + "' is not percent-encoded UTF-8");
    }
    if ((*name == "tls" || *name == "verify") && !switch_of(*value)) {
      throw EngineError("the orderbook DSN parameter '" + *name + "' is on or off");
    }
    if (*name == "ca" && (value->empty() || value->find('\0') != std::string::npos)) {
      throw EngineError(kCa);
    }
    if (*name == "timeout" && !seconds_of(*value)) throw EngineError(kTimeout);
    options.emplace(*name, std::move(*value));
  }
  // Rule 10.
  const auto option = [&](std::string_view name) -> const std::string* {
    const auto found = options.find(name);
    return found == options.end() ? nullptr : &found->second;
  };
  out.tls = option("tls") != nullptr && *switch_of(*option("tls"));
  out.verify = option("verify") == nullptr || *switch_of(*option("verify"));
  if (const std::string* ca = option("ca")) out.ca = *ca;
  if (out.ca && !out.tls) throw EngineError(kCaWithoutTls);
  if (!out.verify && !out.tls) throw EngineError(kVerifyWithoutTls);
  if (out.ca && !out.verify) throw EngineError(kCaWithoutVerify);
  if (const std::string* timeout = option("timeout")) out.timeout = *seconds_of(*timeout);
  // Never refuses here - every rule above is stricter - but it is the rule a target is held to.
  check_target(out);
  return out;
}

}  // namespace sde::detail::orderbook

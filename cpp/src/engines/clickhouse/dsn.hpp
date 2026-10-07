#pragma once

/// The portable ClickHouse connection URI, parsed once and with nothing opened: the reference's
/// `parse_dsn` (`sde/engines/_clickhouse_connection.py`), rule for rule and in its order, with its
/// messages - each names the rule broken, never a value, so no credential reaches an error. It is
/// a port of a behaviour as much as of a format: where the reference leans on Python's `urlsplit`,
/// `parse_qsl` and `ipaddress`, this does what they do on every input the rules let through.

#include <optional>
#include <string>
#include <string_view>

namespace sde::detail::clickhouse {

inline constexpr double kConnectTimeoutSeconds = 10;
inline constexpr double kHandshakeTimeoutSeconds = 15;

/// Where to connect, and how: the reference's `ConnectionParameters`.
struct Target {
  std::string host;  ///< a lowercase DNS name, or an IP address as Python's `ipaddress` writes it
  int port = 0;
  std::string username;
  std::string password;
  std::string database;
  bool secure = false;
  std::optional<std::string> ca_cert;
  double connect_timeout = kConnectTimeoutSeconds;
  double send_receive_timeout = kHandshakeTimeoutSeconds;
  /// Whether the URI chose `send_receive_timeout`: only then does it bound every exchange, rather
  /// than the handshake alone.
  bool receive_timeout_supplied = false;

  [[nodiscard]] std::string_view interface() const noexcept { return secure ? "https" : "http"; }
};

/// Parses `clickhouse`, `clickhouses`, `http` and `https` URIs with one database segment and the
/// query options `secure`, `verify`, `ca_cert`, `connect_timeout` and `send_receive_timeout`.
/// Refuses (`EngineError`) anything else with the reference's message for the rule it breaks.
[[nodiscard]] Target parse_dsn(std::string_view dsn);

/// An IPv6 address as Python's `ipaddress` writes it: lowercase hextets without leading zeros, the
/// first longest run of two or more zero hextets as `::`, and never a dotted IPv4 tail. Empty for
/// text that is not an IPv6 address.
[[nodiscard]] std::optional<std::string> ipv6_text(std::string_view text);

}  // namespace sde::detail::clickhouse

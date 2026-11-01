#pragma once

/// The orderbook engine's connection, `orderbook://[identity:secret@]host:port[/][?name=value...]`,
/// read by format contract 7n's grammar - by no URL parser, as every library reads it - rule by
/// rule in the contract's order, each refusal in the contract's words. A refusal names the part
/// that is wrong and never repeats the DSN, which carries the secret. Nothing is opened here.

#include <optional>
#include <string>
#include <string_view>
#include <utility>

namespace sde::detail::orderbook {

inline constexpr double kDefaultTimeoutSeconds = 10;
/// A millisecond: a Node.js socket waits no less, so no library promises less.
inline constexpr double kShortestTimeoutSeconds = 0.001;
/// 2^31 - 1 milliseconds: the longest a Node.js timer waits as asked, so no library promises more.
inline constexpr double kLongestTimeoutSeconds = 2147483.647;

/// Where to connect, and how.
struct Target {
  std::string host;  ///< a name in lower case, or an IPv6 address in lower case without brackets
  int port = 0;
  std::optional<std::pair<std::string, std::string>> auth;  ///< identity and secret
  bool tls = false;
  std::optional<std::string> ca;  ///< a CA file, read when the connection opens
  bool verify = true;
  double timeout = kDefaultTimeoutSeconds;  ///< seconds for the connection and for each answer
};

/// Reads the DSN by contract 7n and refuses (`EngineError`) it at the first rule it breaks.
[[nodiscard]] Target parse_dsn(std::string_view dsn);

/// The rules a connection given field by field is held to (contract 7n), in the reference
/// constructor's words: a host, a port from 1 to 65535, an identity of the server's characters and
/// a secret, no setting that would do nothing, and a timeout every library's socket waits as asked.
/// A connection checks its target with this before it opens a socket.
void check_target(const Target& target);

}  // namespace sde::detail::orderbook

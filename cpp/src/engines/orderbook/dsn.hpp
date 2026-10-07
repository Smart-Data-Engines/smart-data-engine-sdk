#pragma once

/// The orderbook engine's connection: `orderbook://[identity[:secret]@]host:port[?options]`, read as
/// the reference reads it (`OrderbookEngine.from_dsn` and its constructor), rule for rule and with
/// its messages - each names the part that is wrong and none repeats the DSN, which carries the
/// secret. Nothing is opened here.

#include <optional>
#include <string>
#include <string_view>
#include <utility>

namespace sde::detail::orderbook {

inline constexpr double kDefaultTimeoutSeconds = 10;

/// Where to connect, and how.
struct Target {
  std::string host;  ///< as `SplitResult.hostname` gives it: lowercase, an IPv6 address unbracketed
  int port = 0;
  std::optional<std::pair<std::string, std::string>> auth;  ///< identity and secret
  bool tls = false;
  std::optional<std::string> ca;  ///< a CA file, read when the connection opens
  bool verify = true;
  double timeout = kDefaultTimeoutSeconds;  ///< seconds for the connection and for each answer
};

/// Parses the DSN, the options `tls`, `ca`, `verify` and `timeout` among its query parameters, and
/// checks the result as the reference's constructor does. Refuses (`EngineError`) whatever breaks a
/// rule, in the reference's words.
[[nodiscard]] Target parse_dsn(std::string_view dsn);

/// The reference constructor's checks, for a target given field by field rather than as a DSN.
void check_target(const Target& target);

}  // namespace sde::detail::orderbook

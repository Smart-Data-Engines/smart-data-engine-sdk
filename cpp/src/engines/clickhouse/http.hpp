#pragma once

/// One ClickHouse server over its HTTP interface, through libcurl, and nothing else: an exchange is
/// one POST on a connection of its own. Never reused, so never resent: libcurl replays a request
/// whose reused connection turns out to be dead, and a replayed INSERT that the server had already
/// accepted is the duplicate the reference measured and refused. A failure after the request may
/// have reached the server is reported as such, never retried, and never redirected.

#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <string_view>

#include "engines/clickhouse/dsn.hpp"

namespace sde::detail::clickhouse {

/// The server refused the statement: its exception code, and the message as the reference's driver
/// writes it - "Received ClickHouse exception, code: N, server response: ... (for url ...)".
class ServerError : public std::exception {
 public:
  ServerError(std::optional<int> code, std::string message)
      : code_(code), message_(std::move(message)) {}
  [[nodiscard]] const char* what() const noexcept override { return message_.c_str(); }
  [[nodiscard]] std::optional<int> code() const noexcept { return code_; }

 private:
  std::optional<int> code_;
  std::string message_;
};

/// No complete answer came back: the connection was refused, timed out, or was cut. The request may
/// or may not have reached the server, so its outcome is unknown and it was not replayed.
class TransportFailure : public std::exception {
 public:
  explicit TransportFailure(std::string detail) : detail_(std::move(detail)) {}
  [[nodiscard]] const char* what() const noexcept override { return detail_.c_str(); }

 private:
  std::string detail_;
};

/// What a failed exchange says to the caller, in the reference's words.
inline constexpr std::string_view kNotReplayed =
    "ClickHouse transport failed; the operation was not replayed and its outcome may be unknown";

/// The CA file of a URI, read once and qualified before any connection: a regular file of at most
/// 1 MiB holding at least one valid PEM certificate. `EngineError` otherwise, in the reference's
/// words. The bytes are what every later connection trusts, so a file changed later changes nothing.
[[nodiscard]] std::string read_ca(const std::string& path);

class Http {
 public:
  /// Reads the CA of a secure target, once.
  explicit Http(Target target);

  [[nodiscard]] const Target& target() const noexcept { return target_; }
  /// `http://host:port`, an IPv6 host in brackets: what the reference's messages name.
  [[nodiscard]] std::string url() const;

  /// One statement, its answer's body when the server accepted it. `format` appends
  /// `FORMAT <format>` and, for JSON, the settings that keep every number exact; `data` is the body
  /// an INSERT carries; `settings` are more URL parameters, already encoded (`&async_insert=0`).
  /// Every exchange is bounded by the URI's send_receive_timeout of silence - progress headers keep
  /// a running query alive, as the reference's driver asks for them - and `handshake` bounds the
  /// whole exchange by it too. Throws `ServerError` or `TransportFailure`.
  std::string post(std::string_view sql, std::optional<std::string_view> format = std::nullopt,
                   std::string_view data = {}, bool handshake = false,
                   std::string_view settings = {}) const;

 private:
  Target target_;
  std::string ca_;
};

}  // namespace sde::detail::clickhouse

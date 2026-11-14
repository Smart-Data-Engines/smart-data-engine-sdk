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
/// What every statement of this adapter says with it, whatever its login's settings say.
///
/// ClickHouse ends an answer early and raises nothing when a limit is reached under an overflow
/// mode of `break`: measured on 24.8 and 26.9, a login with `max_result_rows` and
/// `result_overflow_mode = 'break'` was answered 48 193 and 20 000 rows of 200 000, and one with a
/// small `max_execution_time` and `timeout_overflow_mode = 'break'` nothing at all, a count
/// included. A profile's `limit`, `offset` and `additional_result_filter` shorten an answer the same
/// way. The library takes an answer without an error for the whole answer, so it says `throw`, and
/// no limit and no filter of the profile's, with every statement. A login that may not say so and
/// whose own value is another is refused by the server, by the setting's name. The names and
/// values of the reference's `WHOLE_ANSWERS`; the braces are an empty map, percent-encoded.
inline constexpr std::string_view kWholeAnswers =
    "&distinct_overflow_mode=throw&group_by_overflow_mode=throw&join_overflow_mode=throw"
    "&read_overflow_mode=throw&result_overflow_mode=throw&set_overflow_mode=throw"
    "&sort_overflow_mode=throw&timeout_overflow_mode=throw&transfer_overflow_mode=throw"
    "&read_overflow_mode_leaf=throw&timeout_overflow_mode_leaf=throw"
    "&limit=0&offset=0&additional_table_filters=%7B%7D&additional_result_filter=";

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
  /// an INSERT carries, and without it the statement is the body - never the URL, which a proxy
  /// may log; `settings` are more URL parameters, already encoded (`&async_insert=0`).
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

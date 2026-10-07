#pragma once

/// The orderbook engine's text protocol over TCP or TLS 1.3, spoken by this library itself: the
/// engine ships a Python client and no C++ one, and requirement 4.4 keeps the engine's library out
/// of a client's link, as the TypeScript library keeps it. The protocol, and the words of each of
/// its failures, are those of the engine's own Python client at the commit
/// `.github/orderbook-engine.txt` pins (`python/orderbook_engine/__init__.py`) - the client the
/// reference talks to the server through, and whose texts it puts into its own messages - measured
/// against a running `ob_tcp_server` before this was written:
///
/// - the server greets with `OK ob_tcp_server v<version>\n\n` once TLS, if any, has finished;
/// - a command is one line, and `MINSERT` carries one more line per level;
/// - an answer is `ERR <message>\n`, `PONG\n`, a role on one line, or `OK` and a body up to an empty
///   line - `OK\n\n` when there is no body;
/// - with client authentication `AUTH` is answered `OK CHALLENGE <nonce>`, and `AUTH <identity>
///   <hmac>` then `OK AUTH <identity>`; a wrong one `ERR auth_failed`, and the server closes;
/// - answers come back in the order the commands went, so commands can be pipelined;
/// - a subscription's `PUSH ` lines may come in front of an answer: nothing here subscribes, and
///   they are skipped anyway, as the client skips them;
/// - `QUIT` is answered by closing.
///
/// **An exchange that does not finish closes the connection** (the engine's #171): the rest of its
/// answer may still be on the way, and the next command would read it as its own. Every later call
/// is refused with the reason, and nothing is retried.
///
/// Three things differ from the engine's client, each for a reason. An address is resolved for
/// either family and tried in turn, where the client opens only IPv4 sockets. Bytes no answer can
/// begin with are refused when they arrive, where the client waits for its timeout. And a greeting
/// must begin with `OK`, where the client takes any. Writes never raise SIGPIPE: a plain socket is
/// written with `MSG_NOSIGNAL`, and TLS runs over memory buffers this code sends itself.

#include <cstdint>
#include <map>
#include <memory>
#include <set>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

#include "engines/orderbook/dsn.hpp"

namespace sde::detail::orderbook {

/// A failure, in the words of the engine's client, and the name of the Python class that client
/// raises - the reference logs it in `sde.write.failed`.
class WireError : public std::runtime_error {
 public:
  WireError(const std::string& text, std::string python_class)
      : std::runtime_error(text), class_(std::move(python_class)) {}
  [[nodiscard]] const std::string& python_class() const noexcept { return class_; }

 private:
  std::string class_;
};

/// One answer, as `_parse_tcp_response` reads it.
struct Answer {
  bool error = false;
  std::string message;  ///< what follows `ERR `, without its line break; or why it is unreadable
  std::vector<std::string> header;
  std::vector<std::vector<std::string>> rows;
};
[[nodiscard]] Answer parse_answer(std::string_view raw);

/// The `key: value` lines of a STATUS answer before its first `[section]`, as
/// `_parse_status_fields` collects them; a value `int()` reads is that integer's digits.
[[nodiscard]] std::map<std::string, std::string> status_fields(std::string_view raw);

/// The names a STATUS answer lists as its `capabilities`, read as `status()` and
/// `server_capabilities()` read them: an `ERR` is `STATUS error: <message>`, and a first data row
/// whose counters `int()` does not read is that `ValueError`.
[[nodiscard]] std::set<std::string> server_capabilities(std::string_view raw);

/// HMAC-SHA256 keyed by the secret over `ob-auth-v1`, `client`, `initiator`, the identity and the
/// nonce, NUL-separated, in lower-case hex: `ob::auth_response()` of the server, byte for byte.
[[nodiscard]] std::string auth_digest(std::string_view identity, std::string_view secret,
                                      std::string_view nonce);

/// One connection to an `ob_tcp_server`, used by one thread, one exchange at a time.
class Connection {
 public:
  /// Builds the TLS context the target asks for (`_build_tls_context`), then connects, completes
  /// TLS, reads the greeting and answers the challenge, every wait bounded by the target's timeout
  /// of silence. Throws `WireError`, whose text never holds the secret.
  explicit Connection(const Target& target);
  ~Connection();
  Connection(const Connection&) = delete;
  Connection& operator=(const Connection&) = delete;

  /// Sends one command and returns its whole answer, decoded as UTF-8 with replacement.
  [[nodiscard]] std::string execute(std::string_view command);
  /// Sends every command in one write, then reads one answer for each, in order.
  [[nodiscard]] std::vector<std::string> execute_pipelined(const std::vector<std::string>& commands);
  /// Says `QUIT` when the connection is still in step, and closes it. Never throws.
  void close() noexcept;
  /// Whether it can still be sent on: neither closed nor left out of step by an exchange.
  [[nodiscard]] bool open() const noexcept { return stream_ != nullptr; }
  /// Throws the `WireError` the next exchange would: why the connection is closed.
  void ensure_open() const;

 private:
  class Stream;

  void read_greeting();
  void authenticate();
  /// One whole answer off the front of what has arrived, reading more until it is there.
  std::string answer();
  /// Runs one exchange; one that does not finish closes the connection and keeps why.
  template <typename Body>
  auto exchange(const std::string& label, const Body& body) -> decltype(body());
  void abandon(const std::string& reason) noexcept;

  Target target_;
  std::string where_;  ///< `host:port`, as the client names the connection
  std::unique_ptr<Stream> stream_;
  std::string buffer_;          ///< bytes received and not yet taken as an answer
  std::string closed_because_;  ///< why an exchange that did not finish closed it; empty otherwise
};

}  // namespace sde::detail::orderbook

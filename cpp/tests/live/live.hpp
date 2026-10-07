#pragma once

/// What the live tests share: the engines they run against, from the environment, and a refusal to
/// pass by not running. Without a DSN a live test is skipped, and says so; in CI (`CI=true`) it
/// fails instead, because a green job that ran nothing is the failure that looks like a pass.

#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <cstdlib>
#include <optional>
#include <random>
#include <string>
#include <thread>
#include <vector>

#include <gtest/gtest.h>

namespace sde::live {

/// The environment's DSN for an engine, or nothing.
inline std::optional<std::string> dsn_from(const char* variable) {
  const char* value = std::getenv(variable);
  if (value == nullptr || *value == '\0') return std::nullopt;
  return std::string(value);
}

inline bool in_ci() {
  const char* value = std::getenv("CI");
  return value != nullptr && std::string(value) == "true";
}

/// A fresh lowercase suffix, so concurrent runs and reruns never share a table.
inline std::string fresh(std::size_t length = 10) {
  static std::mt19937_64 generator{std::random_device{}()};
  constexpr std::string_view kAlphabet = "abcdefghijklmnopqrstuvwxyz0123456789";
  std::uniform_int_distribution<std::size_t> pick(0, kAlphabet.size() - 1);
  std::string out;
  for (std::size_t i = 0; i < length; ++i) out += kAlphabet[pick(generator)];
  return out;
}

/// The same URI DSN pointed at another port of the same host - parsed, not string-replaced, so a
/// DSN on another port than the one this machine uses is still moved.
inline std::string with_port(const std::string& dsn, int port) {
  const std::size_t scheme = dsn.find("://");
  const std::size_t authority = scheme == std::string::npos ? 0 : scheme + 3;
  const std::size_t path = dsn.find_first_of("/?", authority);
  const std::size_t end = path == std::string::npos ? dsn.size() : path;
  const std::size_t at = dsn.rfind('@', end);
  const std::size_t host = at == std::string::npos || at < authority ? authority : at + 1;
  std::string host_part = dsn.substr(host, end - host);
  if (const std::size_t colon = host_part.rfind(':');
      colon != std::string::npos && host_part.find(']') == std::string::npos) {
    host_part = host_part.substr(0, colon);
  }
  return dsn.substr(0, host) + host_part + ":" + std::to_string(port) + dsn.substr(end);
}

/// The DSN with one more query parameter.
inline std::string with_parameter(const std::string& dsn, const std::string& parameter) {
  return dsn + (dsn.find('?') == std::string::npos ? "?" : "&") + parameter;
}

/// A port with nothing listening on it: bound and released, so the number is known to be unused.
inline int free_port() {
  const int probe = ::socket(AF_INET, SOCK_STREAM, 0);
  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  ::bind(probe, reinterpret_cast<sockaddr*>(&address), sizeof address);
  socklen_t length = sizeof address;
  ::getsockname(probe, reinterpret_cast<sockaddr*>(&address), &length);
  ::close(probe);
  return ntohs(address.sin_port);
}

/// A socket that completes the TCP handshake and then says nothing, ever: a firewall that accepts,
/// a load balancer with no healthy backend, a server starting up. Closed with the object.
class SilentServer {
 public:
  SilentServer() {
    listener_ = ::socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    ::bind(listener_, reinterpret_cast<sockaddr*>(&address), sizeof address);
    socklen_t length = sizeof address;
    ::getsockname(listener_, reinterpret_cast<sockaddr*>(&address), &length);
    port_ = ntohs(address.sin_port);
    ::listen(listener_, 8);
    thread_ = std::thread([this] {
      while (!stopping_) {
        const int accepted = ::accept(listener_, nullptr, nullptr);
        if (accepted < 0) return;
        accepted_.push_back(accepted);
      }
    });
  }
  ~SilentServer() {
    stopping_ = true;
    ::shutdown(listener_, SHUT_RDWR);
    ::close(listener_);
    thread_.join();
    for (const int socket : accepted_) ::close(socket);
  }
  SilentServer(const SilentServer&) = delete;
  SilentServer& operator=(const SilentServer&) = delete;
  [[nodiscard]] int port() const noexcept { return port_; }

 private:
  int listener_ = -1;
  int port_ = 0;
  std::atomic<bool> stopping_{false};
  std::vector<int> accepted_;
  std::thread thread_;
};

}  // namespace sde::live

/// Skips a live test without its engine, and fails it in CI.
#define SDE_REQUIRE_DSN(variable, into)                                                         \
  do {                                                                                          \
    const auto found_ = ::sde::live::dsn_from(variable);                                        \
    if (!found_) {                                                                              \
      if (::sde::live::in_ci()) FAIL() << variable " is not set, and CI runs every live test"; \
      GTEST_SKIP() << "set " variable " to run this against a real engine";                    \
    }                                                                                           \
    (into) = *found_;                                                                           \
  } while (false)

#pragma once

/// Servers the transport tests talk to instead of ClickHouse, or in front of it: one that plays a
/// script of answers, one that never finishes an answer, and a proxy that decides what reaches the
/// real server and what comes back.

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <cstdint>
#include <functional>
#include <mutex>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

namespace sde::live {

/// What a scripted server does with one connection once the request has arrived: send these pieces,
/// each after its pause, and close; send nothing and close (no pieces); or hold the connection open
/// and silent until the server is destroyed.
struct Reply {
  std::vector<std::pair<std::chrono::milliseconds, std::string>> pieces;
  bool hold = false;

  /// The whole answer at once; empty text closes without answering.
  Reply(std::string answer = {}) {  // NOLINT(google-explicit-constructor): a script reads as text
    if (!answer.empty()) pieces.emplace_back(std::chrono::milliseconds(0), std::move(answer));
  }
  Reply(const char* answer) : Reply(std::string(answer)) {}  // NOLINT(google-explicit-constructor)
  [[nodiscard]] static Reply silent() {
    Reply reply;
    reply.hold = true;
    return reply;
  }
  [[nodiscard]] static Reply paced(
      std::vector<std::pair<std::chrono::milliseconds, std::string>> pieces) {
    Reply reply;
    reply.pieces = std::move(pieces);
    return reply;
  }
};

/// A server that answers each connection from a script: one reply per connection, in order. It
/// reads the whole request first - the headers and the body their Content-Length announces - so a
/// failure it plays is a failure after the request arrived, which is the case where the outcome is
/// unknown. A connection past the end of the script is closed unanswered.
class ScriptedServer {
 public:
  explicit ScriptedServer(std::vector<Reply> responses) : responses_(std::move(responses)) {
    listener_ = ::socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    ::bind(listener_, reinterpret_cast<sockaddr*>(&address), sizeof address);
    socklen_t length = sizeof address;
    ::getsockname(listener_, reinterpret_cast<sockaddr*>(&address), &length);
    port_ = ntohs(address.sin_port);
    ::listen(listener_, 8);
    worker_ = std::thread([this] { serve(); });
  }
  ~ScriptedServer() {
    stopping_ = true;
    ::shutdown(listener_, SHUT_RDWR);
    ::close(listener_);
    worker_.join();
  }
  ScriptedServer(const ScriptedServer&) = delete;
  ScriptedServer& operator=(const ScriptedServer&) = delete;

  [[nodiscard]] int port() const noexcept { return port_; }
  /// Each request as it arrived, headers and body.
  [[nodiscard]] std::vector<std::string> requests() const {
    const std::lock_guard<std::mutex> lock(mutex_);
    return requests_;
  }

 private:
  void serve() {
    std::size_t next = 0;
    while (true) {
      const int socket = ::accept(listener_, nullptr, nullptr);
      if (socket < 0) return;
      timeval timeout{2, 0};
      ::setsockopt(socket, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof timeout);
      std::string request;
      char buffer[65536];
      std::size_t end = std::string::npos;
      while ((end = request.find("\r\n\r\n")) == std::string::npos) {
        const ssize_t got = ::recv(socket, buffer, sizeof buffer, 0);
        if (got <= 0) break;
        request.append(buffer, static_cast<std::size_t>(got));
      }
      if (end != std::string::npos) {
        std::size_t body = 0;
        for (const std::string_view name : {"Content-Length: ", "content-length: "}) {
          if (const std::size_t at = request.find(name); at != std::string::npos && at < end) {
            body = std::stoul(request.substr(at + name.size()));
          }
        }
        while (request.size() < end + 4 + body) {
          const ssize_t got = ::recv(socket, buffer, sizeof buffer, 0);
          if (got <= 0) break;
          request.append(buffer, static_cast<std::size_t>(got));
        }
        {
          const std::lock_guard<std::mutex> lock(mutex_);
          requests_.push_back(request);
        }
        const Reply reply = next < responses_.size() ? responses_[next] : Reply();
        ++next;
        for (const auto& [pause, piece] : reply.pieces) {
          std::this_thread::sleep_for(pause);
          std::size_t sent = 0;
          while (sent < piece.size()) {
            const ssize_t wrote =
                ::send(socket, piece.data() + sent, piece.size() - sent, MSG_NOSIGNAL);
            if (wrote <= 0) break;
            sent += static_cast<std::size_t>(wrote);
          }
        }
        while (reply.hold && !stopping_) std::this_thread::sleep_for(std::chrono::milliseconds(20));
      }
      ::close(socket);
    }
  }

  std::vector<Reply> responses_;
  int listener_ = -1;
  int port_ = 0;
  std::atomic<bool> stopping_{false};
  mutable std::mutex mutex_;
  std::vector<std::string> requests_;
  std::thread worker_;
};

/// A server that answers every request with a 200 whose chunked body never ends: one byte every
/// `every`, until the object is destroyed. Never silent, never finished - what a bound on silence
/// alone cannot stop.
class TrickleServer {
 public:
  explicit TrickleServer(std::chrono::milliseconds every) : every_(every) {
    listener_ = ::socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    ::bind(listener_, reinterpret_cast<sockaddr*>(&address), sizeof address);
    socklen_t length = sizeof address;
    ::getsockname(listener_, reinterpret_cast<sockaddr*>(&address), &length);
    port_ = ntohs(address.sin_port);
    ::listen(listener_, 8);
    worker_ = std::thread([this] { serve(); });
  }
  ~TrickleServer() {
    stopping_ = true;
    ::shutdown(listener_, SHUT_RDWR);
    ::close(listener_);
    worker_.join();
  }
  TrickleServer(const TrickleServer&) = delete;
  TrickleServer& operator=(const TrickleServer&) = delete;
  [[nodiscard]] int port() const noexcept { return port_; }

 private:
  void serve() {
    const int socket = ::accept(listener_, nullptr, nullptr);
    if (socket < 0) return;
    char buffer[65536];
    std::string request;
    while (request.find("\r\n\r\n") == std::string::npos) {
      const ssize_t got = ::recv(socket, buffer, sizeof buffer, 0);
      if (got <= 0) break;
      request.append(buffer, static_cast<std::size_t>(got));
    }
    const std::string head = "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n";
    bool open = ::send(socket, head.data(), head.size(), MSG_NOSIGNAL) > 0;
    while (open && !stopping_) {
      std::this_thread::sleep_for(every_);
      open = ::send(socket, "1\r\nx\r\n", 6, MSG_NOSIGNAL) > 0;
    }
    ::close(socket);
  }

  std::chrono::milliseconds every_;
  int listener_ = -1;
  int port_ = 0;
  std::atomic<bool> stopping_{false};
  std::thread worker_;
};

/// A proxy between an adapter and a real server that decides, request by request, what reaches the
/// server and what comes back. Each connection carries one request, read whole and handed to
/// `decide`; then it is refused (closed, never forwarded), or forwarded with `Connection: close`,
/// so the server's answer ends with its connection, and the answer relayed - or, for `lose`, read
/// whole and dropped with the client's connection: a request the server accepted whose answer never
/// arrived. Connections are served one at a time, in order.
class ForwardingProxy {
 public:
  enum class Action { relay, lose, refuse };
  using Decide = std::function<Action(const std::string& request)>;

  ForwardingProxy(std::string upstream_host, int upstream_port, Decide decide)
      : host_(std::move(upstream_host)), upstream_(upstream_port), decide_(std::move(decide)) {
    listener_ = ::socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    ::bind(listener_, reinterpret_cast<sockaddr*>(&address), sizeof address);
    socklen_t length = sizeof address;
    ::getsockname(listener_, reinterpret_cast<sockaddr*>(&address), &length);
    port_ = ntohs(address.sin_port);
    ::listen(listener_, 8);
    worker_ = std::thread([this] { serve(); });
  }
  ~ForwardingProxy() {
    ::shutdown(listener_, SHUT_RDWR);
    ::close(listener_);
    worker_.join();
  }
  ForwardingProxy(const ForwardingProxy&) = delete;
  ForwardingProxy& operator=(const ForwardingProxy&) = delete;

  [[nodiscard]] int port() const noexcept { return port_; }
  /// Each request as it arrived, with what was done with it.
  [[nodiscard]] std::vector<std::pair<std::string, Action>> seen() const {
    const std::lock_guard<std::mutex> lock(mutex_);
    return seen_;
  }

 private:
  static bool send_all(int socket, const std::string& bytes) {
    std::size_t sent = 0;
    while (sent < bytes.size()) {
      const ssize_t wrote = ::send(socket, bytes.data() + sent, bytes.size() - sent, MSG_NOSIGNAL);
      if (wrote <= 0) return false;
      sent += static_cast<std::size_t>(wrote);
    }
    return true;
  }

  /// The whole request: headers, and the body their Content-Length announces.
  static std::string read_request(int socket) {
    std::string request;
    char buffer[65536];
    std::size_t end = std::string::npos;
    while ((end = request.find("\r\n\r\n")) == std::string::npos) {
      const ssize_t got = ::recv(socket, buffer, sizeof buffer, 0);
      if (got <= 0) return request;
      request.append(buffer, static_cast<std::size_t>(got));
    }
    std::size_t body = 0;
    for (const std::string_view name : {"Content-Length: ", "content-length: "}) {
      if (const std::size_t at = request.find(name); at != std::string::npos && at < end) {
        body = std::stoul(request.substr(at + name.size()));
      }
    }
    while (request.size() < end + 4 + body) {
      const ssize_t got = ::recv(socket, buffer, sizeof buffer, 0);
      if (got <= 0) break;
      request.append(buffer, static_cast<std::size_t>(got));
    }
    return request;
  }

  /// The request to the server and its whole answer back, or nothing when it cannot be reached.
  std::string exchange(std::string request) const {
    const std::size_t end = request.find("\r\n\r\n");
    request.insert(end + 2, "Connection: close\r\n");
    const int upstream = ::socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_port = htons(static_cast<std::uint16_t>(upstream_));
    ::inet_pton(AF_INET, host_.c_str(), &address.sin_addr);
    std::string answer;
    if (::connect(upstream, reinterpret_cast<sockaddr*>(&address), sizeof address) == 0 &&
        send_all(upstream, request)) {
      char buffer[65536];
      while (true) {
        const ssize_t got = ::recv(upstream, buffer, sizeof buffer, 0);
        if (got <= 0) break;
        answer.append(buffer, static_cast<std::size_t>(got));
      }
    }
    ::close(upstream);
    return answer;
  }

  void serve() {
    while (true) {
      const int socket = ::accept(listener_, nullptr, nullptr);
      if (socket < 0) return;
      const std::string request = read_request(socket);
      const Action action = request.empty() ? Action::refuse : decide_(request);
      {
        const std::lock_guard<std::mutex> lock(mutex_);
        seen_.emplace_back(request, action);
      }
      if (action != Action::refuse) {
        const std::string answer = exchange(request);
        if (action == Action::relay) (void)send_all(socket, answer);
      }
      ::close(socket);
    }
  }

  std::string host_;
  int upstream_;
  Decide decide_;
  int listener_ = -1;
  int port_ = 0;
  mutable std::mutex mutex_;
  std::vector<std::pair<std::string, Action>> seen_;
  std::thread worker_;
};

/// A complete HTTP/1.1 answer with this status line and body.
inline std::string http_answer(std::string_view status, std::string_view body,
                               std::string_view headers = {}) {
  return "HTTP/1.1 " + std::string(status) + "\r\nContent-Length: " + std::to_string(body.size()) +
         "\r\nConnection: close\r\n" + std::string(headers) + "\r\n" + std::string(body);
}

/// A body in `RowBinaryWithNamesAndTypes` of String columns: their names, then each row's texts.
/// Every text is shorter than 128 bytes, so its length is one byte.
inline std::string texts_body(const std::vector<std::string>& names,
                              const std::vector<std::vector<std::string>>& rows) {
  const auto text = [](std::string_view value) {
    return std::string(1, static_cast<char>(value.size())) + std::string(value);
  };
  std::string body(1, static_cast<char>(names.size()));
  for (const std::string& name : names) body += text(name);
  for (std::size_t i = 0; i < names.size(); ++i) body += text("String");
  for (const std::vector<std::string>& row : rows) {
    for (const std::string& cell : row) body += text(cell);
  }
  return body;
}

/// The same body as a whole 200 answer.
inline std::string texts_answer(const std::vector<std::string>& names,
                                const std::vector<std::vector<std::string>>& rows) {
  return http_answer("200 OK", texts_body(names, rows));
}

/// What `SELECT version()` gets back from a server of `version`.
inline std::string version_answer(const std::string& version) {
  return texts_answer({"version()"}, {{version}});
}

}  // namespace sde::live

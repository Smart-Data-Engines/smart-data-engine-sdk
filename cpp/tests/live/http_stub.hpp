#pragma once

/// A server that answers each connection from a script: one HTTP response per connection, in order,
/// or an empty entry to close the connection without answering. It reads the whole request first -
/// the headers and the body their Content-Length announces - so a failure it plays is a failure
/// after the request arrived, which is the case where the outcome is unknown. A connection past the
/// end of the script is closed unanswered.

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#include <cstdint>
#include <mutex>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

namespace sde::live {

class ScriptedServer {
 public:
  explicit ScriptedServer(std::vector<std::string> responses) : responses_(std::move(responses)) {
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
        const std::string& answer = next < responses_.size() ? responses_[next] : std::string();
        ++next;
        std::size_t sent = 0;
        while (sent < answer.size()) {
          const ssize_t wrote = ::send(socket, answer.data() + sent, answer.size() - sent, MSG_NOSIGNAL);
          if (wrote <= 0) break;
          sent += static_cast<std::size_t>(wrote);
        }
      }
      ::close(socket);
    }
  }

  std::vector<std::string> responses_;
  int listener_ = -1;
  int port_ = 0;
  mutable std::mutex mutex_;
  std::vector<std::string> requests_;
  std::thread worker_;
};

/// A complete HTTP/1.1 answer with this status line and body.
inline std::string http_answer(std::string_view status, std::string_view body,
                               std::string_view headers = {}) {
  return "HTTP/1.1 " + std::string(status) + "\r\nContent-Length: " + std::to_string(body.size()) +
         "\r\nConnection: close\r\n" + std::string(headers) + "\r\n" + std::string(body);
}

/// A 200 answer in `RowBinaryWithNamesAndTypes` of String columns: their names, then each row's
/// texts. Every text is shorter than 128 bytes, so its length is one byte.
inline std::string texts_answer(const std::vector<std::string>& names,
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
  return http_answer("200 OK", body);
}

/// What `SELECT version()` gets back from a server of `version`.
inline std::string version_answer(const std::string& version) {
  return texts_answer({"version()"}, {{version}});
}

}  // namespace sde::live

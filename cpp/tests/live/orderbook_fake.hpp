#pragma once

/// Servers that speak the orderbook engine's text protocol in this process, plain or over TLS: one
/// that runs a script for each connection, and a fake engine that answers as the engine was
/// measured to answer on 2 October 2026 (engine `971dda2`) - rows in arrival order, `LIMIT` on
/// arrival, `OB_ERR_NOT_FOUND` for a book nothing was written to, a sequence number per book, the
/// AUTH challenge. The adapter's decisions and the protocol's edges are tested against them; that
/// they describe the engine is held by the live slice against a real `ob_tcp_server`.
///
/// The fake checks an AUTH digest with OpenSSL's HMAC, not with the library's own function, so a
/// wrong digest in the library cannot be agreed with here.

#include <arpa/inet.h>
#include <netinet/in.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <openssl/err.h>
#include <openssl/evp.h>
#include <openssl/hmac.h>
#include <openssl/rand.h>
#include <openssl/ssl.h>

#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <filesystem>
#include <functional>
#include <map>
#include <mutex>
#include <optional>
#include <regex>
#include <set>
#include <string>
#include <string_view>
#include <thread>
#include <tuple>
#include <utility>
#include <vector>

#include "live/tls.hpp"

namespace sde::live {

/// One accepted connection, as the server sees it.
class Peer {
 public:
  Peer(int fd, SSL* ssl) : fd_(fd), ssl_(ssl) {}

  /// Sends bytes; false once the client has gone.
  bool send(std::string_view bytes) {
    std::size_t sent = 0;
    while (sent < bytes.size()) {
      const auto left = bytes.size() - sent;
      const long count =
          ssl_ != nullptr ? SSL_write(ssl_, bytes.data() + sent, static_cast<int>(left))
                          : static_cast<long>(::send(fd_, bytes.data() + sent, left, MSG_NOSIGNAL));
      if (count <= 0) return false;
      sent += static_cast<std::size_t>(count);
    }
    return true;
  }

  /// The next line without its line break; nothing once the client has closed, or after `wait`.
  std::optional<std::string> line(std::chrono::milliseconds wait = std::chrono::seconds(5)) {
    for (;;) {
      const std::size_t newline = buffer_.find('\n');
      if (newline != std::string::npos) {
        std::string out = buffer_.substr(0, newline);
        buffer_.erase(0, newline + 1);
        return out;
      }
      if (!fill(wait)) return std::nullopt;
    }
  }

  /// Everything that arrives until `wait` passes in silence or the client closes.
  std::string drain(std::chrono::milliseconds wait) {
    while (fill(wait)) {
    }
    return std::exchange(buffer_, {});
  }

  /// Whether the client closed its end within `wait`.
  bool closed_by_client(std::chrono::milliseconds wait) {
    const auto deadline = std::chrono::steady_clock::now() + wait;
    while (std::chrono::steady_clock::now() < deadline) {
      if (!fill(std::chrono::milliseconds(20))) {
        pollfd target{fd_, POLLIN, 0};
        if (::poll(&target, 1, 0) > 0) return true;  // readable and nothing to read: the end
      }
    }
    return false;
  }

  /// Ends the connection from this side.
  void close() noexcept { ::shutdown(fd_, SHUT_RDWR); }
  /// Ends it with a reset instead, when the server closes it after the handler returns.
  void reset_on_close() noexcept {
    const linger abrupt{1, 0};
    (void)::setsockopt(fd_, SOL_SOCKET, SO_LINGER, &abrupt, sizeof abrupt);
  }

 private:
  bool fill(std::chrono::milliseconds wait) {
    if (ssl_ == nullptr || SSL_pending(ssl_) == 0) {
      pollfd target{fd_, POLLIN, 0};
      if (::poll(&target, 1, static_cast<int>(wait.count())) <= 0) return false;
    }
    char chunk[65536];
    const long count = ssl_ != nullptr
                           ? SSL_read(ssl_, chunk, static_cast<int>(sizeof chunk))
                           : static_cast<long>(::recv(fd_, chunk, sizeof chunk, 0));
    if (count <= 0) return false;
    buffer_.append(chunk, static_cast<std::size_t>(count));
    return true;
  }

  int fd_;
  SSL* ssl_;
  std::string buffer_;
};

/// The certificate a TLS server presents, and the highest version it offers.
struct ProtocolTls {
  std::filesystem::path certificate;
  std::filesystem::path key;
  int max_version = 0;  ///< `TLS1_2_VERSION` offers no more than 1.2; 0 the library's highest
};

/// Accepts on a loopback address and runs `handler` for every connection, each in a thread of its
/// own and, with TLS, after a handshake a refused client never gets past. Destroying it closes
/// every connection it holds.
class ProtocolServer {
 public:
  using Handler = std::function<void(Peer&)>;

  explicit ProtocolServer(Handler handler, std::optional<ProtocolTls> tls = std::nullopt,
                          const std::string& address = "127.0.0.1")
      : handler_(std::move(handler)) {
    if (tls) {
      // The server writes TLS records with write(2): a client that went away must not take this
      // process with it. The library's own writes never raise the signal, which a test of its own
      // checks in a child process where it is not ignored.
      (void)std::signal(SIGPIPE, SIG_IGN);
      context_ = SSL_CTX_new(TLS_server_method());
      tls_detail::check(context_ != nullptr, "a server context");
      tls_detail::check(SSL_CTX_use_certificate_file(context_, tls->certificate.c_str(),
                                                     SSL_FILETYPE_PEM) == 1,
                        "the server certificate");
      tls_detail::check(
          SSL_CTX_use_PrivateKey_file(context_, tls->key.c_str(), SSL_FILETYPE_PEM) == 1,
          "the server key");
      if (tls->max_version != 0) (void)SSL_CTX_set_max_proto_version(context_, tls->max_version);
    }
    const bool six = address.find(':') != std::string::npos;
    listener_ = ::socket(six ? AF_INET6 : AF_INET, SOCK_STREAM, 0);
    tls_detail::check(listener_ >= 0, "a socket");
    if (six) {
      sockaddr_in6 bound{};
      bound.sin6_family = AF_INET6;
      ::inet_pton(AF_INET6, address.c_str(), &bound.sin6_addr);
      tls_detail::check(::bind(listener_, reinterpret_cast<sockaddr*>(&bound), sizeof bound) == 0,
                        "binding");
      socklen_t length = sizeof bound;
      ::getsockname(listener_, reinterpret_cast<sockaddr*>(&bound), &length);
      port_ = ntohs(bound.sin6_port);
    } else {
      sockaddr_in bound{};
      bound.sin_family = AF_INET;
      ::inet_pton(AF_INET, address.c_str(), &bound.sin_addr);
      tls_detail::check(::bind(listener_, reinterpret_cast<sockaddr*>(&bound), sizeof bound) == 0,
                        "binding");
      socklen_t length = sizeof bound;
      ::getsockname(listener_, reinterpret_cast<sockaddr*>(&bound), &length);
      port_ = ntohs(bound.sin_port);
    }
    tls_detail::check(::listen(listener_, 16) == 0, "listening");
    acceptor_ = std::thread([this] { accept_loop(); });
  }

  ~ProtocolServer() {
    stopping_ = true;
    ::shutdown(listener_, SHUT_RDWR);
    ::close(listener_);
    acceptor_.join();
    std::vector<std::thread> running;
    {
      const std::lock_guard<std::mutex> lock(mutex_);
      for (const int fd : open_) ::shutdown(fd, SHUT_RDWR);
      running = std::move(threads_);
    }
    for (std::thread& thread : running) thread.join();
    if (context_ != nullptr) SSL_CTX_free(context_);
  }
  ProtocolServer(const ProtocolServer&) = delete;
  ProtocolServer& operator=(const ProtocolServer&) = delete;

  [[nodiscard]] int port() const noexcept { return port_; }
  /// TCP connections accepted so far.
  [[nodiscard]] int accepted() const {
    const std::lock_guard<std::mutex> lock(mutex_);
    return accepted_;
  }
  /// Handshakes that failed, waiting up to a second for one: a client that refuses the certificate
  /// gives up before this side has read its alert.
  [[nodiscard]] int failed_handshakes(bool wait_for_one = false) const {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(1);
    while (wait_for_one && std::chrono::steady_clock::now() < deadline) {
      {
        const std::lock_guard<std::mutex> lock(mutex_);
        if (failed_ > 0) return failed_;
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    const std::lock_guard<std::mutex> lock(mutex_);
    return failed_;
  }
  /// The server name each completed handshake carried, empty where it carried none.
  [[nodiscard]] std::vector<std::string> server_names() const {
    const std::lock_guard<std::mutex> lock(mutex_);
    return names_;
  }

 private:
  void accept_loop() {
    while (!stopping_) {
      const int fd = ::accept(listener_, nullptr, nullptr);
      if (fd < 0) return;
      const std::lock_guard<std::mutex> lock(mutex_);
      ++accepted_;
      open_.insert(fd);
      threads_.emplace_back([this, fd] { run(fd); });
    }
  }

  void run(int fd) {
    SSL* ssl = nullptr;
    bool served = true;
    if (context_ != nullptr) {
      ssl = SSL_new(context_);
      SSL_set_fd(ssl, fd);
      if (SSL_accept(ssl) != 1) {
        served = false;
        const std::lock_guard<std::mutex> lock(mutex_);
        ++failed_;
      } else {
        const char* name = SSL_get_servername(ssl, TLSEXT_NAMETYPE_host_name);
        const std::lock_guard<std::mutex> lock(mutex_);
        names_.emplace_back(name == nullptr ? "" : name);
      }
    }
    if (served) {
      Peer peer(fd, ssl);
      handler_(peer);
    }
    if (ssl != nullptr) SSL_free(ssl);
    const std::lock_guard<std::mutex> lock(mutex_);
    open_.erase(fd);
    ::close(fd);
  }

  Handler handler_;
  SSL_CTX* context_ = nullptr;
  int listener_ = -1;
  int port_ = 0;
  std::atomic<bool> stopping_{false};
  std::thread acceptor_;
  mutable std::mutex mutex_;
  std::vector<std::thread> threads_;
  std::set<int> open_;
  int accepted_ = 0;
  int failed_ = 0;
  std::vector<std::string> names_;
};

/// The greeting a real server sends.
inline constexpr std::string_view kGreeting = "OK ob_tcp_server v0.1.0\n\n";

/// One row as the engine stores it, its numbers kept as written so a test can hold what no field
/// of the model holds (`quantity` 2^64 - 1).
struct StoredRow {
  std::string symbol;
  std::string exchange;
  std::uint64_t timestamp = 0;
  int side = 0;  ///< 0 bid, 1 ask, as the engine answers it
  std::string level = "0";
  std::int64_t price = 0;
  std::string quantity = "1";
  std::string count = "1";
  std::string sequence = "1";
};

/// A book store that answers like the engine: arrival order, `LIMIT` on arrival, `NOT_FOUND`.
class FakeOrderbook {
 public:
  explicit FakeOrderbook(std::optional<ProtocolTls> tls = std::nullopt)
      : server_(std::make_unique<ProtocolServer>([this](Peer& peer) { serve_safely(peer); },
                                                 std::move(tls))) {}

  [[nodiscard]] int port() const noexcept { return server_->port(); }
  [[nodiscard]] std::string dsn(const std::string& query = "") const {
    return "orderbook://127.0.0.1:" + std::to_string(port()) + "?timeout=5" + query;
  }

  void capabilities(std::string text) {
    const std::lock_guard<std::mutex> lock(mutex_);
    capabilities_ = std::move(text);
  }
  /// Client authentication, as `--auth-secret-file` gives it.
  void require_auth(std::string identity, std::string secret) {
    const std::lock_guard<std::mutex> lock(mutex_);
    auth_ = std::make_pair(std::move(identity), std::move(secret));
  }
  /// Refuses this write, counted from 0 over INSERTs and MINSERTs.
  void refuse_write(int number) {
    const std::lock_guard<std::mutex> lock(mutex_);
    refuse_.insert(number);
  }
  /// Ends the connection on receiving this write, counted from 1, with nothing answered.
  void drop_on_write(int number) {
    const std::lock_guard<std::mutex> lock(mutex_);
    drop_on_ = number;
  }
  /// Stores an update as a write would: levels numbered by position, one sequence number per book.
  void store(const std::string& symbol, const std::string& exchange, int side,
             std::uint64_t timestamp,
             const std::vector<std::tuple<std::int64_t, std::int64_t, std::int64_t>>& levels) {
    const std::lock_guard<std::mutex> lock(mutex_);
    store_locked(symbol, exchange, side, timestamp, levels);
  }
  void add_row(StoredRow row) {
    const std::lock_guard<std::mutex> lock(mutex_);
    rows_.push_back(std::move(row));
  }

  /// Every command received, a MINSERT with its body.
  [[nodiscard]] std::vector<std::string> commands() const {
    const std::lock_guard<std::mutex> lock(mutex_);
    return commands_;
  }
  /// The commands that wrote.
  [[nodiscard]] std::vector<std::string> writes() const {
    std::vector<std::string> out;
    for (const std::string& command : commands()) {
      if (command.starts_with("INSERT ") || command.starts_with("MINSERT ")) out.push_back(command);
    }
    return out;
  }
  [[nodiscard]] std::vector<std::string> queries() const {
    std::vector<std::string> out;
    for (const std::string& command : commands()) {
      if (command.starts_with("SELECT ")) out.push_back(command);
    }
    return out;
  }
  [[nodiscard]] std::vector<StoredRow> rows() const {
    const std::lock_guard<std::mutex> lock(mutex_);
    return rows_;
  }
  void forget_commands() {
    const std::lock_guard<std::mutex> lock(mutex_);
    commands_.clear();
  }
  [[nodiscard]] int connections() const { return server_->accepted(); }

 private:
  void store_locked(const std::string& symbol, const std::string& exchange, int side,
                    std::uint64_t timestamp,
                    const std::vector<std::tuple<std::int64_t, std::int64_t, std::int64_t>>& levels) {
    const std::int64_t sequence = ++sequences_[symbol + '\0' + exchange];
    for (std::size_t level = 0; level < levels.size(); ++level) {
      const auto& [price, quantity, count] = levels[level];
      rows_.push_back({symbol, exchange, timestamp, side, std::to_string(level), price,
                       std::to_string(quantity), std::to_string(count), std::to_string(sequence)});
    }
  }

  static std::string hmac_hex(const std::string& key, const std::string& message) {
    unsigned char digest[EVP_MAX_MD_SIZE];
    unsigned int length = 0;
    HMAC(EVP_sha256(), key.data(), static_cast<int>(key.size()),
         reinterpret_cast<const unsigned char*>(message.data()), message.size(), digest, &length);
    static const char* hex = "0123456789abcdef";
    std::string out;
    for (unsigned int i = 0; i < length; ++i) {
      out += hex[digest[i] >> 4U];
      out += hex[digest[i] & 0x0FU];
    }
    return out;
  }

  static std::vector<std::string> words(const std::string& line) {
    std::vector<std::string> out;
    std::size_t start = 0;
    while (start <= line.size()) {
      const std::size_t space = line.find(' ', start);
      out.push_back(line.substr(start, space == std::string::npos ? std::string::npos : space - start));
      if (space == std::string::npos) break;
      start = space + 1;
    }
    return out;
  }

  void serve(Peer& peer) {
    std::optional<std::pair<std::string, std::string>> auth;
    {
      const std::lock_guard<std::mutex> lock(mutex_);
      auth = auth_;
    }
    bool authenticated = !auth.has_value();
    std::string nonce;
    if (!peer.send(kGreeting)) return;
    for (;;) {
      std::optional<std::string> line = peer.line(std::chrono::seconds(30));
      if (!line) return;
      std::string command = *line;
      std::vector<std::string> body;
      if (command.starts_with("MINSERT ")) {
        const std::vector<std::string> parts = words(command);
        const std::size_t count = parts.size() > 4 ? std::stoul(parts[4]) : 0;
        for (std::size_t i = 0; i < count; ++i) {
          std::optional<std::string> level = peer.line();
          if (!level) return;
          body.push_back(*level);
          command += "\n" + *level;
        }
      }
      {
        const std::lock_guard<std::mutex> lock(mutex_);
        commands_.push_back(command);
      }
      if (*line == "QUIT") return;
      if (*line == "PING") {
        if (!peer.send("PONG\n")) return;
        continue;
      }
      if (*line == "AUTH") {
        if (!auth) {
          if (!peer.send("ERR auth_disabled\n")) return;
          continue;
        }
        unsigned char random[32];
        RAND_bytes(random, sizeof random);
        nonce = hmac_hex("nonce", std::string(reinterpret_cast<const char*>(random), sizeof random));
        if (!peer.send("OK CHALLENGE " + nonce + "\n\n")) return;
        continue;
      }
      if (line->starts_with("AUTH ")) {
        const std::vector<std::string> parts = words(*line);
        std::string message = "ob-auth-v1";
        for (const std::string& field : {std::string("client"), std::string("initiator"),
                                         parts.size() > 1 ? parts[1] : std::string(), nonce}) {
          message += '\0';
          message += field;
        }
        if (auth && !nonce.empty() && parts.size() == 3 && parts[1] == auth->first &&
            parts[2] == hmac_hex(auth->second, message)) {
          authenticated = true;
          if (!peer.send("OK AUTH " + auth->first + "\n\n")) return;
          continue;
        }
        (void)peer.send("ERR auth_failed\n");
        return;
      }
      if (!authenticated) {
        if (!peer.send("ERR unauthenticated\n")) return;
        continue;
      }
      if (line->starts_with("INSERT ") || line->starts_with("MINSERT ")) {
        bool drop = false;
        bool refused = false;
        {
          const std::lock_guard<std::mutex> lock(mutex_);
          ++writes_;
          drop = drop_on_ == writes_;
          refused = refuse_.contains(writes_ - 1);
          if (!drop && !refused) {
            const std::vector<std::string> parts = words(*line);
            const int side = parts[3] == "ask" ? 1 : 0;
            std::vector<std::tuple<std::int64_t, std::int64_t, std::int64_t>> levels;
            if (parts[0] == "INSERT") {
              levels.emplace_back(std::stoll(parts[4]), std::stoll(parts[5]), std::stoll(parts[6]));
              store_locked(parts[1], parts[2], side, std::stoull(parts[7]), levels);
            } else {
              for (const std::string& item : body) {
                const std::vector<std::string> numbers = words(item);
                levels.emplace_back(std::stoll(numbers[0]), std::stoll(numbers[1]),
                                    std::stoll(numbers[2]));
              }
              store_locked(parts[1], parts[2], side, std::stoull(parts[5]), levels);
            }
          }
        }
        if (drop) return;
        if (!peer.send(refused ? "ERR something the server said\n" : "OK\n\n")) return;
        continue;
      }
      if (*line == "FLUSH") {
        if (!peer.send("OK\n\n")) return;
        continue;
      }
      if (*line == "STATUS") {
        std::string capabilities;
        {
          const std::lock_guard<std::mutex> lock(mutex_);
          capabilities = capabilities_;
        }
        if (!peer.send("OK\nsessions\tqueries\tinserts\n1\t0\t0\ncapabilities: " + capabilities +
                       "\nrole: standalone\n\n")) {
          return;
        }
        continue;
      }
      if (!peer.send(select(*line))) return;
    }
  }

  /// A command's numbers read as the engine reads them; one it cannot read is its refusal, never an
  /// exception in a server thread.
  void serve_safely(Peer& peer) {
    try {
      serve(peer);
    } catch (const std::exception&) {
      (void)peer.send("ERR unknown command\n");
    }
  }

  std::string select(const std::string& line) {
    static const std::regex grammar(
        R"(^SELECT \* FROM '([^']+)'\.'([^']+)'(?: WHERE timestamp BETWEEN (\d+) AND (\d+)(?: AND price BETWEEN (-?\d+) AND (-?\d+))?)? LIMIT (\d+)$)");
    std::smatch match;
    if (!std::regex_match(line, match, grammar)) return "ERR the fake cannot parse: " + line + "\n";
    try {
      return select(match);
    } catch (const std::out_of_range&) {
      return "ERR Parse error: invalid integer literal\n";  // past what the engine's parser reads
    }
  }

  std::string select(const std::smatch& match) {
    const std::lock_guard<std::mutex> lock(mutex_);
    std::vector<const StoredRow*> book;
    for (const StoredRow& row : rows_) {
      if (row.symbol == match[1].str() && row.exchange == match[2].str()) book.push_back(&row);
    }
    if (book.empty()) {
      return "ERR OB_ERR_NOT_FOUND: symbol '" + match[1].str() + "' exchange '" + match[2].str() +
             "' not found\n";
    }
    std::string out = "OK\ntimestamp_ns\tprice\tquantity\torder_count\tside\tlevel\tsequence_number\n";
    std::size_t sent = 0;
    const std::size_t limit = std::stoull(match[7].str());
    for (const StoredRow* row : book) {
      if (sent == limit) break;
      if (match[3].matched) {
        const std::uint64_t low = std::stoull(match[3].str());
        const std::uint64_t high = std::stoull(match[4].str());
        if (row->timestamp < low || row->timestamp > high) continue;
      }
      if (match[5].matched) {
        const std::int64_t low = std::stoll(match[5].str());
        const std::int64_t high = std::stoll(match[6].str());
        if (row->price < low || row->price > high) continue;
      }
      out += std::to_string(row->timestamp) + "\t" + std::to_string(row->price) + "\t" +
             row->quantity + "\t" + row->count + "\t" + std::to_string(row->side) + "\t" +
             row->level + "\t" + row->sequence + "\n";
      ++sent;
    }
    return out + "\n";
  }

  mutable std::mutex mutex_;
  std::string capabilities_ = "insert_event_time,strict_args,backup";
  std::optional<std::pair<std::string, std::string>> auth_;
  std::set<int> refuse_;
  int drop_on_ = -1;
  int writes_ = 0;
  std::map<std::string, std::int64_t> sequences_;
  std::vector<StoredRow> rows_;
  std::vector<std::string> commands_;
  std::unique_ptr<ProtocolServer> server_;  ///< last, so it stops before the state it serves from
};

}  // namespace sde::live

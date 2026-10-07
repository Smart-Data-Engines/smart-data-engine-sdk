#include "engines/orderbook/wire.hpp"

#include <arpa/inet.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>

#include <openssl/err.h>
#include <openssl/ssl.h>
#include <openssl/x509_vfy.h>
#include <openssl/x509v3.h>

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <climits>
#include <cmath>
#include <cstdio>
#include <optional>
#include <system_error>
#include <utility>

#include "crypto.hpp"
#include "python_compat.hpp"
#include "python_url.hpp"
#include "sde/errors.hpp"
#include "unicode_internal.hpp"

namespace sde::detail::orderbook {

namespace {

constexpr std::size_t kChunk = 65536;

/// A wait that ran out of its bound of silence. Each caller words it as the client words it there.
struct Silence {};

/// `[Errno N] text`, as Python writes an `OSError`.
std::string os_error_text(int code) {
  return "[Errno " + std::to_string(code) + "] " + std::system_category().message(code);
}

/// The `OSError` subclass Python raises for an errno, which the reference logs.
std::string os_error_class(int code) {
  switch (code) {
    case EPIPE:
    case ESHUTDOWN:
      return "BrokenPipeError";
    case ECONNABORTED:
      return "ConnectionAbortedError";
    case ECONNREFUSED:
      return "ConnectionRefusedError";
    case ECONNRESET:
      return "ConnectionResetError";
    case ETIMEDOUT:
      return "TimeoutError";
    case EACCES:
    case EPERM:
      return "PermissionError";
    default:
      return "OSError";
  }
}

[[noreturn]] void os_error(int code) { throw WireError(os_error_text(code), os_error_class(code)); }

/// The first `count` characters of bytes decoded as the client decodes them.
std::string first_characters(std::string_view bytes, std::size_t count) {
  const std::string text = python_url::decode_replacing(bytes);
  std::size_t at = 0;
  for (std::size_t taken = 0; at < text.size() && taken < count; ++taken) {
    at += std::max<std::size_t>(1, utf8_sequence_length(text, at));
  }
  return text.substr(0, at);
}

/// `text.split(separator)`: every piece, empty ones included.
std::vector<std::string> split(std::string_view text, char separator) {
  std::vector<std::string> out;
  std::size_t start = 0;
  for (;;) {
    const std::size_t at = text.find(separator, start);
    if (at == std::string_view::npos) {
      out.emplace_back(text.substr(start));
      return out;
    }
    out.emplace_back(text.substr(start, at - start));
    start = at + 1;
  }
}

/// Waits for the socket to be ready for `events`; false when `seconds` pass in silence. An error or
/// a hang-up counts as ready: the call that follows says what happened.
bool wait(int fd, short events, double seconds) {
  const auto start = std::chrono::steady_clock::now();
  for (;;) {
    const double left =
        seconds - std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();
    if (left <= 0) return false;
    const double milliseconds = std::ceil(left * 1000.0);
    const int bound = milliseconds >= static_cast<double>(INT_MAX)
                          ? INT_MAX
                          : std::max(1, static_cast<int>(milliseconds));
    pollfd target{fd, events, 0};
    const int ready = ::poll(&target, 1, bound);
    if (ready > 0) return true;
    if (ready < 0 && errno != EINTR) os_error(errno);
  }
}

/// `sock.settimeout(timeout)`: CPython keeps a timeout as int64 nanoseconds, rounded up, and refuses
/// one past them in these words before any socket connects.
void check_timeout(double seconds) {
  if (!(std::ceil(seconds * 1e9) < 9223372036854775808.0)) {
    throw WireError("timestamp out of range for platform time_t", "OverflowError");
  }
}

/// An IPv4 or IPv6 address as text, which `a2i_IPADDRESS` reads: no SNI for it, and its own check.
bool ip_literal(const std::string& host) {
  in6_addr address{};
  return ::inet_pton(AF_INET, host.c_str(), &address) == 1 ||
         ::inet_pton(AF_INET6, host.c_str(), &address) == 1;
}

/// The name CPython's `ssl` module gives an OpenSSL library in a message.
std::string library_name(unsigned long error) {
  switch (ERR_GET_LIB(error)) {
    case ERR_LIB_SSL:
      return "SSL";
    case ERR_LIB_X509:
      return "X509";
    case ERR_LIB_X509V3:
      return "X509V3";
    case ERR_LIB_PEM:
      return "PEM";
    case ERR_LIB_SYS:
      return "SYS";
    case ERR_LIB_ASN1:
      return "ASN1";
    case ERR_LIB_EVP:
      return "EVP";
    case ERR_LIB_BIO:
      return "BIO";
    default: {
      const char* name = ERR_lib_error_string(error);
      return name == nullptr ? "" : name;
    }
  }
}

/// What OpenSSL's last error says, as CPython words an `ssl.SSLError`: `[LIB: REASON] text`,
/// without the `(_ssl.c:N)` CPython appends. A reason's symbol is its text in capitals with
/// underscores, which is CPython's table for every reason a connection meets. A failed verification
/// says why, as CPython says it, and an end of the stream inside a record is CPython's `EOF occurred
/// in violation of protocol`.
std::string tls_failure(const SSL* ssl, const std::string& host) {
  const unsigned long error = ERR_peek_last_error();
  ERR_clear_error();
  if (error == 0) return "EOF occurred in violation of protocol";
  const char* reason_text = ERR_reason_error_string(error);
  if (reason_text == nullptr) return "[" + library_name(error) + "] unknown error";
  std::string symbol;
  for (const char c : std::string_view(reason_text)) {
    symbol += c == ' ' ? '_' : static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
  }
  std::string text = reason_text;
  if (ERR_GET_LIB(error) == ERR_LIB_SSL && ERR_GET_REASON(error) == SSL_R_UNEXPECTED_EOF_WHILE_READING) {
    text = "EOF occurred in violation of protocol";
  }
  if (ssl != nullptr && ERR_GET_LIB(error) == ERR_LIB_SSL &&
      ERR_GET_REASON(error) == SSL_R_CERTIFICATE_VERIFY_FAILED) {
    const long code = SSL_get_verify_result(ssl);
    if (code == X509_V_ERR_HOSTNAME_MISMATCH) {
      text += ": Hostname mismatch, certificate is not valid for '" + host + "'.";
    } else if (code == X509_V_ERR_IP_ADDRESS_MISMATCH) {
      text += ": IP address mismatch, certificate is not valid for '" + host + "'.";
    } else {
      text += std::string(": ") + X509_verify_cert_error_string(code);
    }
  }
  return "[" + library_name(error) + ": " + symbol + "] " + text;
}

struct ContextFree {
  void operator()(SSL_CTX* context) const noexcept { SSL_CTX_free(context); }
};
using Context = std::unique_ptr<SSL_CTX, ContextFree>;

/// `os.path.isfile`: a regular file once links are followed; a path holding a NUL is none.
bool regular_file(const std::string& path) {
  if (path.find('\0') != std::string::npos) return false;
  struct stat status {};
  return ::stat(path.c_str(), &status) == 0 && S_ISREG(status.st_mode);
}

/// `_build_tls_context`: TLS 1.3 at least, the CA file the one trust anchor or else the system's,
/// and verification unless it was declined. Built before any socket, so a bad CA is refused before
/// anything connects, in the client's words.
Context tls_context(const Target& target) {
  if (!target.tls) return nullptr;
  const auto refused = [](const std::string& text) { return WireError(text, "OrderbookTlsError"); };
  if (target.ca && !target.verify) {
    throw refused("tls_ca_file is set with tls_verify=False: a trust anchor nothing consults");
  }
  if (target.ca && !regular_file(*target.ca)) {
    throw refused("tls_ca_file " + python_repr(*target.ca) + " is not a readable file");
  }
  Context context(SSL_CTX_new(TLS_client_method()));
  if (!context || SSL_CTX_set_min_proto_version(context.get(), TLS1_3_VERSION) != 1) {
    throw refused(tls_failure(nullptr, target.host));
  }
  if (target.ca) {
    // `load_verify_locations` reports a file it cannot open as the `OSError` of opening it.
    std::FILE* file = std::fopen(target.ca->c_str(), "rb");
    if (file == nullptr) {
      throw refused("tls_ca_file " + python_repr(*target.ca) + " rejected: " + os_error_text(errno));
    }
    std::fclose(file);
    ERR_clear_error();
    if (SSL_CTX_load_verify_locations(context.get(), target.ca->c_str(), nullptr) != 1) {
      throw refused("tls_ca_file " + python_repr(*target.ca) + " rejected: " +
                    tls_failure(nullptr, target.host));
    }
  } else {
    (void)SSL_CTX_set_default_verify_paths(context.get());
  }
  SSL_CTX_set_verify(context.get(), target.verify ? SSL_VERIFY_PEER : SSL_VERIFY_NONE, nullptr);
  return context;
}

/// A descriptor closed when it goes out of scope, unless it was handed on.
class Descriptor {
 public:
  explicit Descriptor(int fd) noexcept : fd_(fd) {}
  ~Descriptor() {
    if (fd_ >= 0) ::close(fd_);
  }
  Descriptor(const Descriptor&) = delete;
  Descriptor& operator=(const Descriptor&) = delete;
  [[nodiscard]] int get() const noexcept { return fd_; }
  int release() noexcept { return std::exchange(fd_, -1); }

 private:
  int fd_;
};

/// A TCP connection to the first address of the host that takes one, each attempt bounded by the
/// timeout; the last failure when none does. Python's own words for each.
int connect_tcp(const std::string& host, int port, double timeout) {
  addrinfo hints{};
  hints.ai_family = AF_UNSPEC;
  hints.ai_socktype = SOCK_STREAM;
  hints.ai_flags = AI_NUMERICSERV;
  addrinfo* found = nullptr;
  const int resolved = ::getaddrinfo(host.c_str(), std::to_string(port).c_str(), &hints, &found);
  if (resolved != 0) {
    if (resolved == EAI_SYSTEM) os_error(errno);
    throw WireError("[Errno " + std::to_string(resolved) + "] " + ::gai_strerror(resolved),
                    "gaierror");
  }
  const std::unique_ptr<addrinfo, decltype(&::freeaddrinfo)> addresses(found, ::freeaddrinfo);
  std::optional<WireError> last;
  for (const addrinfo* address = found; address != nullptr; address = address->ai_next) {
    Descriptor socket(::socket(address->ai_family, address->ai_socktype | SOCK_NONBLOCK | SOCK_CLOEXEC,
                               address->ai_protocol));
    if (socket.get() < 0) {
      last.emplace(os_error_text(errno), os_error_class(errno));
      continue;
    }
    int failure = 0;
    if (::connect(socket.get(), address->ai_addr, address->ai_addrlen) != 0) {
      if (errno != EINPROGRESS) {
        failure = errno;
      } else if (!wait(socket.get(), POLLOUT, timeout)) {
        last.emplace("timed out", "TimeoutError");
        continue;
      } else {
        socklen_t length = sizeof failure;
        if (::getsockopt(socket.get(), SOL_SOCKET, SO_ERROR, &failure, &length) != 0) failure = errno;
      }
    }
    if (failure != 0) {
      last.emplace(os_error_text(failure), os_error_class(failure));
      continue;
    }
    const int on = 1;
    (void)::setsockopt(socket.get(), IPPROTO_TCP, TCP_NODELAY, &on, sizeof on);
    return socket.release();
  }
  throw last.value_or(WireError("[Errno -2] Name or service not known", "gaierror"));
}

/// `text.encode("ascii")`, refused in the words of Python's `UnicodeEncodeError`.
void ascii_or_refuse(std::string_view text) {
  const std::optional<std::u32string> code_points = decode_utf8(text);
  if (!code_points) return;
  const std::u32string& characters = *code_points;
  std::size_t start = 0;
  while (start < characters.size() && characters[start] < 0x80) ++start;
  if (start == characters.size()) return;
  std::size_t end = start;
  while (end < characters.size() && characters[end] >= 0x80) ++end;
  std::string message = "'ascii' codec can't encode ";
  if (end - start == 1) {
    const auto c = static_cast<unsigned>(characters[start]);
    char shown[16];
    if (c <= 0xFF) {
      std::snprintf(shown, sizeof shown, "\\x%02x", c);
    } else if (c <= 0xFFFF) {
      std::snprintf(shown, sizeof shown, "\\u%04x", c);
    } else {
      std::snprintf(shown, sizeof shown, "\\U%08x", c);
    }
    message += "character '" + std::string(shown) + "' in position " + std::to_string(start);
  } else {
    message += "characters in position " + std::to_string(start) + "-" + std::to_string(end - 1);
  }
  throw WireError(message + ": ordinal not in range(128)", "UnicodeEncodeError");
}

/// Whether bytes that have arrived could still become an answer this client reads.
bool could_begin_an_answer(std::string_view bytes) {
  static constexpr std::string_view kHeads[] = {"OK",      "ERR ",       "PONG",         "PRIMARY",
                                                "REPLICA", "STANDALONE", "MULTI_MASTER", "PUSH "};
  for (const std::string_view head : kHeads) {
    if (bytes.size() >= head.size() ? bytes.starts_with(head) : head.starts_with(bytes)) return true;
  }
  return false;
}

/// The first word of a command, which is all an exchange's label repeats: the rest of an `AUTH`
/// line is a digest.
std::string verb_of(std::string_view command) {
  const std::string_view stripped = python_strip(command);
  if (stripped.empty()) return "an empty command";
  std::size_t end = 0;
  while (end < stripped.size() && python_space_at(stripped, end) == 0) {
    end += std::max<std::size_t>(1, utf8_sequence_length(stripped, end));
  }
  return std::string(stripped.substr(0, end));
}

}  // namespace

// --- the stream: a socket, plain or TLS over memory buffers --------------------------------------

class Connection::Stream {
 public:
  Stream(const Target& target, SSL_CTX* context, std::string where)
      : timeout_(target.timeout), host_(target.host), where_(std::move(where)) {
    fd_ = connect_tcp(target.host, target.port, timeout_);
    if (context == nullptr) return;
    try {
      secure(target, context);
    } catch (...) {
      release();  // a constructor that throws runs no destructor
      throw;
    }
  }

  ~Stream() { release(); }
  Stream(const Stream&) = delete;
  Stream& operator=(const Stream&) = delete;

 private:
  /// TLS over memory buffers: SNI for a name, the name or the address verified unless declined.
  void secure(const Target& target, SSL_CTX* context) {
    ssl_ = SSL_new(context);
    BIO* in = BIO_new(BIO_s_mem());
    BIO* out = BIO_new(BIO_s_mem());
    if (ssl_ == nullptr || in == nullptr || out == nullptr) {
      BIO_free(in);
      BIO_free(out);
      throw WireError("TLS handshake with " + where_ + " failed: " + tls_failure(nullptr, host_),
                      "OrderbookTlsError");
    }
    SSL_set_bio(ssl_, in, out);
    in_ = in;
    out_ = out;
    SSL_set_connect_state(ssl_);
    const bool literal = ip_literal(host_);
    // `SSL_set_tlsext_host_name`, spelled out: the macro casts in C's way.
    if (!literal) {
      (void)SSL_ctrl(ssl_, SSL_CTRL_SET_TLSEXT_HOSTNAME, TLSEXT_NAMETYPE_host_name,
                     const_cast<char*>(host_.c_str()));
    }
    if (target.verify) {
      X509_VERIFY_PARAM* parameters = SSL_get0_param(ssl_);
      X509_VERIFY_PARAM_set_hostflags(parameters, X509_CHECK_FLAG_NO_PARTIAL_WILDCARDS);
      if (literal) {
        (void)X509_VERIFY_PARAM_set1_ip_asc(parameters, host_.c_str());
      } else {
        (void)X509_VERIFY_PARAM_set1_host(parameters, host_.c_str(), 0);
      }
    }
    handshake();
  }

  void release() noexcept {
    if (ssl_ != nullptr) SSL_free(ssl_);
    ssl_ = nullptr;
    if (fd_ >= 0) ::close(fd_);
    fd_ = -1;
  }

 public:
  [[nodiscard]] bool tls() const noexcept { return ssl_ != nullptr; }

  /// Sends all of it within one bound, as `sendall` does; past the bound, the words of a timed-out
  /// send on this kind of socket.
  void send_all(std::string_view bytes) {
    if (ssl_ == nullptr) {
      try {
        send_raw(bytes);
      } catch (const Silence&) {
        throw WireError("timed out", "TimeoutError");
      }
      return;
    }
    std::size_t written = 0;
    while (written < bytes.size()) {
      ERR_clear_error();
      std::size_t count = 0;
      if (SSL_write_ex(ssl_, bytes.data() + written, bytes.size() - written, &count) == 1) {
        written += count;
        continue;
      }
      throw WireError(tls_failure(ssl_, host_), "SSLError");
    }
    try {
      flush();
    } catch (const Silence&) {
      throw WireError("The write operation timed out", "TimeoutError");
    }
  }

  /// Appends what arrives; false at the end of the stream. `Silence` when the bound passes first.
  /// A TLS stream that ends inside a record has ended, as `suppress_ragged_eofs` makes it.
  bool receive(std::string& into) {
    if (ssl_ == nullptr) return receive_raw(into);
    char chunk[kChunk];
    for (;;) {
      ERR_clear_error();
      const int count = SSL_read(ssl_, chunk, static_cast<int>(sizeof chunk));
      if (count > 0) {
        into.append(chunk, static_cast<std::size_t>(count));
        return true;
      }
      const int error = SSL_get_error(ssl_, count);
      if (error == SSL_ERROR_ZERO_RETURN) return false;
      if (error != SSL_ERROR_WANT_READ) throw WireError(tls_failure(ssl_, host_), "SSLError");
      flush();
      if (ended_) return false;
      if (!fill()) {
        ended_ = true;
        return false;
      }
    }
  }

 private:
  void handshake() {
    bool ended = false;
    for (;;) {
      ERR_clear_error();
      const int result = SSL_do_handshake(ssl_);
      try {
        flush();
      } catch (const Silence&) {
        throw WireError("The handshake operation timed out", "TimeoutError");
      }
      if (result == 1) return;
      if (SSL_get_error(ssl_, result) != SSL_ERROR_WANT_READ || ended) {
        throw WireError("TLS handshake with " + where_ + " failed: " + tls_failure(ssl_, host_),
                        "OrderbookTlsError");
      }
      try {
        ended = !fill();
      } catch (const Silence&) {
        throw WireError("The handshake operation timed out", "TimeoutError");
      }
      // The end of the stream, told to OpenSSL so that it names it.
      if (ended) BIO_set_mem_eof_return(in_, 0);
    }
  }

  /// Whatever OpenSSL has written, sent.
  void flush() {
    std::string pending;
    char chunk[16384];
    for (;;) {
      const int count = BIO_read(out_, chunk, static_cast<int>(sizeof chunk));
      if (count <= 0) break;
      pending.append(chunk, static_cast<std::size_t>(count));
    }
    if (!pending.empty()) send_raw(pending);
  }

  /// What the socket has next, given to OpenSSL; false at the end of the stream.
  bool fill() {
    std::string bytes;
    if (!receive_raw(bytes)) return false;
    (void)BIO_write(in_, bytes.data(), static_cast<int>(bytes.size()));
    return true;
  }

  void send_raw(std::string_view bytes) {
    const auto start = std::chrono::steady_clock::now();
    std::size_t sent = 0;
    while (sent < bytes.size()) {
      const ssize_t count = ::send(fd_, bytes.data() + sent, bytes.size() - sent, MSG_NOSIGNAL);
      if (count >= 0) {
        sent += static_cast<std::size_t>(count);
        continue;
      }
      if (errno == EINTR) continue;
      if (errno != EAGAIN && errno != EWOULDBLOCK) os_error(errno);
      const double spent =
          std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();
      if (!wait(fd_, POLLOUT, timeout_ - spent)) throw Silence{};
    }
  }

  bool receive_raw(std::string& into) {
    char chunk[kChunk];
    for (;;) {
      const ssize_t count = ::recv(fd_, chunk, sizeof chunk, 0);
      if (count > 0) {
        into.append(chunk, static_cast<std::size_t>(count));
        return true;
      }
      if (count == 0) return false;
      if (errno == EINTR) continue;
      if (errno != EAGAIN && errno != EWOULDBLOCK) os_error(errno);
      if (!wait(fd_, POLLIN, timeout_)) throw Silence{};
    }
  }

  int fd_ = -1;
  SSL* ssl_ = nullptr;
  BIO* in_ = nullptr;   ///< owned by `ssl_`
  BIO* out_ = nullptr;  ///< owned by `ssl_`
  bool ended_ = false;
  double timeout_;
  std::string host_;
  std::string where_;
};

// --- answers --------------------------------------------------------------------------------------

Answer parse_answer(std::string_view raw) {
  Answer out;
  if (raw.starts_with("ERR ")) {
    std::string_view message = raw.substr(4);
    while (!message.empty() && message.back() == '\n') message.remove_suffix(1);
    out.error = true;
    out.message = message;
    return out;
  }
  if (raw.starts_with("PONG")) return out;
  if (raw.starts_with("OK\n")) {
    const std::vector<std::string> lines = split(raw.substr(3), '\n');
    std::size_t at = 0;
    while (at < lines.size() && lines[at].empty()) ++at;
    if (at >= lines.size()) return out;
    out.header = split(lines[at], '\t');
    for (++at; at < lines.size() && !lines[at].empty(); ++at) out.rows.push_back(split(lines[at], '\t'));
    return out;
  }
  if (python_strip(raw) == "OK") return out;
  out.error = true;
  out.message = "unexpected response: " + first_characters(raw, 80);
  return out;
}

std::map<std::string, std::string> status_fields(std::string_view raw) {
  std::map<std::string, std::string> out;
  for (const std::string& untrimmed : split(raw, '\n')) {
    const std::string_view line = python_strip(untrimmed);
    if (line.empty()) continue;
    if (line.starts_with('[')) break;
    const std::size_t colon = line.find(':');
    if (colon == std::string_view::npos) continue;  // the OK line and the counters' row
    const std::string key(python_strip(line.substr(0, colon)));
    const std::string value(python_strip(line.substr(colon + 1)));
    if (key.empty() || key.find(' ') != std::string::npos) continue;  // prose, not a field
    try {
      const python_url::PythonInt number = python_url::int_of(value);
      out[key] = (number.negative ? "-" : "") + number.digits;
    } catch (const python_url::PythonValueError&) {
      out[key] = value;
    } catch (const python_url::PythonIntLimit&) {
      out[key] = value;
    }
  }
  return out;
}

std::set<std::string> server_capabilities(std::string_view raw) {
  const Answer answer = parse_answer(raw);
  if (answer.error) throw WireError("STATUS error: " + answer.message, "OrderbookError");
  if (!answer.rows.empty() && answer.rows.front().size() >= 3) {
    for (std::size_t i = 0; i < 3; ++i) {
      try {
        (void)python_value_int(Value(answer.rows.front()[i]));
      } catch (const EngineError& error) {
        throw WireError(error.what(), "ValueError");
      }
    }
  }
  const std::map<std::string, std::string> fields = status_fields(raw);
  const auto line = fields.find("capabilities");
  std::set<std::string> out;
  if (line == fields.end()) return out;
  for (const std::string& name : split(line->second, ',')) {
    if (!name.empty()) out.insert(name);
  }
  return out;
}

std::string auth_digest(std::string_view identity, std::string_view secret, std::string_view nonce) {
  std::string message = "ob-auth-v1";
  for (const std::string_view field : {std::string_view("client"), std::string_view("initiator"),
                                       identity, nonce}) {
    message += '\0';
    message += field;
  }
  return to_hex(hmac_sha256(secret, message));
}

// --- the connection -------------------------------------------------------------------------------

Connection::Connection(const Target& target)
    : target_(target), where_(target.host + ":" + std::to_string(target.port)) {
  const Context context = tls_context(target_);
  // The handshake: one exchange with no connection before it, so a failure leaves nothing open.
  check_timeout(target_.timeout);
  stream_ = std::make_unique<Stream>(target_, context.get(), where_);
  read_greeting();
  if (target_.auth) authenticate();
}

Connection::~Connection() = default;

void Connection::read_greeting() {
  for (;;) {
    // Refused as soon as it cannot be one, where the client would wait for its timeout.
    if (!(buffer_.empty() || (buffer_.size() == 1 ? buffer_ == "O" : buffer_.starts_with("OK")))) {
      const std::size_t end = buffer_.find("\n\n");
      throw WireError("unexpected greeting from " + where_ + ": " +
                          first_characters(std::string_view(buffer_).substr(0, end), 80),
                      "OrderbookError");
    }
    const std::size_t end = buffer_.find("\n\n");
    if (end != std::string::npos) {
      buffer_.erase(0, end + 2);
      return;
    }
    bool more = false;
    try {
      more = stream_->receive(buffer_);
    } catch (const Silence&) {
      throw WireError(stream_->tls() ? "The read operation timed out" : "timed out", "TimeoutError");
    }
    if (!more) throw WireError("Connection closed before banner", "OrderbookError");
  }
}

void Connection::authenticate() {
  // Challenge and response, never the secret: the digest answers a fresh nonce.
  const auto& [identity, secret] = *target_.auth;
  stream_->send_all("AUTH\n");
  const std::string raw_challenge = answer();
  const std::string challenge(python_strip(raw_challenge));
  if (!challenge.starts_with("OK CHALLENGE ")) {
    throw WireError("Server refused the authentication request: " + challenge, "OrderbookError");
  }
  const std::string nonce(python_strip(std::string_view(challenge).substr(13)));
  ascii_or_refuse(nonce);
  stream_->send_all("AUTH " + identity + " " + auth_digest(identity, secret, nonce) + "\n");
  const std::string raw_verdict = answer();
  const std::string verdict(python_strip(raw_verdict));
  if (!verdict.starts_with("OK AUTH")) {
    throw WireError("Authentication failed: " + verdict, "OrderbookError");
  }
}

std::string Connection::answer() {
  for (;;) {
    // A pushed row may sit in front of the answer: complete `PUSH ` lines are skipped.
    while (buffer_.starts_with("PUSH ")) {
      const std::size_t newline = buffer_.find('\n');
      if (newline == std::string::npos) break;
      buffer_.erase(0, newline + 1);
    }
    std::size_t end = std::string::npos;
    if (buffer_.starts_with("ERR ") || buffer_.starts_with("PONG") ||
        buffer_.starts_with("PRIMARY") || buffer_.starts_with("REPLICA") ||
        buffer_.starts_with("STANDALONE") || buffer_.starts_with("MULTI_MASTER")) {
      const std::size_t newline = buffer_.find('\n');
      if (newline != std::string::npos) end = newline + 1;
    } else if (buffer_.starts_with("OK")) {
      const std::size_t blank = buffer_.find("\n\n");
      if (blank != std::string::npos) end = blank + 2;
    } else if (!could_begin_an_answer(buffer_)) {
      throw WireError("an answer from " + where_ + " this client cannot read: " +
                          first_characters(buffer_, 80),
                      "OrderbookError");
    }
    if (end != std::string::npos) {
      std::string raw = python_url::decode_replacing(std::string_view(buffer_).substr(0, end));
      buffer_.erase(0, end);
      return raw;
    }
    bool more = false;
    try {
      more = stream_->receive(buffer_);
    } catch (const Silence&) {
      throw WireError("TCP recv timeout", "OrderbookError");
    }
    if (!more) throw WireError("TCP connection closed by server", "OrderbookError");
  }
}

void Connection::ensure_open() const {
  if (stream_) return;
  if (closed_because_.empty()) {
    throw WireError("the connection to " + where_ + " is closed", "OrderbookError");
  }
  throw WireError("the connection to " + where_ + " was closed because " + closed_because_ +
                      ". The rest of that exchange may still have been on its way, and the next "
                      "command would have read it as its own reply, so nothing more is sent on "
                      "this connection and nothing was retried. Open a new client; a pool replaces "
                      "the connection at its next health check.",
                  "OrderbookError");
}

template <typename Body>
auto Connection::exchange(const std::string& label, const Body& body) -> decltype(body()) {
  ensure_open();
  try {
    return body();
  } catch (const WireError& error) {
    const std::string what = error.what();
    abandon("an exchange (" + label + ") did not finish: " + (what.empty() ? error.python_class() : what));
    throw;
  } catch (const std::exception& error) {
    abandon("an exchange (" + label + ") did not finish: " + error.what());
    throw;
  }
}

void Connection::abandon(const std::string& reason) noexcept {
  // No QUIT: the stream is out of step, so nothing more goes on it.
  if (closed_because_.empty()) closed_because_ = reason;
  stream_.reset();
  buffer_.clear();
}

std::string Connection::execute(std::string_view command) {
  return exchange(verb_of(command), [&] {
    std::string line(command);
    if (!line.ends_with('\n')) line += '\n';
    stream_->send_all(line);
    return answer();
  });
}

std::vector<std::string> Connection::execute_pipelined(const std::vector<std::string>& commands) {
  if (commands.empty()) return {};
  return exchange("a pipelined batch of " + std::to_string(commands.size()) + " command(s)", [&] {
    std::string bytes;
    for (const std::string& command : commands) {
      bytes += command;
      if (!command.ends_with('\n')) bytes += '\n';
    }
    stream_->send_all(bytes);
    std::vector<std::string> answers;
    answers.reserve(commands.size());
    for (std::size_t i = 0; i < commands.size(); ++i) answers.push_back(answer());
    return answers;
  });
}

void Connection::close() noexcept {
  if (!stream_) return;
  try {
    stream_->send_all("QUIT\n");
  } catch (...) {
    // A connection that cannot say QUIT is closed all the same.
  }
  stream_.reset();
  buffer_.clear();
}

}  // namespace sde::detail::orderbook

#include "engines/clickhouse/http.hpp"

#include <curl/curl.h>
#include <fcntl.h>
#include <openssl/bio.h>
#include <openssl/err.h>
#include <openssl/pem.h>
#include <openssl/x509.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <memory>
#include <mutex>
#include <string>

#include "sde/errors.hpp"
#include "unicode_internal.hpp"

namespace sde::detail::clickhouse {

namespace {

constexpr std::size_t kMaxCaBytes = 1024 * 1024;
constexpr std::size_t kMaxErrorCharacters = 1024;
constexpr const char* kCaRefused =
    "ClickHouse CA file must contain readable valid PEM certificates within 1 MiB";

using Clock = std::chrono::steady_clock;

void initialise_once() {
  static std::once_flag once;
  std::call_once(once, [] { (void)curl_global_init(CURL_GLOBAL_DEFAULT); });
}

/// What one exchange received, and when it last received anything.
struct Exchange {
  std::string body;
  std::optional<std::string> exception_code;
  Clock::time_point heard = Clock::now();
  std::chrono::milliseconds silence{0};
  bool silent_too_long = false;
};

std::size_t on_body(char* data, std::size_t size, std::size_t count, void* user) {
  auto& exchange = *static_cast<Exchange*>(user);
  exchange.body.append(data, size * count);
  exchange.heard = Clock::now();
  return size * count;
}

std::size_t on_header(char* data, std::size_t size, std::size_t count, void* user) {
  auto& exchange = *static_cast<Exchange*>(user);
  exchange.heard = Clock::now();  // a progress header is the server saying it is still working
  std::string_view line(data, size * count);
  const std::size_t colon = line.find(':');
  if (colon != std::string_view::npos) {
    std::string name(line.substr(0, colon));
    for (char& c : name) c = static_cast<char>(c >= 'A' && c <= 'Z' ? c - 'A' + 'a' : c);
    if (name == "x-clickhouse-exception-code") {
      std::string_view value = line.substr(colon + 1);
      while (!value.empty() && (value.front() == ' ' || value.front() == '\t')) value.remove_prefix(1);
      while (!value.empty() && (value.back() == '\r' || value.back() == '\n' || value.back() == ' ')) {
        value.remove_suffix(1);
      }
      exchange.exception_code = std::string(value);
    }
  }
  return size * count;
}

int on_progress(void* user, curl_off_t, curl_off_t, curl_off_t, curl_off_t) {
  auto& exchange = *static_cast<Exchange*>(user);
  if (Clock::now() - exchange.heard > exchange.silence) {
    exchange.silent_too_long = true;
    return 1;  // abort: the bound is on silence, not on how long the server works
  }
  return 0;
}

/// Python's `str.strip()` on the first 1,024 characters: what the reference's driver keeps.
std::string clipped(std::string_view text) {
  std::size_t at = 0;
  std::size_t characters = 0;
  while (at < text.size() && characters < kMaxErrorCharacters) {
    const std::size_t length = utf8_sequence_length(text, at);
    at += length == 0 ? 1 : length;
    ++characters;
  }
  std::string_view kept = text.substr(0, at);
  return std::string(strip_white_space(kept));
}

std::string escaped(CURL* handle, std::string_view text) {
  char* raw = curl_easy_escape(handle, text.data(), static_cast<int>(text.size()));
  std::string out(raw != nullptr ? raw : "");
  curl_free(raw);
  return out;
}

}  // namespace

std::string read_ca(const std::string& path) {
  const int descriptor = ::open(path.c_str(), O_RDONLY | O_NONBLOCK | O_CLOEXEC);
  if (descriptor < 0) throw EngineError(kCaRefused);
  std::string payload;
  struct stat info {};
  const bool regular = ::fstat(descriptor, &info) == 0 && S_ISREG(info.st_mode) && info.st_size > 0 &&
                       static_cast<std::size_t>(info.st_size) <= kMaxCaBytes;
  if (regular) {
    char buffer[65536];
    while (payload.size() <= kMaxCaBytes) {
      const ssize_t read = ::read(descriptor, buffer, std::min(sizeof buffer, kMaxCaBytes + 1 - payload.size()));
      if (read <= 0) break;
      payload.append(buffer, static_cast<std::size_t>(read));
    }
  }
  ::close(descriptor);
  if (!regular || payload.empty() || payload.size() > kMaxCaBytes) throw EngineError(kCaRefused);
  for (const char c : payload) {
    if (static_cast<unsigned char>(c) > 0x7F) throw EngineError(kCaRefused);
  }
  // At least one certificate, and every PEM block a valid one: what loading the bytes into an
  // empty store does in the reference.
  const std::unique_ptr<BIO, decltype(&BIO_free)> bio(
      BIO_new_mem_buf(payload.data(), static_cast<int>(payload.size())), &BIO_free);
  int certificates = 0;
  while (X509* certificate = PEM_read_bio_X509(bio.get(), nullptr, nullptr, nullptr)) {
    X509_free(certificate);
    ++certificates;
  }
  const unsigned long last = ERR_peek_last_error();
  const bool clean_end = ERR_GET_LIB(last) == ERR_LIB_PEM && ERR_GET_REASON(last) == PEM_R_NO_START_LINE;
  ERR_clear_error();
  if (certificates == 0 || (last != 0 && !clean_end)) throw EngineError(kCaRefused);
  return payload;
}

Http::Http(Target target) : target_(std::move(target)) {
  initialise_once();
  if (target_.ca_cert) ca_ = read_ca(*target_.ca_cert);
}

std::string Http::url() const {
  const std::string host = target_.host.find(':') != std::string::npos ? "[" + target_.host + "]"
                                                                         : target_.host;
  return std::string(target_.interface()) + "://" + host + ":" + std::to_string(target_.port);
}

std::string Http::post(std::string_view sql, std::optional<std::string_view> format,
                       std::string_view data, bool handshake, std::string_view settings) const {
  const std::unique_ptr<CURL, decltype(&curl_easy_cleanup)> handle(curl_easy_init(),
                                                                   &curl_easy_cleanup);
  if (!handle) throw TransportFailure("no transfer handle");
  CURL* curl = handle.get();
  const std::string statement =
      format ? std::string(sql) + " FORMAT " + std::string(*format) : std::string(sql);
  // The reference's driver keeps a long query alive with progress headers at this interval, so the
  // bound on silence never cuts a query that is still running.
  const long interval = std::min<long>(
      120000, std::max<long>(10000, (static_cast<long>(std::ceil(target_.send_receive_timeout)) - 5) * 1000));
  // The statement travels as the body, as the reference's driver sends it, so a value in a WHERE
  // clause is never part of a URL that a proxy between here and the server would log. Only an
  // INSERT that carries rows names its statement in the URL - that statement holds no value - and
  // sends the rows as the body.
  std::string query = url() + "/?database=" + escaped(curl, target_.database) +
                      (data.empty() ? std::string() : "&query=" + escaped(curl, statement)) +
                      "&session_timezone=UTC&date_time_output_format=iso"
                      "&wait_end_of_query=1&send_progress_in_http_headers=1"
                      "&http_headers_progress_interval_ms=" + std::to_string(interval) +
                      "&cancel_http_readonly_queries_on_client_close=1";
  if (format && format->starts_with("JSON")) {
    // A JSON number is a double to most readers, so every exact one comes quoted, and so do the
    // floats JSON has no number for.
    query +=
        "&output_format_json_quote_64bit_integers=1&output_format_json_quote_decimals=1"
        "&output_format_decimal_trailing_zeros=1&output_format_json_quote_denormals=1";
  }
  query += settings;
  Exchange exchange;
  exchange.silence = std::chrono::milliseconds(
      static_cast<long long>(std::ceil(target_.send_receive_timeout * 1000.0)));
  (void)curl_easy_setopt(curl, CURLOPT_URL, query.c_str());
  const std::string_view sent = data.empty() ? std::string_view(statement) : data;
  (void)curl_easy_setopt(curl, CURLOPT_POST, 1L);
  (void)curl_easy_setopt(curl, CURLOPT_POSTFIELDS, sent.data());
  (void)curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE_LARGE, static_cast<curl_off_t>(sent.size()));
  (void)curl_easy_setopt(curl, CURLOPT_HTTPAUTH, CURLAUTH_BASIC);
  (void)curl_easy_setopt(curl, CURLOPT_USERNAME, target_.username.c_str());
  (void)curl_easy_setopt(curl, CURLOPT_PASSWORD, target_.password.c_str());
  // A connection of its own, never reused and so never resent; no redirect followed.
  (void)curl_easy_setopt(curl, CURLOPT_FRESH_CONNECT, 1L);
  (void)curl_easy_setopt(curl, CURLOPT_FORBID_REUSE, 1L);
  (void)curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 0L);
  (void)curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
  (void)curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS,
                         static_cast<long>(std::ceil(target_.connect_timeout * 1000.0)));
  if (handshake) {
    (void)curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS,
                           static_cast<long>(std::ceil(target_.send_receive_timeout * 1000.0)));
  }
  (void)curl_easy_setopt(curl, CURLOPT_NOPROGRESS, 0L);
  (void)curl_easy_setopt(curl, CURLOPT_XFERINFOFUNCTION, &on_progress);
  (void)curl_easy_setopt(curl, CURLOPT_XFERINFODATA, &exchange);
  (void)curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, &on_body);
  (void)curl_easy_setopt(curl, CURLOPT_WRITEDATA, &exchange);
  (void)curl_easy_setopt(curl, CURLOPT_HEADERFUNCTION, &on_header);
  (void)curl_easy_setopt(curl, CURLOPT_HEADERDATA, &exchange);
  const std::unique_ptr<curl_slist, decltype(&curl_slist_free_all)> headers(
      curl_slist_append(curl_slist_append(nullptr, data.empty()
                                                       ? "Content-Type: text/plain; charset=utf-8"
                                                       : "Content-Type: application/octet-stream"),
                        "Expect:"),
      &curl_slist_free_all);
  (void)curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers.get());
  if (target_.secure) {
    (void)curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L);
    (void)curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 2L);
    if (!ca_.empty()) {
      curl_blob blob{const_cast<char*>(ca_.data()), ca_.size(), CURL_BLOB_COPY};
      (void)curl_easy_setopt(curl, CURLOPT_CAINFO_BLOB, &blob);
    }
  }
  exchange.heard = Clock::now();
  const CURLcode outcome = curl_easy_perform(curl);
  if (outcome != CURLE_OK) {
    throw TransportFailure(exchange.silent_too_long ? "no answer within the bound"
                                                    : curl_easy_strerror(outcome));
  }
  long status = 0;
  (void)curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);
  if (status >= 200 && status < 300) return std::move(exchange.body);
  std::optional<int> code;
  if (exchange.exception_code && !exchange.exception_code->empty() &&
      std::all_of(exchange.exception_code->begin(), exchange.exception_code->end(),
                  [](char c) { return c >= '0' && c <= '9'; }) &&
      exchange.exception_code->size() < 9) {
    code = std::stoi(*exchange.exception_code);
  }
  // The reference's driver: the code from the header, else the status; the body's first 1,024
  // characters, stripped; and the URL, which names no credential.
  std::string message = exchange.exception_code
                            ? "Received ClickHouse exception, code: " + *exchange.exception_code
                            : "HTTP driver received HTTP status " + std::to_string(status);
  const std::string body = clipped(exchange.body);
  if (!body.empty()) message += ", server response: " + body;
  message += " (for url " + url() + ")";
  throw ServerError(code, std::move(message));
}

}  // namespace sde::detail::clickhouse

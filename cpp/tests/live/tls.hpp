#pragma once

/// TLS for the live tests: ephemeral certificate material made with OpenSSL in the test's own
/// directory - a CA, an unrelated CA and server certificates for each identity case, no private key
/// ever printed - and a PostgreSQL endpoint that speaks only SSLRequest, `S` and TLS, then reads
/// the StartupMessage and closes. It is not a database: libpq sends StartupMessage only after it
/// has verified the server's certificate, so seeing one is the witness that verification passed,
/// and its absence that the certificate was refused. The reference's `tls_certificates.py` and
/// `pg_endpoint`, in C++.

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>

#include <openssl/err.h>
#include <openssl/evp.h>
#include <openssl/pem.h>
#include <openssl/ssl.h>
#include <openssl/x509.h>
#include <openssl/x509v3.h>

#include <atomic>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <map>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "live/live.hpp"

namespace sde::live {

namespace tls_detail {

struct KeyFree {
  void operator()(EVP_PKEY* key) const noexcept { EVP_PKEY_free(key); }
};
struct CertFree {
  void operator()(X509* cert) const noexcept { X509_free(cert); }
};
using Key = std::unique_ptr<EVP_PKEY, KeyFree>;
using Cert = std::unique_ptr<X509, CertFree>;

inline void check(bool ok, const char* what) {
  if (!ok) throw std::runtime_error(std::string("OpenSSL: ") + what);
}

inline void extension(X509* cert, X509* issuer, int nid, const std::string& value) {
  X509V3_CTX context;
  X509V3_set_ctx_nodb(&context);
  X509V3_set_ctx(&context, issuer, cert, nullptr, nullptr, 0);
  X509_EXTENSION* made = X509V3_EXT_conf_nid(nullptr, &context, nid, value.c_str());
  check(made != nullptr, "an extension");
  check(X509_add_ext(cert, made, -1) == 1, "adding an extension");
  X509_EXTENSION_free(made);
}

/// A certificate for `key`, named `label`, valid from `from_days` to `to_days` from now, signed by
/// `issuer_key` as `issuer` - or by itself when `issuer` is null.
inline Cert certificate(EVP_PKEY* key, const std::string& label, long from_days, long to_days,
                        X509* issuer, EVP_PKEY* issuer_key,
                        const std::vector<std::pair<int, std::string>>& extensions) {
  static std::atomic<long> serial{1000};
  Cert cert(X509_new());
  check(cert != nullptr, "a certificate");
  check(X509_set_version(cert.get(), 2) == 1, "version 3");
  check(ASN1_INTEGER_set(X509_get_serialNumber(cert.get()), ++serial) == 1, "a serial");
  check(X509_gmtime_adj(X509_getm_notBefore(cert.get()), from_days * 86400L) != nullptr, "from");
  check(X509_gmtime_adj(X509_getm_notAfter(cert.get()), to_days * 86400L) != nullptr, "until");
  check(X509_set_pubkey(cert.get(), key) == 1, "the public key");
  X509_NAME* name = X509_get_subject_name(cert.get());
  check(X509_NAME_add_entry_by_txt(name, "CN", MBSTRING_UTF8,
                                   reinterpret_cast<const unsigned char*>(label.c_str()), -1, -1,
                                   0) == 1,
        "the subject");
  X509* signer = issuer != nullptr ? issuer : cert.get();
  check(X509_set_issuer_name(cert.get(), X509_get_subject_name(signer)) == 1, "the issuer");
  for (const auto& [nid, value] : extensions) extension(cert.get(), signer, nid, value);
  check(X509_sign(cert.get(), issuer_key != nullptr ? issuer_key : key, EVP_sha256()) > 0,
        "signing");
  return cert;
}

inline void write_certificate(const std::filesystem::path& path, X509* cert) {
  FILE* file = std::fopen(path.c_str(), "wx");
  check(file != nullptr, "creating a certificate file");
  const bool written = PEM_write_X509(file, cert) == 1;
  std::fclose(file);
  check(written, "writing a certificate");
}

}  // namespace tls_detail

/// Every file of the material, by the reference's names: `ca`, `other_ca`, `server_cert`,
/// `wrong_host_cert`, `expired_cert`, `dns_only_cert`, `ip_only_cert` and `server_key`.
inline std::map<std::string, std::filesystem::path> create_material(
    const std::filesystem::path& directory) {
  using namespace tls_detail;
  std::filesystem::create_directories(directory);
  std::filesystem::permissions(directory, std::filesystem::perms::owner_all);
  const auto authority_extensions = std::vector<std::pair<int, std::string>>{
      {NID_basic_constraints, "critical,CA:TRUE,pathlen:0"},
      {NID_subject_key_identifier, "hash"},
      {NID_authority_key_identifier, "keyid:always"},
      {NID_key_usage, "critical,digitalSignature,keyCertSign,cRLSign"}};
  const Key ca_key(EVP_RSA_gen(2048));
  const Key other_key(EVP_RSA_gen(2048));
  const Key server_key(EVP_RSA_gen(2048));
  check(ca_key && other_key && server_key, "a key");
  const Cert ca = certificate(ca_key.get(), "SDE ephemeral test CA", -3, 30, nullptr, nullptr,
                              authority_extensions);
  const Cert other_ca = certificate(other_key.get(), "SDE unrelated ephemeral test CA", -3, 30,
                                    nullptr, nullptr, authority_extensions);
  const auto server = [&](const std::string& label, const std::string& names, bool expired) {
    return certificate(server_key.get(), label, -2, expired ? -1 : 7, ca.get(), ca_key.get(),
                       {{NID_basic_constraints, "critical,CA:FALSE"},
                        {NID_subject_key_identifier, "hash"},
                        {NID_authority_key_identifier, "keyid:always"},
                        {NID_key_usage, "critical,digitalSignature,keyEncipherment"},
                        {NID_subject_alt_name, names},
                        {NID_ext_key_usage, "serverAuth"}});
  };
  const std::string every_name = "DNS:localhost,IP:127.0.0.1,IP:::1";
  std::map<std::string, std::filesystem::path> paths;
  const std::vector<std::pair<std::string, Cert>> certificates = [&] {
    std::vector<std::pair<std::string, Cert>> out;
    out.emplace_back("server_cert", server("localhost", every_name, false));
    out.emplace_back("wrong_host_cert", server("wrong.invalid", "DNS:wrong.invalid", false));
    out.emplace_back("expired_cert", server("localhost", every_name, true));
    out.emplace_back("dns_only_cert", server("localhost", "DNS:localhost", false));
    out.emplace_back("ip_only_cert", server("127.0.0.1", "IP:127.0.0.1,IP:::1", false));
    return out;
  }();
  write_certificate(directory / "ca.pem", ca.get());
  paths["ca"] = directory / "ca.pem";
  write_certificate(directory / "other_ca.pem", other_ca.get());
  paths["other_ca"] = directory / "other_ca.pem";
  for (const auto& [name, cert] : certificates) {
    write_certificate(directory / (name + ".pem"), cert.get());
    paths[name] = directory / (name + ".pem");
  }
  const std::filesystem::path key_path = directory / "server_key.pem";
  FILE* file = std::fopen(key_path.c_str(), "wx");
  check(file != nullptr, "creating the key file");
  ::fchmod(::fileno(file), 0600);
  const bool written =
      PEM_write_PrivateKey(file, server_key.get(), nullptr, nullptr, 0, nullptr, nullptr) == 1;
  std::fclose(file);
  check(written, "writing the key");
  paths["server_key"] = key_path;
  return paths;
}

/// What a PostgreSQL TLS endpoint saw: connections, SSL requests, the StartupMessages that arrived
/// over TLS, and the handshakes that failed.
struct Observed {
  int tcp = 0;
  int ssl_requests = 0;
  std::vector<std::string> startups;
  int tls_errors = 0;
};

/// SSLRequest, `S`, TLS with `certificate`, the StartupMessage, and a close - on `address`.
class PostgresTlsEndpoint {
 public:
  PostgresTlsEndpoint(const std::filesystem::path& certificate, const std::filesystem::path& key,
                      const std::string& address)
      : context_(SSL_CTX_new(TLS_server_method())) {
    // OpenSSL writes to the socket with write(2), and libpq closes it the moment it refuses a
    // certificate: a process that does not ignore SIGPIPE dies there. Python ignores it from the
    // start, which is why the reference's endpoint never meets this.
    (void)std::signal(SIGPIPE, SIG_IGN);
    tls_detail::check(context_ != nullptr, "a server context");
    tls_detail::check(
        SSL_CTX_use_certificate_file(context_, certificate.c_str(), SSL_FILETYPE_PEM) == 1,
        "the server certificate");
    tls_detail::check(SSL_CTX_use_PrivateKey_file(context_, key.c_str(), SSL_FILETYPE_PEM) == 1,
                      "the server key");
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
    tls_detail::check(::listen(listener_, 8) == 0, "listening");
    worker_ = std::thread([this] { serve(); });
  }

  ~PostgresTlsEndpoint() {
    stopping_ = true;
    ::shutdown(listener_, SHUT_RDWR);
    ::close(listener_);
    worker_.join();
    SSL_CTX_free(context_);
  }

  PostgresTlsEndpoint(const PostgresTlsEndpoint&) = delete;
  PostgresTlsEndpoint& operator=(const PostgresTlsEndpoint&) = delete;

  [[nodiscard]] int port() const noexcept { return port_; }
  [[nodiscard]] Observed observed() const {
    const std::lock_guard<std::mutex> lock(mutex_);
    return observed_;
  }

 private:
  static bool exact(int socket, char* into, std::size_t amount) {
    std::size_t read = 0;
    while (read < amount) {
      const ssize_t piece = ::recv(socket, into + read, amount - read, 0);
      if (piece <= 0) return false;
      read += static_cast<std::size_t>(piece);
    }
    return true;
  }

  static std::uint32_t number(const char* bytes) {
    return (std::uint32_t{static_cast<unsigned char>(bytes[0])} << 24U) |
           (std::uint32_t{static_cast<unsigned char>(bytes[1])} << 16U) |
           (std::uint32_t{static_cast<unsigned char>(bytes[2])} << 8U) |
           std::uint32_t{static_cast<unsigned char>(bytes[3])};
  }

  void serve() {
    while (!stopping_) {
      const int accepted = ::accept(listener_, nullptr, nullptr);
      if (accepted < 0) return;
      handle(accepted);
      ::close(accepted);
    }
  }

  void handle(int socket) {
    {
      const std::lock_guard<std::mutex> lock(mutex_);
      ++observed_.tcp;
    }
    timeval timeout{2, 0};
    ::setsockopt(socket, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof timeout);
    char request[8];
    if (!exact(socket, request, 8)) return;
    if (number(request) == 8 && number(request + 4) == 80877104) {  // GSSENCRequest, if enabled
      if (::send(socket, "N", 1, MSG_NOSIGNAL) != 1 || !exact(socket, request, 8)) return;
    }
    if (number(request) != 8 || number(request + 4) != 80877103) return;  // SSLRequest
    {
      const std::lock_guard<std::mutex> lock(mutex_);
      ++observed_.ssl_requests;
    }
    if (::send(socket, "S", 1, MSG_NOSIGNAL) != 1) return;
    SSL* secure = SSL_new(context_);
    SSL_set_fd(secure, socket);
    if (SSL_accept(secure) != 1) {
      const std::lock_guard<std::mutex> lock(mutex_);
      ++observed_.tls_errors;
      ERR_clear_error();
      SSL_free(secure);
      return;
    }
    char size[4];
    std::string startup;
    if (SSL_read(secure, size, 4) == 4 && number(size) >= 8 && number(size) <= 65536) {
      startup.resize(number(size) - 4);
      std::size_t read = 0;
      while (read < startup.size()) {
        const int piece = SSL_read(secure, startup.data() + read,
                                   static_cast<int>(startup.size() - read));
        if (piece <= 0) break;
        read += static_cast<std::size_t>(piece);
      }
      if (read == startup.size() && number(startup.data()) == 196608) {  // protocol 3.0
        const std::lock_guard<std::mutex> lock(mutex_);
        observed_.startups.push_back(startup);
      }
    }
    // Refuse the rest of startup: a certificate witness, not a database.
    ERR_clear_error();
    SSL_free(secure);
  }

  SSL_CTX* context_;
  int listener_ = -1;
  int port_ = 0;
  std::atomic<bool> stopping_{false};
  mutable std::mutex mutex_;
  Observed observed_;
  std::thread worker_;
};

}  // namespace sde::live

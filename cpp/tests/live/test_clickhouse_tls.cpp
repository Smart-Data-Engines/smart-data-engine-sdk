/// TLS to ClickHouse is the URI's, verified against the CA it names - read once, qualified before
/// any socket, and pinned for the engine's life - and the certificate's identity is bound to the
/// host the URI names. Against an HTTPS witness rather than a server (`live/tls.hpp`): a request
/// arrives there only after the client verified the certificate, so its arrival is the witness that
/// verification passed, and its absence that the certificate was refused. Needs no DSN. Ported from
/// the reference's `test_clickhouse_tls.py`, where its cases are not the driver's own.

#include <sys/stat.h>
#include <unistd.h>

#include <cctype>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <functional>
#include <map>
#include <string>
#include <vector>

#include <gtest/gtest.h>

#include "live/live.hpp"
#include "live/tls.hpp"
#include "sde/clickhouse.hpp"
#include "sde/errors.hpp"

namespace {

using Clock = std::chrono::steady_clock;

std::string message_of(const std::function<void()>& body) {
  try {
    body();
  } catch (const std::exception& error) {
    return error.what();
  }
  return "accepted";
}

/// Percent-encoding for a CA path in a query string.
std::string encoded(const std::string& path) {
  static const char* digits = "0123456789ABCDEF";
  std::string out;
  for (const char c : path) {
    const auto byte = static_cast<unsigned char>(c);
    if (std::isalnum(byte) != 0 || c == '-' || c == '_' || c == '.' || c == '~') {
      out += c;
    } else {
      out += '%';
      out += digits[byte >> 4U];
      out += digits[byte & 0x0FU];
    }
  }
  return out;
}

std::string tls_dsn(int port, const std::filesystem::path& ca, const std::string& host = "localhost",
                    const std::string& query = "") {
  return "https://fixture:canary@" + host + ":" + std::to_string(port) + "/default?ca_cert=" +
         encoded(ca.string()) + query;
}

/// The material, made once for the suite in a directory of its own and removed after it.
class ClickHouseTls : public ::testing::Test {
 protected:
  static void SetUpTestSuite() {
    directory_ = new std::filesystem::path(std::filesystem::temp_directory_path() /
                                           ("sde-ch-tls-" + sde::live::fresh(12)));
    material_ = new std::map<std::string, std::filesystem::path>(
        sde::live::create_material(*directory_));
  }
  static void TearDownTestSuite() {
    std::filesystem::remove_all(*directory_);
    delete material_;
    delete directory_;
  }

  static const std::filesystem::path& file(const std::string& name) { return material_->at(name); }
  static std::filesystem::path scratch(const std::string& name) { return *directory_ / name; }

  /// A connect that must fail: the witness is not a server.
  static std::string refused(const std::string& dsn) {
    return message_of([&] { sde::ClickHouseEngine(dsn).connect(); });
  }

  static std::filesystem::path* directory_;
  static std::map<std::string, std::filesystem::path>* material_;
};

std::filesystem::path* ClickHouseTls::directory_ = nullptr;
std::map<std::string, std::filesystem::path>* ClickHouseTls::material_ = nullptr;

TEST_F(ClickHouseTls, ACustomCaAndADnsOrIpIdentityAllowOnlyAuthenticatedApplicationBytes) {
  for (const std::string host : {"localhost", "127.0.0.1", "[::1]"}) {
    SCOPED_TRACE(host);
    sde::live::HttpsEndpoint endpoint(file("server_cert"), file("server_key"),
                                      host == "[::1]" ? "::1" : "127.0.0.1");
    // The witness answers 400 after a verified handshake: refused by the "server", not by TLS.
    EXPECT_EQ(refused(tls_dsn(endpoint.port(), file("ca"), host)),
              "could not connect to ClickHouse with the configured transport");
    const sde::live::HttpsObserved seen = endpoint.observed();
    EXPECT_EQ(seen.tcp, 1);
    EXPECT_EQ(seen.tls_errors, 0);
    ASSERT_EQ(seen.requests.size(), 1U);
    EXPECT_TRUE(seen.requests[0].starts_with("POST ")) << seen.requests[0];
  }
}

TEST_F(ClickHouseTls, AnInvalidPeerCertificateReachesTlsButNeverApplicationBytes) {
  for (const std::string certificate : {"wrong_host_cert", "expired_cert"}) {
    SCOPED_TRACE(certificate);
    sde::live::HttpsEndpoint endpoint(file(certificate), file("server_key"), "127.0.0.1");
    EXPECT_EQ(refused(tls_dsn(endpoint.port(), file("ca"))),
              "could not connect to ClickHouse: ClickHouse transport failed; the operation was not "
              "replayed and its outcome may be unknown");
    // Where the refusal happens is libcurl's: 8.5 (Ubuntu 24.04) fails an expired chain inside the
    // handshake, and the witness sees the alert, but checks another host's name after it; 8.18
    // (Ubuntu 26.04) checks both after the handshake, and the witness sees none (measured). Python's
    // ssl module checks both during it. What every one of them guarantees, and what is asserted
    // here, is that no application byte reached the peer.
    const sde::live::HttpsObserved seen = endpoint.observed();
    EXPECT_EQ(seen.tcp, 1);
    EXPECT_TRUE(seen.requests.empty()) << "application bytes went to an unverified peer";
  }
}

TEST_F(ClickHouseTls, AnotherCaIsRefusedAndTrustsNothingForTheNextClient) {
  sde::live::HttpsEndpoint endpoint(file("server_cert"), file("server_key"), "127.0.0.1");
  (void)refused(tls_dsn(endpoint.port(), file("ca")));
  EXPECT_EQ(endpoint.observed().requests.size(), 1U);
  (void)refused(tls_dsn(endpoint.port(), file("other_ca")));
  // Refused inside the handshake or after it, as libcurl's version decides; never with a request.
  EXPECT_EQ(endpoint.observed().tcp, 2);
  EXPECT_EQ(endpoint.observed().requests.size(), 1U);
  (void)refused(tls_dsn(endpoint.port(), file("ca")));
  EXPECT_EQ(endpoint.observed().requests.size(), 2U);
}

TEST_F(ClickHouseTls, TheCaIsQualifiedBeforeASocketOpens) {
  // Absent, empty, not PEM, a directory, past 1 MiB, a FIFO and a link to one: refused in the
  // reference's words, naming neither a credential nor the file's content, before any connection.
  const std::filesystem::path base = scratch("damaged-" + sde::live::fresh(8));
  std::filesystem::create_directories(base);
  const auto write = [](const std::filesystem::path& path, const std::string& bytes) {
    std::ofstream(path, std::ios::binary) << bytes;
  };
  std::map<std::string, std::filesystem::path> damaged;
  damaged["absent"] = base / "absent.pem";
  write(damaged["empty"] = base / "empty.pem", "");
  write(damaged["malformed"] = base / "malformed.pem", "private-ca-content-canary, not PEM");
  std::filesystem::create_directories(damaged["directory"] = base / "directory");
  write(damaged["oversized"] = base / "oversized.pem", std::string(1024 * 1024 + 1, '#'));
  ASSERT_EQ(::mkfifo((base / "pipe").c_str(), 0600), 0);
  damaged["fifo"] = base / "pipe";
  std::filesystem::create_symlink(base / "pipe", damaged["symlink-fifo"] = base / "link.pem");
  sde::live::HttpsEndpoint endpoint(file("server_cert"), file("server_key"), "127.0.0.1");
  for (const auto& [damage, path] : damaged) {
    SCOPED_TRACE(damage);
    const std::string message = refused(tls_dsn(endpoint.port(), path));
    EXPECT_EQ(message,
              "could not connect to ClickHouse: ClickHouse CA file must contain readable valid PEM "
              "certificates within 1 MiB");
    EXPECT_EQ(message.find("canary"), std::string::npos);
  }
  EXPECT_EQ(endpoint.observed().tcp, 0);
}

TEST_F(ClickHouseTls, ARegularCaSymlinkIsAllowedAndQualified) {
  const std::filesystem::path alias = scratch("ca-link-" + sde::live::fresh(8) + ".pem");
  std::filesystem::create_symlink(file("ca"), alias);
  sde::live::HttpsEndpoint endpoint(file("server_cert"), file("server_key"), "127.0.0.1");
  (void)refused(tls_dsn(endpoint.port(), alias));
  EXPECT_EQ(endpoint.observed().tcp, 1);
  EXPECT_EQ(endpoint.observed().tls_errors, 0);
  EXPECT_EQ(endpoint.observed().requests.size(), 1U);
}

TEST_F(ClickHouseTls, AnHttpsRedirectNeverReachesAPlainHttpTarget) {
  sde::live::HttpsEndpoint plaintext("127.0.0.1");
  sde::live::HttpsEndpoint endpoint(file("server_cert"), file("server_key"), "127.0.0.1");
  (void)refused(tls_dsn(endpoint.port(), file("ca")));
  EXPECT_EQ(endpoint.observed().requests.size(), 1U);
  sde::live::HttpsEndpoint::Behaviour redirect;
  redirect.status = 302;
  redirect.location = "http://127.0.0.1:" + std::to_string(plaintext.port()) + "/redirected";
  endpoint.behave(redirect);
  (void)refused(tls_dsn(endpoint.port(), file("ca")));
  EXPECT_EQ(endpoint.observed().requests.size(), 2U);
  EXPECT_EQ(endpoint.observed().tls_errors, 0);
  EXPECT_EQ(plaintext.observed().tcp, 0) << "a redirect was followed to plain HTTP";
}

TEST_F(ClickHouseTls, AFractionalHandshakeBoundIsUsedBeforeTheServerAnswers) {
  sde::live::HttpsEndpoint::Behaviour hold;
  hold.hold = true;
  sde::live::HttpsEndpoint endpoint(file("server_cert"), file("server_key"), "127.0.0.1", hold);
  const auto started = Clock::now();
  (void)refused(tls_dsn(endpoint.port(), file("ca"), "localhost",
                        "&connect_timeout=0.25&send_receive_timeout=0.125"));
  const double seconds = std::chrono::duration<double>(Clock::now() - started).count();
  EXPECT_EQ(endpoint.observed().requests.size(), 1U);
  EXPECT_GE(seconds, 0.08);
  EXPECT_LT(seconds, 0.8);
}

TEST_F(ClickHouseTls, AConnectedEngineKeepsItsCaBytesWhileANewOneReadsTheChangedFile) {
  // The file is read once: changing it moves nothing for the engine that read it, and a new engine
  // reads the change - here, a CA the server's certificate does not chain to.
  const std::filesystem::path ca = scratch("rotating-" + sde::live::fresh(8) + ".pem");
  std::filesystem::copy_file(file("ca"), ca);
  sde::live::HttpsEndpoint::Behaviour server;
  server.initialize = true;
  sde::live::HttpsEndpoint endpoint(file("server_cert"), file("server_key"), "127.0.0.1", server);
  const std::string dsn = tls_dsn(endpoint.port(), ca);
  sde::ClickHouseEngine engine(dsn);
  engine.connect();
  EXPECT_EQ(engine.server_version(), "25.8.0.0");
  const int connections = endpoint.observed().tcp;
  std::filesystem::copy_file(file("other_ca"), ca, std::filesystem::copy_options::overwrite_existing);
  EXPECT_EQ(engine.count("anything"), 7U);
  EXPECT_GT(endpoint.observed().tcp, connections) << "every exchange is a connection of its own";
  const std::size_t requests = endpoint.observed().requests.size();
  EXPECT_EQ(refused(dsn),
            "could not connect to ClickHouse: ClickHouse transport failed; the operation was not "
            "replayed and its outcome may be unknown");
  EXPECT_EQ(endpoint.observed().requests.size(), requests);
}

}  // namespace

/// TLS to the orderbook engine as the engine's client sets it up (`_build_tls_context`): version 1.3
/// at least, the CA the DSN names as the one trust anchor, the certificate's identity bound to the
/// host - a name, with SNI, or an address, without - and verification declined only when the DSN
/// says so. Against a TLS server in this process with the reference's ephemeral material: a
/// connection whose handshake failed never reaches the protocol, so a greeting that arrived is the
/// witness that verification passed. Needs no DSN.

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>

#include <openssl/err.h>
#include <openssl/ssl.h>

#include <atomic>
#include <cctype>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <map>
#include <optional>
#include <string>
#include <thread>
#include <vector>

#include <gtest/gtest.h>

#include "engines/orderbook/dsn.hpp"
#include "engines/orderbook/wire.hpp"
#include "live/live.hpp"
#include "live/orderbook_fake.hpp"
#include "live/tls.hpp"

namespace {

namespace ob = sde::detail::orderbook;
using Clock = std::chrono::steady_clock;
using sde::live::Peer;
using sde::live::ProtocolServer;
using sde::live::ProtocolTls;
using std::chrono::milliseconds;

template <typename Body>
std::string failure_of(const Body& body) {
  try {
    body();
  } catch (const ob::WireError& error) {
    return error.python_class() + ": " + error.what();
  }
  return "accepted";
}

/// A server that greets and answers PING, counting the connections that got as far as the
/// protocol.
class Greeter {
 public:
  explicit Greeter(std::optional<ProtocolTls> tls, const std::string& address = "127.0.0.1")
      : server_(
            [this](Peer& peer) {
              ++greeted_;
              if (!peer.send(sde::live::kGreeting)) return;
              while (const std::optional<std::string> line = peer.line(milliseconds(2000))) {
                if (*line == "QUIT" || !peer.send("PONG\n")) return;
              }
            },
            std::move(tls), address) {}
  [[nodiscard]] int port() const { return server_.port(); }
  [[nodiscard]] int greeted() const { return greeted_; }
  [[nodiscard]] const ProtocolServer& server() const { return server_; }

 private:
  std::atomic<int> greeted_{0};
  ProtocolServer server_;
};

class OrderbookTls : public ::testing::Test {
 protected:
  static void SetUpTestSuite() {
    directory_ = new std::filesystem::path(std::filesystem::temp_directory_path() /
                                           ("sde-ob-tls-" + sde::live::fresh(12)));
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
  static ProtocolTls presenting(const std::string& certificate, int max_version = 0) {
    return ProtocolTls{file(certificate), file("server_key"), max_version};
  }
  static std::string dsn(const std::string& host, int port, const std::filesystem::path& ca,
                         const std::string& extra = "") {
    return "orderbook://" + host + ":" + std::to_string(port) + "?tls=on&timeout=2&ca=" +
           ca.string() + extra;
  }

  static std::filesystem::path* directory_;
  static std::map<std::string, std::filesystem::path>* material_;
};

std::filesystem::path* OrderbookTls::directory_ = nullptr;
std::map<std::string, std::filesystem::path>* OrderbookTls::material_ = nullptr;

TEST_F(OrderbookTls, ACustomCaAndADnsOrIpIdentityReachTheProtocol) {
  for (const std::string host : {"localhost", "127.0.0.1", "[::1]"}) {
    SCOPED_TRACE(host);
    Greeter server(presenting("server_cert"), host == "[::1]" ? "::1" : "127.0.0.1");
    ob::Connection connection(ob::parse_dsn(dsn(host, server.port(), file("ca"))));
    EXPECT_EQ(connection.execute("PING"), "PONG\n");
    EXPECT_EQ(server.greeted(), 1);
    // SNI names a host and never an address, which RFC 6066 forbids.
    EXPECT_EQ(server.server().server_names(),
              std::vector<std::string>{host == "localhost" ? "localhost" : ""});
  }
}

TEST_F(OrderbookTls, ACertificateThatDoesNotProveTheHostNeverReachesTheProtocol) {
  const std::string verify_failed =
      "OrderbookTlsError: TLS handshake with %s failed: [SSL: CERTIFICATE_VERIFY_FAILED] "
      "certificate verify failed: ";
  const auto expected = [&](const std::string& where, const std::string& why) {
    std::string out = verify_failed;
    out.replace(out.find("%s"), 2, where);
    return out + why;
  };
  struct Case {
    const char* certificate;
    const char* host;
    const char* ca;
    std::string why;
  };
  for (const Case& item : std::vector<Case>{
           {"wrong_host_cert", "localhost", "ca",
            "Hostname mismatch, certificate is not valid for 'localhost'."},
           {"ip_only_cert", "localhost", "ca",
            "Hostname mismatch, certificate is not valid for 'localhost'."},
           {"dns_only_cert", "127.0.0.1", "ca",
            "IP address mismatch, certificate is not valid for '127.0.0.1'."},
           {"expired_cert", "localhost", "ca", "certificate has expired"},
           {"server_cert", "localhost", "other_ca", "unable to get local issuer certificate"}}) {
    SCOPED_TRACE(item.certificate);
    Greeter server(presenting(item.certificate));
    const std::string where = std::string(item.host) + ":" + std::to_string(server.port());
    EXPECT_EQ(failure_of([&] { ob::Connection(ob::parse_dsn(dsn(item.host, server.port(), file(item.ca)))); }),
              expected(where, item.why));
    EXPECT_EQ(server.greeted(), 0) << "the protocol was reached past a refused certificate";
  }
}

TEST_F(OrderbookTls, DecliningVerificationAcceptsAnyCertificateAndStillEncrypts) {
  Greeter server(presenting("wrong_host_cert"));
  ob::Connection connection(ob::parse_dsn("orderbook://localhost:" + std::to_string(server.port()) +
                                          "?tls=on&verify=off&timeout=2"));
  EXPECT_EQ(connection.execute("PING"), "PONG\n");
  // Without a CA the system's store is the anchor, which knows nothing of this one.
  Greeter verified(presenting("server_cert"));
  EXPECT_NE(failure_of([&] {
              ob::Connection(ob::parse_dsn("orderbook://localhost:" +
                                           std::to_string(verified.port()) + "?tls=on&timeout=2"));
            }).find("unable to get local issuer certificate"),
            std::string::npos);
}

TEST_F(OrderbookTls, TheCaIsCheckedBeforeAnySocketInTheClientsWords) {
  Greeter server(presenting("server_cert"));
  const std::filesystem::path base = scratch("damaged-" + sde::live::fresh(8));
  std::filesystem::create_directories(base);
  ASSERT_EQ(::mkfifo((base / "pipe").c_str(), 0600), 0);
  std::filesystem::create_directories(base / "directory");
  { std::ofstream(base / "not.pem") << "private-ca-content-canary, not PEM"; }
  const auto refused = [&](const std::filesystem::path& ca, const std::string& extra = "") {
    return failure_of([&] { ob::Connection(ob::parse_dsn(dsn("localhost", server.port(), ca, extra))); });
  };
  for (const std::filesystem::path& path : {base / "absent.pem", base / "pipe", base / "directory"}) {
    SCOPED_TRACE(path);
    EXPECT_EQ(refused(path), "OrderbookTlsError: tls_ca_file '" + path.string() +
                                 "' is not a readable file");
  }
  EXPECT_EQ(refused(base / "not.pem"), "OrderbookTlsError: tls_ca_file '" + (base / "not.pem").string() +
                                           "' rejected: [X509: NO_CERTIFICATE_OR_CRL_FOUND] no "
                                           "certificate or crl found");
  EXPECT_EQ(refused(file("ca"), "&verify=off"),
            "OrderbookTlsError: tls_ca_file is set with tls_verify=False: a trust anchor nothing "
            "consults");
  if (::geteuid() != 0) {  // root reads a file whatever its mode
    std::filesystem::copy_file(file("ca"), base / "locked.pem");
    std::filesystem::permissions(base / "locked.pem", std::filesystem::perms::none);
    EXPECT_EQ(refused(base / "locked.pem"), "OrderbookTlsError: tls_ca_file '" +
                                                (base / "locked.pem").string() +
                                                "' rejected: [Errno 13] Permission denied");
  }
  EXPECT_EQ(server.server().accepted(), 0) << "a socket was opened for a CA that was refused";
}

TEST_F(OrderbookTls, AServerThatOffersLessThanTls13IsRefused) {
  Greeter server(presenting("server_cert", TLS1_2_VERSION));
  const std::string refused =
      failure_of([&] { ob::Connection(ob::parse_dsn(dsn("localhost", server.port(), file("ca")))); });
  EXPECT_TRUE(refused.starts_with("OrderbookTlsError: TLS handshake with localhost:" +
                                  std::to_string(server.port()) + " failed: [SSL: "))
      << refused;
  EXPECT_NE(refused.find("PROTOCOL_VERSION"), std::string::npos) << refused;
  EXPECT_EQ(server.greeted(), 0);
}

TEST_F(OrderbookTls, PlainTextAgainstATlsPortWaitsOutItsTimeout) {
  // Measured against the engine: the server waits for a handshake and the client for a greeting.
  Greeter server(presenting("server_cert"));
  const auto start = Clock::now();
  EXPECT_EQ(failure_of([&] {
              ob::Connection(ob::parse_dsn("orderbook://localhost:" + std::to_string(server.port()) +
                                           "?timeout=0.3"));
            }),
            "TimeoutError: timed out");
  EXPECT_GE(std::chrono::duration<double>(Clock::now() - start).count(), 0.25);
}

/// What this platform's OpenSSL itself says when a TLS 1.3 client reads a plain server's greeting
/// as a record, in CPython's `[SSL: REASON] text`: `wrong version number` on OpenSSL 3.0 and
/// `record layer failure` on 3.5 - both measured, and both what CPython says on the same OpenSSL.
std::string openssl_reading_plain_text(int port) {
  SSL_CTX* context = SSL_CTX_new(TLS_client_method());
  (void)SSL_CTX_set_min_proto_version(context, TLS1_3_VERSION);
  const int fd = ::socket(AF_INET, SOCK_STREAM, 0);
  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_port = htons(static_cast<std::uint16_t>(port));
  ::inet_pton(AF_INET, "127.0.0.1", &address.sin_addr);
  (void)::connect(fd, reinterpret_cast<const sockaddr*>(&address), sizeof address);
  SSL* ssl = SSL_new(context);
  SSL_set_fd(ssl, fd);
  ERR_clear_error();
  (void)SSL_connect(ssl);
  const char* reason = ERR_reason_error_string(ERR_peek_last_error());
  const std::string text = reason == nullptr ? "" : reason;
  ERR_clear_error();
  SSL_free(ssl);
  ::close(fd);
  SSL_CTX_free(context);
  std::string symbol;
  for (const char c : text) {
    symbol += c == ' ' ? '_' : static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
  }
  return "[SSL: " + symbol + "] " + text;
}

TEST_F(OrderbookTls, TlsAgainstAPlainPortReadsTheGreetingAsARecord) {
  Greeter server(std::nullopt);
  const std::string says = openssl_reading_plain_text(server.port());
  ASSERT_NE(says, "[SSL: ] ") << "OpenSSL itself refused nothing";
  EXPECT_EQ(failure_of([&] { ob::Connection(ob::parse_dsn(dsn("localhost", server.port(), file("ca")))); }),
            "OrderbookTlsError: TLS handshake with localhost:" + std::to_string(server.port()) +
                " failed: " + says);
}

TEST_F(OrderbookTls, AServerThatNeverGreetsAfterTheHandshakeIsReadTimedOutInTlsWords) {
  // CPython words a read that times out on a TLS socket `The read operation timed out`, and on a
  // plain one `timed out`; the engine's client lets either through before its first answer.
  ProtocolServer silent([](Peer& peer) { (void)peer.drain(milliseconds(2000)); },
                        presenting("server_cert"));
  const auto start = Clock::now();
  EXPECT_EQ(failure_of([&] {
              ob::Connection(ob::parse_dsn("orderbook://localhost:" + std::to_string(silent.port()) +
                                           "?tls=on&timeout=0.3&ca=" + file("ca").string()));
            }),
            "TimeoutError: The read operation timed out");
  EXPECT_GE(std::chrono::duration<double>(Clock::now() - start).count(), 0.25);
}

TEST_F(OrderbookTls, AServerThatHangsUpWithoutClosingTlsHasEndedTheStream) {
  // An end of the stream with no close_notify is an end, as `suppress_ragged_eofs` makes it for the
  // engine's client: the same words as on a plain connection, not a TLS failure.
  ProtocolServer abrupt([](Peer& peer) {
    if (!peer.send(sde::live::kGreeting) || !peer.line()) return;
    peer.close();  // a TCP end, no TLS alert before it
  },
                        presenting("server_cert"));
  ob::Connection connection(ob::parse_dsn(dsn("localhost", abrupt.port(), file("ca"))));
  EXPECT_EQ(failure_of([&] { (void)connection.execute("PING"); }),
            "OrderbookError: TCP connection closed by server");
}

TEST_F(OrderbookTls, AHandshakeTheServerNeverAnswersIsBoundedBySilence) {
  ProtocolServer silent([](Peer& peer) { (void)peer.drain(milliseconds(2000)); });
  const auto start = Clock::now();
  EXPECT_EQ(failure_of([&] {
              ob::Connection(ob::parse_dsn("orderbook://localhost:" + std::to_string(silent.port()) +
                                           "?tls=on&timeout=0.3&ca=" + file("ca").string()));
            }),
            "TimeoutError: The handshake operation timed out");
  const double seconds = std::chrono::duration<double>(Clock::now() - start).count();
  EXPECT_GE(seconds, 0.25);
  EXPECT_LT(seconds, 1.5);
}

TEST_F(OrderbookTls, AServerThatHangsUpInTheHandshakeIsAnEndOfStreamInViolationOfProtocol) {
  ProtocolServer hanging_up([](Peer& peer) {
    (void)peer.line(milliseconds(200));  // the ClientHello, part of it
    peer.close();
  });
  EXPECT_EQ(failure_of([&] { ob::Connection(ob::parse_dsn(dsn("localhost", hanging_up.port(), file("ca")))); }),
            "OrderbookTlsError: TLS handshake with localhost:" + std::to_string(hanging_up.port()) +
                " failed: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol");
}

TEST_F(OrderbookTls, AServerThatResetsTheHandshakeIsSaidAsTheSocketSaysIt) {
  // An OSError in the handshake is the engine's client's own, not wrapped: only an SSL error is.
  ProtocolServer resetting([](Peer& peer) {
    (void)peer.line(milliseconds(200));
    peer.reset_on_close();
  });
  EXPECT_EQ(failure_of([&] { ob::Connection(ob::parse_dsn(dsn("localhost", resetting.port(), file("ca")))); }),
            "ConnectionResetError: [Errno 104] Connection reset by peer");
}

TEST_F(OrderbookTls, TheCaIsReadAtEveryConnectAndAnOpenConnectionKeepsWhatItVerified) {
  // The engine's client builds its context at construction, and the reference builds a client at
  // every connect: a changed file moves nothing for a connection already open, and the next one
  // reads the change - here, a CA the server's certificate does not chain to.
  const std::filesystem::path ca = scratch("rotating-" + sde::live::fresh(8) + ".pem");
  std::filesystem::copy_file(file("ca"), ca);
  Greeter server(presenting("server_cert"));
  ob::Connection open(ob::parse_dsn(dsn("localhost", server.port(), ca)));
  std::filesystem::copy_file(file("other_ca"), ca, std::filesystem::copy_options::overwrite_existing);
  EXPECT_EQ(open.execute("PING"), "PONG\n");
  EXPECT_NE(failure_of([&] { ob::Connection(ob::parse_dsn(dsn("localhost", server.port(), ca))); })
                .find("unable to get local issuer certificate"),
            std::string::npos);
  EXPECT_EQ(server.greeted(), 1);
}

}  // namespace

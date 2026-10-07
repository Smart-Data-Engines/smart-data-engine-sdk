/// TLS to PostgreSQL is the DSN's, verified by libpq, and the certificate's identity is bound to the
/// host the DSN names: an IP literal to an IP name, a host name to a DNS name. Against a certificate
/// witness rather than a server (`live/tls.hpp`), so it needs no DSN and runs everywhere the live
/// tests build. Ported from the reference's `test_postgres_tls.py`.

#include <cctype>
#include <filesystem>
#include <map>
#include <string>
#include <tuple>

#include <gtest/gtest.h>

#include "live/live.hpp"
#include "live/tls.hpp"
#include "sde/errors.hpp"
#include "sde/postgres.hpp"

namespace {

/// The material, made once for the suite in a directory of its own and removed after it.
class PostgresTls
    : public ::testing::TestWithParam<std::tuple<std::string, std::string, std::string, bool>> {
 protected:
  static void SetUpTestSuite() {
    directory_ = new std::filesystem::path(std::filesystem::temp_directory_path() /
                                           ("sde-pg-tls-" + sde::live::fresh(12)));
    material_ = new std::map<std::string, std::filesystem::path>(
        sde::live::create_material(*directory_));
  }
  static void TearDownTestSuite() {
    std::filesystem::remove_all(*directory_);
    delete material_;
    delete directory_;
  }

  static std::filesystem::path* directory_;
  static std::map<std::string, std::filesystem::path>* material_;
};

std::filesystem::path* PostgresTls::directory_ = nullptr;
std::map<std::string, std::filesystem::path>* PostgresTls::material_ = nullptr;

TEST_P(PostgresTls, VerifyFullBindsTheCertificateToTheConfiguredHost) {
  const auto& [host, certificate, root, accepted] = GetParam();
  const std::string address = host == "::1" ? "::1" : "127.0.0.1";
  std::string refused;
  sde::live::Observed observed;
  {
    sde::live::PostgresTlsEndpoint endpoint(material_->at(certificate), material_->at("server_key"),
                                            address);
    const std::string dsn = "host=" + host + " port=" + std::to_string(endpoint.port()) +
                            " user=tls_probe password=synthetic dbname=tls_probe "
                            "sslmode=verify-full sslrootcert='" +
                            material_->at(root).string() + "' connect_timeout=2";
    sde::PostgresEngine engine(dsn);
    try {
      engine.connect();
      ADD_FAILURE() << "a certificate witness is not a database, and this connected to it";
    } catch (const sde::EngineError& error) {
      refused = error.what();
    }
    observed = endpoint.observed();
  }
  EXPECT_GE(observed.tcp, 1);
  EXPECT_EQ(observed.ssl_requests, 1);
  ASSERT_EQ(observed.startups.size(), accepted ? 1U : 0U) << refused;
  if (accepted) {
    EXPECT_NE(observed.startups[0].find(std::string("user\0tls_probe\0", 15)), std::string::npos);
    EXPECT_EQ(observed.tls_errors, 0);
  } else {
    // libpq may complete TLS and refuse the name afterwards, closing without an alert.
    EXPECT_NE(refused.find("certificate"), std::string::npos) << refused;
  }
}

INSTANTIATE_TEST_SUITE_P(
    Identities, PostgresTls,
    // The reference's six: an IP literal binds to an IP name and a host name to a DNS name. Then a
    // certificate with every name, one expired, one for another host, and one the DSN's root did
    // not sign - the cases the reference checks for ClickHouse alone.
    ::testing::Values(std::make_tuple("127.0.0.1", "dns_only_cert", "ca", false),
                      std::make_tuple("127.0.0.1", "ip_only_cert", "ca", true),
                      std::make_tuple("localhost", "dns_only_cert", "ca", true),
                      std::make_tuple("localhost", "ip_only_cert", "ca", false),
                      std::make_tuple("::1", "dns_only_cert", "ca", false),
                      std::make_tuple("::1", "ip_only_cert", "ca", true),
                      std::make_tuple("127.0.0.1", "server_cert", "ca", true),
                      std::make_tuple("localhost", "expired_cert", "ca", false),
                      std::make_tuple("localhost", "wrong_host_cert", "ca", false),
                      std::make_tuple("localhost", "server_cert", "other_ca", false)),
    [](const auto& param_info) {
      std::string name = std::get<0>(param_info.param) + "_" + std::get<1>(param_info.param) +
                         "_" + std::get<2>(param_info.param);
      for (char& c : name) {
        if (!std::isalnum(static_cast<unsigned char>(c))) c = '_';
      }
      return name;
    });

}  // namespace

/// `dsn/`: the orderbook engine's DSN, read by format contract 7n's grammar - what each one says, or
/// the refusal, which must not contain the DSN's secret. Every expectation was written by hand in
/// `conformance/tools/dsn_vectors.py`, from the contract, rather than by a library.

#include <string>

#include <gtest/gtest.h>

#include "engines/orderbook/dsn.hpp"
#include "sde/errors.hpp"
#include "support/vectors.hpp"

namespace {

using namespace sde::testing_support;
using sde::detail::orderbook::parse_dsn;
using sde::detail::orderbook::Target;

class DsnVector : public ::testing::TestWithParam<std::string> {};

/// A member that is a string, or null: `std::nullopt` for null.
std::optional<std::string> text_or_null(const sde::Json& value) {
  if (value.is_null()) return std::nullopt;
  return value.as_string();
}

TEST_P(DsnVector, SaysWhatItSaysOrIsRefused) {
  const sde::Json spec = read_json(vectors_root() / "dsn" / GetParam() / "case.json");
  const std::string& dsn = spec.find("dsn")->as_string();
  SCOPED_TRACE(spec.find("why")->as_string());
  if (const sde::Json* refused = spec.find("refused")) {
    try {
      (void)parse_dsn(dsn);
      ADD_FAILURE() << "accepted";
    } catch (const sde::EngineError& error) {
      const std::string message = error.what();
      EXPECT_NE(message.find(refused->find("match")->as_string()), std::string::npos) << message;
      if (const sde::Json* absent = refused->find("absent")) {
        EXPECT_EQ(message.find(absent->as_string()), std::string::npos) << message;
      }
    }
    return;
  }
  const sde::Json& says = *spec.find("says");
  const Target got = parse_dsn(dsn);
  EXPECT_EQ(got.host, says.find("host")->as_string());
  EXPECT_EQ(got.port, says.find("port")->to_int64().value());
  const std::optional<std::string> identity = text_or_null(*says.find("identity"));
  const std::optional<std::string> secret = text_or_null(*says.find("secret"));
  EXPECT_EQ(got.auth.has_value(), identity.has_value());
  if (got.auth && identity && secret) {
    EXPECT_EQ(got.auth->first, *identity);
    EXPECT_EQ(got.auth->second, *secret);
  }
  EXPECT_EQ(got.tls, says.find("tls")->as_bool());
  EXPECT_EQ(got.ca, text_or_null(*says.find("ca")));
  EXPECT_EQ(got.verify, says.find("verify")->as_bool());
  // Compared as the double it is: the JSON reader rounds to the nearest double, as the parser does.
  EXPECT_EQ(got.timeout, says.find("timeout")->to_double().value());
}

INSTANTIATE_TEST_SUITE_P(Dsn, DsnVector, ::testing::ValuesIn(cases("dsn")),
                         [](const auto& param_info) { return test_name(param_info.param); });

TEST(DsnFamily, EveryCaseRunsAndBothOutcomesAreThere) {
  EXPECT_GE(count_cases("dsn"), 100U);
  EXPECT_EQ(cases("dsn").size(), count_cases("dsn"));
  int says = 0;
  int refused = 0;
  for (const std::string& name : cases("dsn")) {
    const sde::Json spec = read_json(vectors_root() / "dsn" / name / "case.json");
    (spec.contains("says") ? says : refused) += 1;
  }
  // A family of refusals alone proves a library can refuse, never that it can accept.
  EXPECT_GT(says, 0);
  EXPECT_GT(refused, 0);
}

}  // namespace

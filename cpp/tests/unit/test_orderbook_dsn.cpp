/// The orderbook engine's DSN beyond the shared vectors (`dsn/`, format contract 7n, run by the
/// conformance binary): what only C++ can be handed - a NUL inside the text, bytes that are not
/// UTF-8 - and the rules a target given field by field is held to before a socket opens.

#include <cmath>
#include <limits>
#include <string>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

#include "engines/orderbook/dsn.hpp"
#include "sde/errors.hpp"

namespace {

using sde::detail::orderbook::check_target;
using sde::detail::orderbook::parse_dsn;
using sde::detail::orderbook::Target;

std::string refusal_of(const Target& target) {
  try {
    check_target(target);
  } catch (const sde::EngineError& error) {
    return error.what();
  }
  return "accepted";
}

Target plain() {
  Target target;
  target.host = "h";
  target.port = 1;
  return target;
}

TEST(OrderbookDsn, BytesNoOtherLanguageCanHandOverAreNotADsn) {
  // A Python str and a JavaScript string hold neither a NUL that ends a C string nor bytes that are
  // not text; a std::string_view holds both. Rule 1 refuses each before anything else is read.
  for (const std::string& dsn : {std::string("orderbook://h:1\0?tls=on", 23),
                                 std::string("orderbook://h\xff:1"),
                                 std::string("orderbook://app:s\xc3\xa9@h:1")}) {
    try {
      (void)parse_dsn(dsn);
      ADD_FAILURE() << "accepted";
    } catch (const sde::EngineError& error) {
      EXPECT_EQ(std::string(error.what()).rfind("an orderbook DSN is printable ASCII", 0), 0U)
          << error.what();
    }
  }
}

TEST(OrderbookDsn, ATargetGivenFieldByFieldIsHeldToTheDsnsRules) {
  EXPECT_EQ(refusal_of(plain()), "accepted");
  Target widest = plain();
  widest.port = 65535;
  widest.auth = std::make_pair(std::string("desk:1"), std::string("s"));
  widest.timeout = 2147483.647;
  EXPECT_EQ(refusal_of(widest), "accepted");
  widest.timeout = 0.001;
  EXPECT_EQ(refusal_of(widest), "accepted");

  for (const int port : {0, 65536, -1}) {
    Target target = plain();
    target.port = port;
    EXPECT_NE(refusal_of(target).find("a port from 1 to 65535"), std::string::npos) << port;
  }
  const std::vector<std::pair<std::string, std::string>> credentials = {
      {"two words", "s"}, {"app@desk", "s"}, {std::string(65, 'a'), "s"},
      {"", "s"},          {"app", ""},       {"app\xe2\x80\x8b", "s"}};
  for (const auto& auth : credentials) {
    Target target = plain();
    target.auth = auth;
    EXPECT_NE(refusal_of(target).find("an identity of 1 to 64 characters of [A-Za-z0-9_.:-]"),
              std::string::npos)
        << auth.first;
  }
  Target ca = plain();
  ca.ca = "/ca.pem";
  EXPECT_NE(refusal_of(ca).find("verifies nothing"), std::string::npos);
  Target unverified = plain();
  unverified.verify = false;
  EXPECT_NE(refusal_of(unverified).find("no certificate to decline"), std::string::npos);
  Target anchor = plain();
  anchor.tls = true;
  anchor.ca = "/ca.pem";
  anchor.verify = false;
  EXPECT_EQ(refusal_of(anchor), "tls_ca_file with tls_verify=False is a trust anchor nothing consults");
  for (const double timeout : {0.0, 0.0009, 2147483.648, std::numeric_limits<double>::infinity(),
                               std::numeric_limits<double>::quiet_NaN(), -1.0}) {
    Target target = plain();
    target.timeout = timeout;
    EXPECT_EQ(refusal_of(target), "timeout is a number of seconds from 0.001 to 2147483.647")
        << timeout;
  }
}

}  // namespace
